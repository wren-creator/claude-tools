import json
import re
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
MAX_CHUNKS = 8  # bounds a huge diff to ~8 model calls instead of an unbounded wait
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

TRANSCRIPT_TRIAGE_INSTRUCTIONS = (
    "You are triaging a raw speech transcript from a video-editing pipeline "
    "for a coding agent that can't afford to read the whole thing closely. "
    "Flag candidate spots that likely need a cut: false starts/restarts (the "
    "same sentence or phrase said again shortly after), long filler-heavy or "
    "rambling stretches, and anything that reads like dead air or an aborted "
    "take. Reply in exactly this format:\n"
    "FLAGGED: <one-line summary>\n"
    "[MM:SS-MM:SS] <reason>\n"
    "[MM:SS-MM:SS] <reason>\n"
    "...\n"
    "If nothing stands out (reads like one clean continuous take), reply "
    "exactly 'CLEAN: no obvious restarts, filler, or dead air found.' and "
    "nothing else. Be terse - this points at trouble spots, it doesn't "
    "decide the actual cuts."
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


# Files whose diffs are noise to a reviewer: dependency lockfiles, minified or
# generated bundles, images/fonts/archives. Excluded via git pathspecs so they
# never eat the truncation budget (one logged diff was 1.5MB).
DIFF_EXCLUDES = [
    ":(exclude,glob)**/package-lock.json",
    ":(exclude,glob)**/yarn.lock",
    ":(exclude,glob)**/pnpm-lock.yaml",
    ":(exclude,glob)**/poetry.lock",
    ":(exclude,glob)**/Cargo.lock",
    ":(exclude,glob)**/go.sum",
    ":(exclude,glob)**/*.min.js",
    ":(exclude,glob)**/*.min.css",
    ":(exclude,glob)**/*.map",
    ":(exclude,glob)**/*.{png,jpg,jpeg,gif,ico,webp,pdf,epub,zip,gz,tar,woff,woff2,ttf,mp4,mov}",
]


def _split_by_file(diff: str) -> list[str]:
    parts = re.split(r"(?m)^(?=diff --git )", diff)
    return [p for p in parts if p.strip()]


def _split_oversized(file_diff: str, limit: int) -> list[str]:
    # A single file bigger than one chunk: split on hunk boundaries, repeating
    # the file header so each piece still says which file it belongs to.
    head, *hunks = re.split(r"(?m)^(?=@@ )", file_diff)
    pieces, cur = [], head
    for h in hunks:
        if len(cur) + len(h) > limit and cur != head:
            pieces.append(cur)
            cur = head
        cur += h
    pieces.append(cur)
    return [_truncate(p, limit) for p in pieces]  # a single giant hunk still gets capped


def _chunk_diff(diff: str, limit: int = MAX_CONTEXT_CHARS) -> list[str]:
    chunks, cur = [], ""
    for f in _split_by_file(diff):
        for piece in ([f] if len(f) <= limit else _split_oversized(f, limit)):
            if cur and len(cur) + len(piece) > limit:
                chunks.append(cur)
                cur = ""
            cur += piece
    if cur:
        chunks.append(cur)
    return chunks


def _verdict(response: str) -> str:
    # The 7B model occasionally answers in some other shape (a JSON blob, for
    # instance). Anything that isn't a clean CLEAN/FLAGGED/Error gets escalated
    # rather than trusted, so the caller's "only review if FLAGGED" rule fails safe.
    r = response.strip()
    if r.startswith(("CLEAN", "FLAGGED", "Error")):
        return r
    return f"FLAGGED: local model gave an unparseable reply, escalate.\n{r[:300]}"


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


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
    # --diff-filter=d drops deleted files: a deletion has no code left to review.
    return subprocess.run(
        ["git", "diff", "--diff-filter=d"] + args + ["--", "."] + DIFF_EXCLUDES,
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )


def _untracked_diff(repo_path: str) -> str:
    # `git diff` never shows untracked files, and the workflow reviews before
    # staging, so brand-new files were invisible to the prefilter. Render each
    # as an added-file diff. Skips binaries/oversized files and the same
    # noise paths DIFF_EXCLUDES drops.
    ls = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", "."] + DIFF_EXCLUDES,
        cwd=repo_path, capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    out = []
    for rel in filter(None, ls.stdout.split("\0")):
        f = Path(repo_path, rel)
        try:
            if not f.is_file() or f.stat().st_size > 200_000:
                continue
            text = f.read_text()  # UnicodeDecodeError means binary, skip it
        except (OSError, UnicodeDecodeError):
            continue
        body = "".join(f"+{line}\n" for line in text.splitlines())
        out.append(f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1 @@\n{body}")
    return "".join(out)


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
    Also reviews untracked (new, not yet git-added) files, skips lockfiles,
    binaries and deleted files, and splits a large diff per file instead of
    truncating it. Checks, in order: uncommitted changes vs HEAD (staged + unstaged
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
    diff_text = diff.stdout + _untracked_diff(repo_path)
    if not diff_text.strip():
        return "No changes to review (checked against HEAD, staged, unstaged, and untracked)."

    chunks = _chunk_diff(diff_text)
    skipped = max(0, len(chunks) - MAX_CHUNKS)
    chunks = chunks[:MAX_CHUNKS]
    flagged, errors = [], []
    for i, chunk in enumerate(chunks, 1):
        prompt = f"{PREFILTER_INSTRUCTIONS}\n\n```diff\n{chunk}\n```"
        r = _verdict(_call_ollama(prompt, model))
        if r.startswith("Error"):
            errors.append(r)
        elif r.startswith("FLAGGED"):
            flagged.append(r if len(chunks) == 1 else f"[part {i}/{len(chunks)}] {r}")

    if errors:
        response = errors[0]  # caller falls back to review_diff on any error
    elif flagged:
        # Lead with FLAGGED so the documented "starts with CLEAN/FLAGGED" holds
        # even for a multi-chunk diff whose first part was clean.
        response = "\n\n".join(flagged)
        if not response.startswith("FLAGGED"):
            response = "FLAGGED: see parts below.\n" + response
    else:
        response = "CLEAN: no issues found."
    if skipped and not errors:
        response = (
            f"FLAGGED: diff too large, {skipped} chunk(s) not reviewed locally, escalate.\n"
            + response
        )
    _log({
        "tool": "prefilter_diff",
        "repo_path": repo_path,
        "model": model,
        "diff_len": len(diff_text),
        "chunks": len(chunks),
        "skipped_chunks": skipped,
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


@mcp.tool()
def triage_transcript(repo_path: str, transcript_path: str, model: str = DEFAULT_MODEL) -> str:
    """Read a transcript JSON file written by youtube-bridge's
    transcribe_video (a list of {start, end, text} segments) and send it to
    a local Ollama model to flag likely restarts, filler-heavy stretches,
    and dead air, instead of the agent reading the whole transcript closely
    to spot them itself. Pass the absolute path of the video's project
    folder as repo_path, and transcript_path as either an absolute path or
    one relative to repo_path - same containment rule as triage_log,
    rejected if it escapes repo_path.

    Returns a list of flagged [MM:SS-MM:SS] spots with a one-line reason
    each, or 'CLEAN: ...' if nothing stands out. This does not decide cuts -
    use it to know where to look closely in transcribe_video's own output
    before building keep_segments for cut_video, not as a substitute for
    reading the flagged spots yourself. A 7B model can miss things or flag
    false positives in a long transcript, treat this as a first pass, and if
    it comes back CLEAN on a take you know had issues, fall back to reading
    the full transcript.
    """
    target = _resolve_in_repo(repo_path, transcript_path)
    if target is None:
        return f"Error: '{transcript_path}' escapes repo_path"

    try:
        raw = target.read_text(errors="replace")
    except OSError as e:
        return f"Error reading '{transcript_path}': {e}"

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        return f"Error: '{transcript_path}' is not valid JSON: {e}"

    if not entries:
        return f"'{transcript_path}' has no segments - nothing to triage."

    formatted = "\n".join(
        f"[{_fmt_ts(e['start'])} - {_fmt_ts(e['end'])}] {e['text']}" for e in entries
    )

    prompt = f"{TRANSCRIPT_TRIAGE_INSTRUCTIONS}\n\n```\n{_truncate(formatted)}\n```"
    response = _call_ollama(prompt, model)
    _log({
        "tool": "triage_transcript",
        "repo_path": repo_path,
        "transcript_path": transcript_path,
        "model": model,
        "segment_count": len(entries),
        "response": response,
    })
    return (
        f"{response}\n\n"
        f"(Triaged from {len(entries)} segments at '{transcript_path}' - "
        f"read the full transcript around any flagged timestamp before "
        f"deciding keep_segments.)"
    )


if __name__ == "__main__":
    mcp.run()
