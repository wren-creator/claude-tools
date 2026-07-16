import json
import os
import socket
import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gemini-bridge")

LOG_PATH = Path(__file__).parent / "log.jsonl"
GEMINI_ENV_FILE = Path.home() / ".gemini" / ".env"
GEMINI_TIMEOUT = 120
GIT_TIMEOUT = 30
MAX_CONTEXT_CHARS = 60_000  # guard against blowing past Gemini's context window
MAX_FILE_CONTEXT_CHARS = 500_000  # ask_gemini_about_files exists specifically to use Gemini's much larger window
NETWORK_CHECK_HOST = "generativelanguage.googleapis.com"
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


def _call_gemini(prompt: str) -> str:
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
    """
    try:
        diff = subprocess.run(
            ["git", "diff"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError:
        return f"Error: repo_path '{repo_path}' does not exist or `git` not found"

    if diff.returncode != 0:
        return f"Error running git diff: {diff.stderr.strip()}"
    if not diff.stdout.strip():
        return "No unstaged changes to review."

    prompt = f"{instructions}\n\n```diff\n{_truncate(diff.stdout)}\n```"
    response = _call_gemini(prompt)
    _log({
        "tool": "review_diff",
        "repo_path": repo_path,
        "instructions": instructions,
        "diff_len": len(diff.stdout),
        "response": response,
    })
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
