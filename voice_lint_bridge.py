"""voice-lint-bridge: deterministic checks for Britley's writing rules, so the
agent doesn't spend tokens re-reading a draft against the same checklist
every time. No model involved, just regex.

Two tools:
- lint_text(text, kind) - check a draft passed inline.
- lint_file(path, kind) - same, for a .md/.txt file on disk.

It returns a short list of line-numbered findings (or "CLEAN"), so the agent
only rewrites the flagged lines instead of re-reading the whole draft.

Checks, from the global writing rules:
- A hyphen, en dash or em dash used as a spacing device (" - ", "—", " -- ")
  instead of a comma. Hyphens inside real compound words are fine.
- LinkedIn posts: must carry the Slack, GitHub, book and contact-email
  links, must not carry the retired consulting-site link, and must fit
  LinkedIn's 3,000 character limit.
- Corporate-speak and throat-clearing phrases (warnings, not errors).
- Commit messages: a feat/fix/docs/refactor prefix and the Co-Authored-By
  trailer.

Fenced code blocks and inline code are skipped, since a " - " in a shell
command or a YAML list is not prose.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("voice-lint-bridge")

LOG_PATH = Path(__file__).parent / "voice_lint_log.jsonl"
ALLOWED_EXTENSIONS = {".md", ".txt"}
MAX_BYTES = 200_000
LINKEDIN_LIMIT = 3000
KINDS = {"generic", "linkedin", "slack", "commit", "doc"}

# Required in every LinkedIn post. (label, pattern)
LINKEDIN_REQUIRED = [
    ("Slack community link (britleydev.slack.com)", re.compile(r"britleydev\.slack\.com", re.I)),
    ("GitHub repo link", re.compile(r"github\.com/", re.I)),
    ("book link (https://books.britleyhoffconsulting.com)", re.compile(r"books\.britleyhoffconsulting\.com", re.I)),
    ("contact email (britleyhoff@britleyhoffconsulting.com)", re.compile(r"britleyhoff@britleyhoffconsulting\.com", re.I)),
]
# The bare consulting site, but not the books. subdomain or the email address.
RETIRED_SITE = re.compile(r"(?<![\w.@])britleyhoffconsulting\.com", re.I)

# Dashes used as punctuation. A line-leading "- " bullet is not matched
# because the check requires a non-space character before the dash.
EM_DASH = re.compile("—")
EN_DASH = re.compile(r"\s–\s")  # a spaced en dash is a clause break; "10–20" ranges are left alone
SPACED_HYPHEN = re.compile(r"(?<=\S) {1,}-{1,2} {1,}(?=\S)")

CORPORATE = [
    "aims to address", "aim to address", "leverage", "synergy", "synergies",
    "delve", "game-changer", "game changer", "unlock the power", "paradigm",
    "in today's fast-paced", "i'm thrilled", "i am thrilled", "i'm excited to announce",
    "i am excited to announce", "i am pleased to announce", "i'm pleased to announce",
    "circle back", "move the needle", "best-in-class", "cutting-edge", "seamless",
    "robust solution", "holistic", "thought leader",
]
CORPORATE_RE = re.compile("|".join(re.escape(p) for p in CORPORATE), re.I)

COMMIT_PREFIX = re.compile(r"^(feat|fix|docs|refactor)(\([^)]*\))?:\s")
INLINE_CODE = re.compile(r"`[^`]*`")
URL = re.compile(r"https?://\S+")


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """(1-based line number, line with code, inline code and URLs blanked
    out), skipping fenced code blocks entirely. URLs are blanked because a
    hyphen in a link is never a spacing dash."""
    out: list[tuple[int, str]] = []
    fenced = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = INLINE_CODE.sub(lambda m: " " * len(m.group()), line)
        line = URL.sub(lambda m: "x" * len(m.group()), line)
        out.append((n, line))
    return out


def _snippet(line: str, start: int) -> str:
    lo = max(0, start - 20)
    return line[lo:start + 25].strip()


def _lint(text: str, kind: str) -> list[tuple[int, str, str]]:
    """Returns (line, severity, message). line 0 means whole-document."""
    findings: list[tuple[int, str, str]] = []
    lines = _prose_lines(text)

    for n, line in lines:
        for m in EM_DASH.finditer(line):
            findings.append((n, "ERROR", f"em dash, use a comma: ...{_snippet(line, m.start())}..."))
        for m in EN_DASH.finditer(line):
            findings.append((n, "ERROR", f"en dash, use a comma: ...{_snippet(line, m.start())}..."))
        for m in SPACED_HYPHEN.finditer(line):
            # Skip markdown table separators like "| --- | --- |" and bare rules.
            if re.fullmatch(r"[\s|:\-]+", line):
                continue
            findings.append((n, "ERROR", f"hyphen used as a spacing dash, use a comma: ...{_snippet(line, m.start())}..."))
        for m in CORPORATE_RE.finditer(line):
            findings.append((n, "WARN", f"corporate-speak: '{m.group()}'"))

    if kind == "linkedin":
        if len(text) > LINKEDIN_LIMIT:
            findings.append((0, "ERROR", f"{len(text)} chars, over LinkedIn's {LINKEDIN_LIMIT} limit by {len(text) - LINKEDIN_LIMIT}"))
        for label, pat in LINKEDIN_REQUIRED:
            if not pat.search(text):
                findings.append((0, "ERROR", f"missing {label}"))
        for n, raw in enumerate(text.splitlines(), 1):
            if RETIRED_SITE.search(raw):
                findings.append((n, "ERROR", "retired consulting-site link (britleyhoffconsulting.com), remove it"))

    if kind == "commit":
        subject = next((l for l in text.splitlines() if l.strip()), "")
        if not COMMIT_PREFIX.match(subject):
            findings.append((1, "ERROR", "subject needs a feat:/fix:/docs:/refactor: prefix"))
        if not re.search(r"^Co-Authored-By:", text, re.M | re.I):
            findings.append((0, "ERROR", "missing Co-Authored-By trailer"))

    findings.sort(key=lambda f: (f[0], f[1] != "ERROR"))
    return findings


def _format(findings: list[tuple[int, str, str]], kind: str, length: int) -> str:
    errors = sum(1 for f in findings if f[1] == "ERROR")
    warns = len(findings) - errors
    if not findings:
        return f"CLEAN: no findings ({kind}, {length} chars)."
    rows = [f"{errors} error(s), {warns} warning(s) ({kind}, {length} chars)"]
    for n, sev, msg in findings:
        where = f"L{n}" if n else "doc"
        rows.append(f"  {where:>4} {sev:<5} {msg}")
    return "\n".join(rows)


@mcp.tool()
def lint_text(text: str, kind: str = "generic") -> str:
    """Check a draft against Britley's writing rules, deterministically (no
    model, no tokens spent reading it back). Run this on any LinkedIn post,
    Slack update, README/doc prose or commit message before sending, and fix
    only the flagged lines.

    kind: 'generic' (default), 'linkedin' (also requires the Slack, GitHub,
    book and email links, bans the retired consulting site, enforces the
    3,000 char limit), 'slack', 'doc', or 'commit' (also requires a
    feat/fix/docs/refactor prefix and the Co-Authored-By trailer).

    Returns 'CLEAN: ...' or a list of line-numbered findings; ERROR means
    fix before sending, WARN is a judgment call. Code blocks and inline code
    are not checked.
    """
    if kind not in KINDS:
        return f"Error: kind must be one of {', '.join(sorted(KINDS))}."
    findings = _lint(text, kind)
    _log({"tool": "lint_text", "kind": kind, "chars": len(text),
          "errors": sum(1 for f in findings if f[1] == "ERROR"), "warnings": sum(1 for f in findings if f[1] == "WARN")})
    return _format(findings, kind, len(text))


@mcp.tool()
def lint_file(path: str, kind: str = "doc") -> str:
    """Same checks as lint_text, for a .md or .txt file on disk (absolute
    path, max 200KB). Use it for READMEs, docs and book chapters so the file
    never has to be read into the conversation just to be checked."""
    if kind not in KINDS:
        return f"Error: kind must be one of {', '.join(sorted(KINDS))}."
    p = Path(path).expanduser()
    if p.suffix.lower() not in ALLOWED_EXTENSIONS:
        return f"Error: only {', '.join(sorted(ALLOWED_EXTENSIONS))} files can be linted."
    try:
        if p.stat().st_size > MAX_BYTES:
            return f"Error: {p.name} is over the {MAX_BYTES} byte limit."
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return f"Error reading {path}: {e}"
    findings = _lint(text, kind)
    _log({"tool": "lint_file", "path": str(p), "kind": kind, "chars": len(text),
          "errors": sum(1 for f in findings if f[1] == "ERROR"), "warnings": sum(1 for f in findings if f[1] == "WARN")})
    return _format(findings, kind, len(text))


if __name__ == "__main__":
    mcp.run()
