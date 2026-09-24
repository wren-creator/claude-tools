"""Shared diff collection for ollama-bridge and gemini-bridge, so both review
the same thing: tracked changes plus untracked new files, minus noise."""
import subprocess
from pathlib import Path

GIT_TIMEOUT = 30
MAX_UNTRACKED_BYTES = 200_000

# Files whose diffs are noise to a reviewer: dependency lockfiles, minified
# or generated bundles, images/fonts/archives. Excluded via git pathspecs so
# they never eat the truncation budget (one logged diff was 1.5MB).
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


def git_diff(repo_path: str, args: list[str]) -> subprocess.CompletedProcess:
    # --diff-filter=d drops deleted files: a deletion has no code left to review.
    return subprocess.run(
        ["git", "diff", "--diff-filter=d"] + args + ["--", "."] + DIFF_EXCLUDES,
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )


def untracked_diff(repo_path: str) -> str:
    # `git diff` never shows untracked files, and the workflow reviews before
    # staging, so brand-new files were invisible to review. Render each as an
    # added-file diff. Skips binaries and oversized files.
    ls = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", "."] + DIFF_EXCLUDES,
        cwd=repo_path, capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    out = []
    for rel in filter(None, ls.stdout.split("\0")):
        f = Path(repo_path, rel)
        try:
            if not f.is_file() or f.stat().st_size > MAX_UNTRACKED_BYTES:
                continue
            text = f.read_text()  # UnicodeDecodeError means binary, skip it
        except (OSError, UnicodeDecodeError):
            continue
        body = "".join(f"+{line}\n" for line in text.splitlines())
        out.append(
            f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n"
            f"--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1 @@\n{body}"
        )
    return "".join(out)


def collect_diff(repo_path: str) -> tuple[str, str | None]:
    """Return (diff_text, error). Checks uncommitted changes vs HEAD (staged +
    unstaged together), then staged-only (works in a repo with zero commits),
    then plain unstaged, and appends untracked files. diff_text is empty when
    there is nothing to review."""
    try:
        diff = git_diff(repo_path, ["HEAD"])
        if diff.returncode != 0:
            diff = git_diff(repo_path, ["--cached"])
        if diff.returncode == 0 and not diff.stdout.strip():
            diff = git_diff(repo_path, [])
    except FileNotFoundError:
        return "", f"Error: repo_path '{repo_path}' does not exist or `git` not found"
    if diff.returncode != 0:
        return "", f"Error running git diff: {diff.stderr.strip()}"
    return diff.stdout + untracked_diff(repo_path), None
