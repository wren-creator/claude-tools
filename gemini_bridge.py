import json
import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gemini-bridge")

LOG_PATH = Path(__file__).parent / "log.jsonl"
GEMINI_TIMEOUT = 120
GIT_TIMEOUT = 30
MAX_CONTEXT_CHARS = 60_000  # guard against blowing past Gemini's context window


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _truncate(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated {len(text) - limit} chars ...]"


def _call_gemini(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["gemini", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=GEMINI_TIMEOUT,
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


if __name__ == "__main__":
    mcp.run()
