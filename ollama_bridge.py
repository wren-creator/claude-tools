import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ollama-bridge")

LOG_PATH = Path(__file__).parent / "ollama_log.jsonl"
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_TIMEOUT = 60
GIT_TIMEOUT = 30
MAX_CONTEXT_CHARS = 20_000  # local 7B models have far less usable context than Gemini
MAX_LOG_CHARS = 40_000  # logs run longer than diffs but still need a hard ceiling
DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_NUM_CTX = 8192
LOG_NUM_CTX = 24576  # generous headroom over MAX_LOG_CHARS even at a dense ~2 chars/token

PREFILTER_INSTRUCTIONS = (
    "You are a fast, local first-pass reviewer for a git diff. Flag only "
    "clear issues: bugs, security problems, or obvious simplification "
    "opportunities. If the diff looks clean, say so plainly - start your "
    "reply with 'CLEAN: no issues found.' If you found something, start "
    "with 'FLAGGED: <one-line reason>' then list the issues. Be terse - "
    "this is a cheap triage pass before a stronger model reviews the same "
    "diff, not the final word."
)

TRIAGE_INSTRUCTIONS = (
    "You are triaging a build/test failure log for a coding agent that "
    "can't afford to read the whole thing. Find the FIRST/root failure - "
    "later errors are often just fallout from it. Reply in exactly this "
    "format:\n"
    "FILE: <path:line, or 'unknown' if none appears in the log>\n"
    "ERROR: <the exact error/exception message, verbatim>\n"
    "CONTEXT: <2-5 lines of the most relevant surrounding output, verbatim>\n"
    "If the log shows no failure (e.g. a clean passing run), reply exactly "
    "'NO FAILURE FOUND.' and nothing else."
)


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _truncate(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated {len(text) - limit} chars ...]"


def _truncate_keep_tail(text: str, limit: int = MAX_LOG_CHARS) -> str:
    # Build/test failures are almost always near the end of a log - the
    # start is usually setup noise (dependency resolution, banners, etc),
    # unlike a diff where head-truncation (_truncate above) is fine.
    if len(text) <= limit:
        return text
    return f"[... truncated {len(text) - limit} chars from the start ...]\n\n" + text[-limit:]


def _resolve_in_repo(repo_path: str, path: str) -> Path | None:
    root = Path(repo_path).resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        return None
    return target


def _call_ollama(prompt: str, model: str, num_ctx: int = DEFAULT_NUM_CTX) -> str:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            # Ollama defaults num_ctx to 2048 tokens regardless of the
            # model's real max - a prompt near the truncation ceiling would
            # get silently left-truncated, dropping the instructions
            # entirely. Callers pass a num_ctx that comfortably covers
            # their own truncation limit plus the instructions, within
            # every installed model's own context_length.
            "num_ctx": num_ctx,
            "temperature": 0.0,  # deterministic, repeatable triage
        },
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return f"Error calling Ollama: HTTP {e.code} - {e.read().decode(errors='replace')}"
    except urllib.error.URLError as e:
        # socket.timeout is a TimeoutError subclass and also an OSError -
        # check it first, or a slow-but-reachable Ollama gets mislabeled as
        # "not reachable" instead of "timed out".
        if isinstance(e.reason, TimeoutError):
            return f"Error calling Ollama: timed out after {OLLAMA_TIMEOUT}s"
        if isinstance(e.reason, OSError):
            return (
                f"Error calling Ollama: not reachable at {OLLAMA_HOST} - "
                "is `ollama serve` running?"
            )
        return f"Error calling Ollama: {e.reason}"
    except TimeoutError:
        return f"Error calling Ollama: timed out after {OLLAMA_TIMEOUT}s"

    if "error" in body:
        return f"Error calling Ollama: {body['error']}"
    return body.get("response", "").strip()


def _git_diff(repo_path: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "diff"] + args,
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )


@mcp.tool()
def prefilter_diff(repo_path: str = ".", model: str = DEFAULT_MODEL) -> str:
    """Run `git diff` in repo_path and send it to a local Ollama model for a
    cheap first-pass triage, before spending a review_diff (Gemini) call on
    it. Pass the absolute path of the repo being worked on - the bridge runs
    as its own process and does not share Claude Code's cwd.
    The response starts with 'CLEAN:' or 'FLAGGED:' - only call review_diff
    afterward if it's FLAGGED. If this tool errors (e.g. Ollama isn't
    running), fall back to review_diff directly rather than skipping review
    entirely.
    Checks, in order: uncommitted changes vs HEAD (staged + unstaged
    together), then staged-only (works even in a repo with zero commits
    yet), then plain unstaged - same order as review_diff.
    """
    try:
        diff = _git_diff(repo_path, ["HEAD"])
        if diff.returncode != 0:
            diff = _git_diff(repo_path, ["--cached"])
        if diff.returncode == 0 and not diff.stdout.strip():
            diff = _git_diff(repo_path, [])
    except FileNotFoundError:
        return f"Error: repo_path '{repo_path}' does not exist or `git` not found"

    if diff.returncode != 0:
        return f"Error running git diff: {diff.stderr.strip()}"
    if not diff.stdout.strip():
        return "No changes to review (checked against HEAD, staged, and unstaged)."

    prompt = f"{PREFILTER_INSTRUCTIONS}\n\n```diff\n{_truncate(diff.stdout)}\n```"
    response = _call_ollama(prompt, model)
    _log({
        "tool": "prefilter_diff",
        "repo_path": repo_path,
        "model": model,
        "diff_len": len(diff.stdout),
        "response": response,
    })
    return response


@mcp.tool()
def triage_log(repo_path: str, log_path: str, model: str = DEFAULT_MODEL) -> str:
    """Read a build/test log file and send it to a local Ollama model to
    extract just the root failure, instead of reading the whole raw log
    directly. Pass the absolute path of the repo/project as repo_path, and
    log_path as either an absolute path or one relative to repo_path - the
    log must live inside repo_path (same containment rule as repo-bridge's
    get_file), rejected otherwise, so this can't be pointed at arbitrary
    files elsewhere on disk (~/.ssh, .env, etc). Redirect a failing
    command's output there first: `cmd > out.log 2>&1`.
    Returns a FILE/ERROR/CONTEXT summary, or 'NO FAILURE FOUND.' if the log
    looks clean - plus a pointer back to log_path. Re-read log_path directly
    if the summary looks incomplete or wrong: a 7B model can misidentify
    the root cause in a complex multi-error log, this is a first pass, not
    a guarantee.
    """
    target = _resolve_in_repo(repo_path, log_path)
    if target is None:
        return f"Error: '{log_path}' escapes repo_path"

    try:
        text = target.read_text(errors="replace")
    except OSError as e:
        return f"Error reading '{log_path}': {e}"

    if not text.strip():
        return f"'{log_path}' is empty - nothing to triage."

    line_count = text.count("\n") + 1
    prompt = f"{TRIAGE_INSTRUCTIONS}\n\n````\n{_truncate_keep_tail(text)}\n````"
    response = _call_ollama(prompt, model, num_ctx=LOG_NUM_CTX)
    _log({
        "tool": "triage_log",
        "repo_path": repo_path,
        "log_path": log_path,
        "model": model,
        "log_len": len(text),
        "line_count": line_count,
        "response": response,
    })
    return (
        f"{response}\n\n"
        f"(Triaged from {line_count} lines / {len(text)} chars at "
        f"'{log_path}' - re-read it directly if this summary looks "
        f"incomplete or wrong.)"
    )


if __name__ == "__main__":
    mcp.run()
