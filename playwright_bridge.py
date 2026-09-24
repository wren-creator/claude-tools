import asyncio
import json
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright

mcp = FastMCP("playwright-bridge")

LOG_PATH = Path(__file__).parent / "playwright_log.jsonl"
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}

# session_id -> {"playwright", "browser", "context", "page", "requests"}.
# This process is long-lived (one per Claude Code session), so sessions and
# their browsers persist across tool calls until close() or the server
# itself is restarted. Browser *binaries* are cached on disk by `playwright
# install` (see README setup) and never re-downloaded per session — only
# the in-process browser/page objects are per-session state.
#
# Uses playwright.async_api, not sync_api: FastMCP dispatches tool calls as
# coroutines on its own running asyncio event loop, and Playwright's sync
# API refuses to run inside one ("Playwright Sync API inside the asyncio
# loop" — it always fails, not intermittently). Every tool below is `async
# def` and awaits its Playwright calls for exactly this reason.
SESSIONS: dict[str, dict] = {}

_BROWSER_TYPES = {"chromium", "firefox", "webkit"}

# Sessions idle longer than this get closed by the reaper. Each one is a real
# browser process; a forgotten session otherwise lives until the server dies.
IDLE_TIMEOUT_S = 600
REAP_INTERVAL_S = 60
_reaper_task: asyncio.Task | None = None


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _get_session(session_id: str) -> dict | None:
    session = SESSIONS.get(session_id)
    if session is not None:
        session["last_used"] = time.time()
    return session


async def _shutdown(session: dict) -> None:
    # Close each layer independently so one failure doesn't strand the rest.
    for key, method in (("context", "close"), ("browser", "close"), ("playwright", "stop")):
        obj = session.get(key)
        if obj is not None:
            try:
                await getattr(obj, method)()
            except Exception:
                pass


async def _reap_idle() -> None:
    while True:
        await asyncio.sleep(REAP_INTERVAL_S)
        cutoff = time.time() - IDLE_TIMEOUT_S
        for sid in [k for k, v in SESSIONS.items() if v.get("last_used", 0) < cutoff]:
            session = SESSIONS.pop(sid, None)
            if session is not None:
                await _shutdown(session)
                _log({"tool": "reaper", "session_id": sid, "reason": f"idle over {IDLE_TIMEOUT_S}s"})


def _ensure_reaper() -> None:
    global _reaper_task
    if _reaper_task is None or _reaper_task.done():
        _reaper_task = asyncio.get_running_loop().create_task(_reap_idle())


@mcp.tool()
async def launch(browser: str = "chromium", headless: bool = True,
                  viewport_width: int = 0, viewport_height: int = 0) -> str:
    """Launch a browser and return a session_id. Pass that session_id to
    goto/evaluate/screenshot/get_requests/click/fill/close.

    browser is "chromium", "firefox", or "webkit" - all three are installed
    once via `playwright install` (see this bridge's README setup) and
    launch from the local cache with no network download, unlike ad-hoc
    `npx playwright install` which re-fetches browser binaries from scratch
    on a machine/session that doesn't already have them cached.

    Every request the page makes (main document, scripts, fonts, XHR, etc.)
    is recorded from launch until close() - see get_requests. This is the
    main thing this bridge exists for: proving a page did or didn't hit a
    given URL (e.g. confirming a CDN dependency was actually removed),
    without needing devtools open.
    """
    if browser not in _BROWSER_TYPES:
        return f"Error: browser must be one of {sorted(_BROWSER_TYPES)}, got '{browser}'"

    _ensure_reaper()
    parts: dict = {}
    try:
        pw = parts["playwright"] = await async_playwright().start()
        browser_type = getattr(pw, browser)
        b = parts["browser"] = await browser_type.launch(headless=headless)
        viewport = DEFAULT_VIEWPORT.copy()
        if viewport_width and viewport_height:
            viewport = {"width": viewport_width, "height": viewport_height}
        context = parts["context"] = await b.new_context(viewport=viewport)
        page = await context.new_page()
    except Exception as e:
        await _shutdown(parts)  # a failed launch used to strand its Playwright driver
        _log({"tool": "launch", "browser": browser, "error": str(e)})
        return f"Error launching {browser}: {e}"

    session_id = uuid.uuid4().hex
    requests: list[dict] = []

    def _on_request(req):
        requests.append({"url": req.url, "method": req.method, "resource_type": req.resource_type})

    def _on_response(resp):
        for r in requests:
            if r["url"] == resp.url and "status" not in r:
                r["status"] = resp.status
                break

    page.on("request", _on_request)
    page.on("response", _on_response)

    SESSIONS[session_id] = {
        "playwright": pw, "browser": b, "context": context, "page": page,
        "requests": requests, "last_used": time.time(),
    }
    _log({"tool": "launch", "browser": browser, "headless": headless, "session_id": session_id})
    return session_id


