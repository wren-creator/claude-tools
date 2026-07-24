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
DEFAULT_MODEL = "qwen2.5-coder:7b"

PREFILTER_INSTRUCTIONS = (
    "You are a fast, local first-pass reviewer for a git diff. Flag only "
    "clear issues: bugs, security problems, or obvious simplification "
    "opportunities. If the diff looks clean, say so plainly - start your "
    "reply with 'CLEAN: no issues found.' If you found something, start "
    "with 'FLAGGED: <one-line reason>' then list the issues. Be terse - "
    "this is a cheap triage pass before a stronger model reviews the same "
    "diff, not the final word."
)


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _truncate(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated {len(text) - limit} chars ...]"


def _call_ollama(prompt: str, model: str) -> str:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            # Ollama defaults num_ctx to 2048 tokens regardless of the
            # model's real max - a diff near MAX_CONTEXT_CHARS would get
            # silently left-truncated, dropping PREFILTER_INSTRUCTIONS
            # entirely. 8192 comfortably covers MAX_CONTEXT_CHARS (~5k
            # tokens) plus the instructions, within every installed model's
            # own context_length.
            "num_ctx": 8192,
            "temperature": 0.0,  # deterministic CLEAN/FLAGGED triage
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


if __name__ == "__main__":
    mcp.run()
