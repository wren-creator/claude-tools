import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("slack-digest-bridge")

ENV_FILE = Path.home() / ".slack" / ".env"
LOG_PATH = Path(__file__).parent / "slack_digest_log.jsonl"
SLACK_API = "https://slack.com/api"
HTTP_TIMEOUT = 30
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_TIMEOUT = 60
OLLAMA_NUM_CTX = 8192
DEFAULT_MODEL = "qwen2.5-coder:7b"
MAX_CONTEXT_CHARS = 20_000
REPLIES_LIMIT = 200

# Slack permalink shape: https://<workspace>.slack.com/archives/<CHANNEL_ID>/p<TS_DIGITS>
# TS_DIGITS is the message timestamp with the decimal point removed
# (p1786041731822489 -> 1786041731.822489). A reply's permalink carries the
# thread's parent ts as a ?thread_ts= query param; a parent message's
# permalink doesn't, so its own ts IS the thread ts.
PERMALINK_RE = re.compile(r"/archives/([A-Z0-9]+)/p(\d+)")

SKIP_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose"}

DIGEST_INSTRUCTIONS = (
    "You are digesting a Slack thread for a coding agent that can't afford "
    "to read the whole raw conversation. Extract only what's actually "
    "load-bearing. Reply in exactly this format:\n"
    "DECISIONS: <bullet list, or 'none'>\n"
    "ACTION_ITEMS: <bullet list, with owner if named, or 'none'>\n"
    "BLOCKERS: <bullet list of open questions/unresolved issues, or 'none'>\n"
    "Skip greetings, reactions, and back-and-forth that didn't land on "
    "anything. Be terse. Messages may contain raw Slack user IDs like "
    "<@U12345> instead of names - carry them through as-is, don't guess a "
    "name."
)


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _load_token() -> str | None:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("SLACK_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def _parse_thread_url(url: str) -> tuple[str, str] | None:
    m = PERMALINK_RE.search(url)
    if not m:
        return None
    channel_id, ts_digits = m.group(1), m.group(2)
    own_ts = f"{ts_digits[:-6]}.{ts_digits[-6:]}"
    query = urllib.parse.urlparse(url).query
    thread_ts = urllib.parse.parse_qs(query).get("thread_ts", [own_ts])[0]
    return channel_id, thread_ts


def _slack_get(method: str, token: str, params: dict) -> dict:
    url = f"{SLACK_API}/{method}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def _truncate(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated {len(text) - limit} chars ...]"


def _call_ollama(prompt: str, model: str) -> str:
    # Same shape as ollama-bridge's _call_ollama, duplicated rather than
    # imported - each bridge in this repo runs as its own standalone MCP
    # server process, none of them import from each other.
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": OLLAMA_NUM_CTX, "temperature": 0.0},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate", data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return f"Error calling Ollama: HTTP {e.code} - {e.read().decode(errors='replace')}"
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            return f"Error calling Ollama: timed out after {OLLAMA_TIMEOUT}s"
        if isinstance(e.reason, OSError):
            return f"Error calling Ollama: not reachable at {OLLAMA_HOST} - is `ollama serve` running?"
        return f"Error calling Ollama: {e.reason}"
    except TimeoutError:
        return f"Error calling Ollama: timed out after {OLLAMA_TIMEOUT}s"

    if "error" in body:
        return f"Error calling Ollama: {body['error']}"
    return body.get("response", "").strip()


@mcp.tool()
def digest_thread(thread_url: str, model: str = DEFAULT_MODEL) -> str:
    """Fetch a Slack thread server-side and send it to a local Ollama model
    for a decisions/action-items/blockers digest, instead of the raw thread
    ever landing in the agent's context. Pass a Slack permalink to any
    message in the thread (e.g. from a message's "Copy link" action) -
    works for both the parent message's link and a reply's link.

    Requires SLACK_BOT_TOKEN in ~/.slack/.env, and the bot must already be a
    member of the channel (/invite it there first) - conversations.replies
    returns a 'not_in_channel' error otherwise.

    Returns DECISIONS/ACTION_ITEMS/BLOCKERS sections, or an error. This is a
    first pass, not a transcript - use slack_read_thread for the raw
    messages if the digest looks incomplete or you need exact wording.
    """
    token = _load_token()
    if not token:
        return f"Error: SLACK_BOT_TOKEN not found in {ENV_FILE}"

    parsed = _parse_thread_url(thread_url)
    if parsed is None:
        return (
            f"Error: could not parse a channel/message from '{thread_url}' - "
            "expected a Slack permalink like "
            "https://workspace.slack.com/archives/C.../p1234567890123456"
        )
    channel_id, thread_ts = parsed

    try:
        result = _slack_get(
            "conversations.replies", token,
            {"channel": channel_id, "ts": thread_ts, "limit": REPLIES_LIMIT},
        )
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        return f"Error calling Slack API: {e}"

    if not result.get("ok"):
        error = result.get("error", "unknown_error")
        hint = ""
        if error == "not_in_channel":
            hint = " - invite the bot to this channel first (/invite it)"
        elif error == "channel_not_found":
            hint = " - check the bot has access and the URL is correct"
        elif error == "invalid_auth":
            hint = f" - check SLACK_BOT_TOKEN in {ENV_FILE}"
        return f"Error from Slack API: {error}{hint}"

    messages = result.get("messages", [])
    lines = [
        m["text"].strip()
        for m in messages
        if m.get("subtype") not in SKIP_SUBTYPES and m.get("text", "").strip()
    ]
    if not lines:
        return "Thread has no message text to digest."

    formatted = "\n---\n".join(lines)
    prompt = f"{DIGEST_INSTRUCTIONS}\n\n```\n{_truncate(formatted)}\n```"
    response = _call_ollama(prompt, model)

    _log({
        "tool": "digest_thread", "channel": channel_id, "thread_ts": thread_ts,
        "model": model, "message_count": len(messages), "response": response,
    })

    footer = (
        f"\n\n(Digested from {len(messages)} messages in this thread - "
        "use slack_read_thread for the raw conversation if this looks "
        "incomplete or wrong.)"
    )
    if result.get("has_more"):
        footer += (
            f" NOTE: this thread has more than {REPLIES_LIMIT} replies; "
            "only the first batch was digested."
        )
    return response + footer


if __name__ == "__main__":
    mcp.run()