@mcp.tool()
async def goto(session_id: str, url: str, wait_until: str = "load") -> str:
    """Navigate an open session's page to url. wait_until is one of "load",
    "domcontentloaded", "networkidle", or "commit" (same meaning as
    Playwright's own goto() option - "load" is a reasonable default,
    "networkidle" is stricter and useful when a page loads assets
    asynchronously after the load event, e.g. web fonts).

    Returns JSON {"url", "title", "status"} on success.
    """
    session = _get_session(session_id)
    if session is None:
        return f"Error: no active session with id {session_id}"

    try:
        response = await session["page"].goto(url, wait_until=wait_until)
        result = {
            "url": session["page"].url,
            "title": await session["page"].title(),
            "status": response.status if response else None,
        }
    except Exception as e:
        _log({"tool": "goto", "session_id": session_id, "url": url, "error": str(e)})
        return f"Error navigating to {url}: {e}"

    _log({"tool": "goto", "session_id": session_id, "url": url, "result": result})
    return json.dumps(result)


@mcp.tool()
async def evaluate(session_id: str, script: str) -> str:
    """Run JavaScript in an open session's page and return the JSON-encoded
    result. script is a JS expression or function body, same as Playwright's
    own page.evaluate() - e.g. "document.title", "() => document.title", or
    "async () => { await document.fonts.ready; return document.fonts.size; }".
    A script that returns a Promise is automatically awaited.

    This is the general-purpose tool for reading page/DOM state (computed
    styles, document.fonts, calling into the page's own JS functions via a
    dynamic import, reading back textContent) that the other tools here
    don't have a dedicated shape for.
    """
    session = _get_session(session_id)
    if session is None:
        return f"Error: no active session with id {session_id}"

    try:
        result = await session["page"].evaluate(script)
    except Exception as e:
        _log({"tool": "evaluate", "session_id": session_id, "script": script, "error": str(e)})
        return f"Error evaluating script: {e}"

    encoded = json.dumps(result, default=str)
    _log({"tool": "evaluate", "session_id": session_id, "script": script, "result": encoded})
    return encoded


@mcp.tool()
async def screenshot(session_id: str, output_path: str, full_page: bool = False, selector: str = "") -> str:
    """Take a screenshot of an open session's page and save it to
    output_path (absolute path, .png). With selector set, screenshots just
    that element instead of the viewport/page. Returns output_path on
    success - read it back with Claude Code's own Read tool to view it.
    A .jpg/.jpeg output_path saves a quality-60 JPEG, roughly a third smaller
    than PNG on a busy page (no gain on a near-blank one). Prefer snapshot when you only
    need to know what's on the page, not how it looks.
    """
    session = _get_session(session_id)
    if session is None:
        return f"Error: no active session with id {session_id}"

    try:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        opts = {"path": str(path)}
        if path.suffix.lower() in (".jpg", ".jpeg"):
            opts.update(type="jpeg", quality=60)
        if selector:
            await session["page"].locator(selector).screenshot(**opts)
        else:
            await session["page"].screenshot(full_page=full_page, **opts)
    except Exception as e:
        _log({"tool": "screenshot", "session_id": session_id, "output_path": output_path, "error": str(e)})
        return f"Error taking screenshot: {e}"

    _log({"tool": "screenshot", "session_id": session_id, "output_path": output_path, "selector": selector})
    return output_path


@mcp.tool()
def get_requests(session_id: str, url_contains: str = "") -> str:
    """Return every network request an open session's page has made since
    launch(), as a JSON list of {"url", "method", "resource_type",
    "status"}. With url_contains set, only returns requests whose URL
    contains that substring - e.g. url_contains="fonts.googleapis.com" to
    confirm a CDN dependency is (or isn't) actually being hit, without
    needing devtools open. "status" is absent for a request still in
    flight when this is called.
    """
    session = _get_session(session_id)
    if session is None:
        return f"Error: no active session with id {session_id}"

    reqs = session["requests"]
    if url_contains:
        reqs = [r for r in reqs if url_contains in r["url"]]
    result = json.dumps(reqs)
    _log({"tool": "get_requests", "session_id": session_id, "url_contains": url_contains, "count": len(reqs)})
    return result


