import json
import os
import socket
import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from diff_utils import collect_diff

mcp = FastMCP("gemini-bridge")

LOG_PATH = Path(__file__).parent / "log.jsonl"
GEMINI_ENV_FILE = Path.home() / ".gemini" / ".env"
GEMINI_TIMEOUT = 300
GIT_TIMEOUT = 30
MAX_CONTEXT_CHARS = 60_000  # guard against blowing past Gemini's context window
MAX_FILE_CONTEXT_CHARS = 500_000  # ask_gemini_about_files exists specifically to use Gemini's much larger window
NETWORK_CHECK_HOST = "generativelanguage.googleapis.com"
RETRY_DELAYS = (2, 5, 10)  # seconds between attempts on a transient 503/connection reset
NETWORK_CHECK_TIMEOUT = 5  # fail fast on a flaky connection instead of waiting GEMINI_TIMEOUT


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _truncate(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated {len(text) - limit} chars ...]"


def _gemini_env() -> dict:
    # gemini-cli's own ~/.gemini/.env auto-discovery doesn't reliably fire
    # when invoked as a subprocess from an arbitrary cwd, so load it ourselves.
    env = os.environ.copy()
    if GEMINI_ENV_FILE.exists():
        for line in GEMINI_ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    env.setdefault("GEMINI_CLI_TRUST_WORKSPACE", "true")
    return env


def _network_reachable() -> bool:
    try:
        socket.create_connection((NETWORK_CHECK_HOST, 443), timeout=NETWORK_CHECK_TIMEOUT).close()
        return True
    except OSError:
        return False


def _call_gemini_once(prompt: str) -> str:
    if not _network_reachable():
        return (
            f"Error calling Gemini: no network reachable to {NETWORK_CHECK_HOST} "
            f"within {NETWORK_CHECK_TIMEOUT}s - skipping call rather than waiting "
            f"out the full {GEMINI_TIMEOUT}s timeout."
        )

    try:
        result = subprocess.run(
            ["gemini", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=GEMINI_TIMEOUT,
            env=_gemini_env(),
        )
    except subprocess.TimeoutExpired:
        return f"Error calling Gemini: timed out after {GEMINI_TIMEOUT}s"
    except FileNotFoundError:
        return "Error calling Gemini: `gemini` CLI not found on PATH"

    if result.returncode != 0:
        return f"Error calling Gemini: {result.stderr.strip()}"
    return result.stdout.strip()


def _is_transient(err: str) -> bool:
    # 503 "high demand" spikes and dropped connections usually clear in seconds.
    # A 429 quota error does NOT: the daily quota won't reset in 10s, so
    # retrying just burns time (and possibly more quota).
    if "429" in err or "RESOURCE_EXHAUSTED" in err:
        return False
    return any(t in err for t in ("503", "UNAVAILABLE", "Connection reset", "ECONNRESET"))


def _call_gemini(prompt: str) -> str:
    response = _call_gemini_once(prompt)
    for delay in RETRY_DELAYS:
        if not (response.startswith("Error calling Gemini") and _is_transient(response)):
            break
        time.sleep(delay)
        response = _call_gemini_once(prompt)
    return response


@mcp.tool()
def ask_gemini(prompt: str, context: str = "") -> str:
    """Ask Gemini CLI a question and return its response.
    Use for a second opinion, code review, or when you want
    a different model's take on an approach.
    """
    context = _truncate(context)
    full_prompt = f"{context}\n\n{prompt}" if context else prompt
    response = _call_gemini(full_prompt)
    _log({
        "tool": "ask_gemini",
        "prompt": prompt,
        "context_len": len(context),
        "response": response,
    })
    return response


@mcp.tool()
def review_diff(
    repo_path: str = ".",
    instructions: str = "Review this diff for bugs, security issues, and simplification opportunities.",
) -> str:
    """Run `git diff` in repo_path and send it to Gemini for critique.
    Pass the absolute path of the repo currently being worked on as repo_path -
    the bridge runs as its own process and does not share Claude Code's cwd.
    Use before committing to get a second model's opinion on the changes.
    Checks, in order: uncommitted changes vs HEAD (staged + unstaged together),
    then staged-only (works even in a repo with zero commits yet), then plain
    unstaged, so staged-only commits and brand-new repos aren't reported as
    having nothing to review.
    """
    diff_text, err = collect_diff(repo_path)
    if err:
        return err
    if not diff_text.strip():
        return "No changes to review (checked against HEAD, staged, unstaged, and untracked)."

    prompt = f"{instructions}\n\n```diff\n{_truncate(diff_text)}\n```"
    response = _call_gemini(prompt)
    fallback = False
    if response.startswith("Error calling Gemini"):
        # Gemini is down or out of quota: a local review beats no review.
        # Imported lazily so this server doesn't need Ollama unless it's used.
        from ollama_bridge import run_prefilter
        local = run_prefilter(repo_path)
        if not local.startswith("Error"):
            response = f"[Gemini unavailable ({response[:120]}), local Ollama prefilter used instead]\n{local}"
            fallback = True
    _log({
        "tool": "review_diff",
        "repo_path": repo_path,
        "instructions": instructions,
        "diff_len": len(diff_text),
        "fallback": fallback,
        "response": response,
    })
    return response


@mcp.tool()
def commit_gate(repo_path: str = ".") -> str:
    """One call for the pre-commit second opinion. Runs the local Ollama
    prefilter first; only if it comes back FLAGGED (or errors) does it spend
    a Gemini review_diff call, and returns a single verdict either way.
    Replaces the two-step "prefilter_diff, then maybe review_diff" routine.
    Pass the absolute path of the repo being worked on.
    """
    from ollama_bridge import run_prefilter
    local = run_prefilter(repo_path)
    if local.startswith("CLEAN") or local.startswith("No changes"):
        _log({"tool": "commit_gate", "repo_path": repo_path, "escalated": False, "response": local})
        return f"{local}\n(local prefilter only, Gemini not called)"
    if local.startswith("FLAGGED"):
        review = review_diff(repo_path)
        response = f"Local prefilter: {local}\n\nGemini review:\n{review}"
    else:  # Ollama errored (not running, timed out): go straight to Gemini
        response = review_diff(repo_path)
    _log({"tool": "commit_gate", "repo_path": repo_path, "escalated": True, "response": response})
    return response


@mcp.tool()
def ask_gemini_about_files(file_paths: list[str], question: str) -> str:
    """Read one or more full files and ask Gemini a question about them.
    Use this when Claude's context is too full to hold the files itself, or
    when you specifically want Gemini's take on entire files/modules rather
    than a diff or a truncated excerpt - Gemini's context window is large
    enough to hold much more than ask_gemini's context param allows.
    Pass absolute paths - the bridge runs as its own process and does not
    share Claude Code's cwd.
    """
    sections = []
    total_len = 0
    for path in file_paths:
        try:
            text = Path(path).read_text()
        except OSError as e:
            return f"Error reading '{path}': {e}"
        total_len += len(text)
        sections.append(f"--- {path} ---\n{text}")

    combined = _truncate("\n\n".join(sections), MAX_FILE_CONTEXT_CHARS)
    prompt = f"{question}\n\n{combined}"
    response = _call_gemini(prompt)
    _log({
        "tool": "ask_gemini_about_files",
        "file_paths": file_paths,
        "question": question,
        "total_len": total_len,
        "response": response,
    })
    return response


if __name__ == "__main__":
    mcp.run()