@mcp.tool()
async def click(session_id: str, selector: str, timeout_ms: int = 5000) -> str:
    """Click the first element matching selector (CSS, or Playwright's
    text=/role= selector syntax) in an open session's page."""
    session = _get_session(session_id)
    if session is None:
        return f"Error: no active session with id {session_id}"

    try:
        await session["page"].click(selector, timeout=timeout_ms)
    except Exception as e:
        _log({"tool": "click", "session_id": session_id, "selector": selector, "error": str(e)})
        return f"Error clicking '{selector}': {e}"

    _log({"tool": "click", "session_id": session_id, "selector": selector})
    return f"Clicked '{selector}'"


@mcp.tool()
async def fill(session_id: str, selector: str, text: str, timeout_ms: int = 5000) -> str:
    """Fill the first element matching selector (CSS, or Playwright's
    text=/role= selector syntax) with text in an open session's page,
    replacing any existing value."""
    session = _get_session(session_id)
    if session is None:
        return f"Error: no active session with id {session_id}"

    try:
        await session["page"].fill(selector, text, timeout=timeout_ms)
    except Exception as e:
        _log({"tool": "fill", "session_id": session_id, "selector": selector, "error": str(e)})
        return f"Error filling '{selector}': {e}"

    _log({"tool": "fill", "session_id": session_id, "selector": selector})
    return f"Filled '{selector}'"


@mcp.tool()
async def snapshot(session_id: str, selector: str = "body", max_chars: int = 6000) -> str:
    """Return the page (or the element at selector) as Playwright's ARIA
    snapshot: a compact YAML outline of roles, names and text, e.g.
    `- button "Submit"`. Far cheaper than a screenshot for finding what to
    click or confirming what text is on screen. Truncated to max_chars.
    """
    session = _get_session(session_id)
    if session is None:
        return f"Error: no active session with id {session_id}"

    try:
        text = await session["page"].locator(selector).aria_snapshot()
    except Exception as e:
        _log({"tool": "snapshot", "session_id": session_id, "selector": selector, "error": str(e)})
        return f"Error taking snapshot of '{selector}': {e}"

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[... truncated {len(text) - max_chars} chars ...]"
    _log({"tool": "snapshot", "session_id": session_id, "selector": selector, "chars": len(text)})
    return text


_STEP_LIMIT = 2000  # per-step result cap so a chatty evaluate can't flood the reply


@mcp.tool()
async def run_steps(session_id: str, steps: list[dict], stop_on_error: bool = True) -> str:
    """Run several actions in one call instead of one round trip each.
    steps is a list of objects, each with an "action" and its arguments:
      {"action": "goto", "url": "...", "wait_until": "load"}
      {"action": "click", "selector": "..."}
      {"action": "fill", "selector": "...", "text": "..."}
      {"action": "evaluate", "script": "..."}
      {"action": "snapshot", "selector": "body"}
      {"action": "screenshot", "output_path": "/abs/path.png", "full_page": false}
      {"action": "wait_for", "selector": "...", "timeout_ms": 5000}
    Returns a JSON list of {"step", "action", "result"}. Stops at the first
    error unless stop_on_error is false; the failing step is marked "error".
    """
    if _get_session(session_id) is None:
        return f"Error: no active session with id {session_id}"

    async def wait_for(session_id: str, selector: str, timeout_ms: int = 5000) -> str:
        try:
            await SESSIONS[session_id]["page"].wait_for_selector(selector, timeout=timeout_ms)
        except Exception as e:
            return f"Error waiting for '{selector}': {e}"
        return f"Found '{selector}'"

    handlers = {"goto": goto, "click": click, "fill": fill, "evaluate": evaluate,
                "snapshot": snapshot, "screenshot": screenshot, "wait_for": wait_for}
    results = []
    for i, step in enumerate(steps, 1):
        action = step.get("action")
        fn = handlers.get(action)
        if fn is None:
            out = f"Error: unknown action '{action}', expected one of {sorted(handlers)}"
        else:
            args = {k: v for k, v in step.items() if k != "action"}
            try:
                out = await fn(session_id, **args)
            except TypeError as e:  # a missing or misspelled argument
                out = f"Error: bad arguments for '{action}': {e}"
        failed = out.startswith("Error")
        results.append({"step": i, "action": action,
                        ("error" if failed else "result"): out[:_STEP_LIMIT]})
        if failed and stop_on_error:
            break
    _log({"tool": "run_steps", "session_id": session_id, "steps": len(steps), "ran": len(results)})
    return json.dumps(results)


@mcp.tool()
async def close(session_id: str) -> str:
    """Close an open session's browser and free its resources."""
    session = SESSIONS.pop(session_id, None)
    if session is None:
        return f"Error: no active session with id {session_id}"

    await _shutdown(session)
    _log({"tool": "close", "session_id": session_id})
    return f"Closed session {session_id}"


if __name__ == "__main__":
    mcp.run()
