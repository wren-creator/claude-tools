"""docs-pdf-bridge: render a Markdown doc to a styled client-facing PDF.

Replaces docs/pdf-build/build_system_design_pdf.py in the stock-alarm-service
repo. That script re-encoded SYSTEM_DESIGN.md as ~450 lines of fpdf calls, so
every prose change had to be made twice and the two copies kept drifting.
This bridge renders the Markdown file directly, so the .md is the only
source of truth.

Note: build_database_answers_pdf.py is left alone on purpose. It is a
standalone Q&A financial-review deliverable with no Markdown source, not a
render of docs/DATABASE.md.

Supported Markdown: ATX headings (#..####), paragraphs, bulleted and
numbered lists (one level of nesting), fenced code blocks (```), ---
horizontal rules, > blockquotes (rendered as an italic aside), and simple
pipe tables. Inline **bold** / *italic* / `code` markers are stripped to
plain text, matching what the old hand-written script produced; [text](url)
keeps the text (and the URL for real http links).
"""

import datetime as _dt
import json
import re
import time
from pathlib import Path

from fpdf import FPDF
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("docs-pdf-bridge")

LOG_PATH = Path(__file__).parent / "log.jsonl"

# Repo + default targets. The old scripts hardcoded these same paths.
REPO = Path("/Users/britleywrenhoff/git/stock-alarm-service")
DOCUMENTS = Path.home() / "Documents"

DESIGN_SOURCE = REPO / "SYSTEM_DESIGN.md"
DESIGN_OUTPUT = DOCUMENTS / "Alertis System Design.pdf"
DESIGN_SUBTITLE = "System Design Document"

BLUE = (30, 60, 110)
GRAY = (90, 90, 90)
BLACK = (20, 20, 20)
LIGHT_BG = (245, 246, 248)
TEXT = (40, 40, 40)

W = 171  # content width in mm (Letter, 20mm margins)
PAGE_BOTTOM = 277 - 20

_UNICODE_MAP = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", "•": "-",
    " ": " ", "→": "->", "←": "<-", "≥": ">=",
    "≤": "<=", "×": "x", "−": "-", "‑": "-",
}


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    entry["tool"] = "docs_pdf_bridge." + entry.get("tool", "?")
    try:
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _ascii(text: str) -> str:
    for bad, good in _UNICODE_MAP.items():
        text = text.replace(bad, good)
    # fpdf core fonts are latin-1 only; drop anything still out of range
    return text.encode("latin-1", "replace").decode("latin-1")


def _strip_marks(text: str) -> str:
    """Flatten inline Markdown to plain latin-1 text.

    The old hand-written script rendered every run as plain body text, so
    dropping **bold** / *italic* / `code` here keeps the PDF visually the
    same. A [label](target) link becomes "label", or "label (url)" when the
    target is a real http(s) URL.
    """
    text = _ascii(text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", text
    )
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?<!\*)\*(?!\*)", "", text)
    return text.strip()


# --------------------------- Markdown -> blocks ---------------------------

_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")


def _parse(md: str) -> list:
    lines = md.replace("\r\n", "\n").split("\n")
    blocks: list = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            buf = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence
            blocks.append(("code", "\n".join(buf), lang))
            continue

        if not line.strip():
            i += 1
            continue

        if _HR.match(line):
            blocks.append(("hr",))
            i += 1
            continue

        m = _ATX.match(line)
        if m:
            blocks.append(("h%d" % len(m.group(1)), m.group(2)))
            i += 1
            continue

        if line.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].lstrip())
                i += 1
            blocks.append(("note", " ".join(b for b in buf if b)))
            continue

        if _BULLET.match(line) or _ORDERED.match(line):
            items = []
            while i < n and (_BULLET.match(lines[i]) or _ORDERED.match(lines[i])):
                bm = _BULLET.match(lines[i]) or _ORDERED.match(lines[i])
                indent = len(bm.group(1).replace("\t", "    "))
                ordered = bool(_ORDERED.match(lines[i]))
                text = bm.group(2)
                i += 1
                # gather wrapped continuation lines
                while (
                    i < n
                    and lines[i].strip()
                    and not _BULLET.match(lines[i])
                    and not _ORDERED.match(lines[i])
                    and not _ATX.match(lines[i])
                    and not lines[i].strip().startswith("```")
                ):
                    text += " " + lines[i].strip()
                    i += 1
                items.append((1 if indent >= 2 else 0, ordered, text))
            blocks.append(("list", items))
            continue

        if line.lstrip().startswith("|") and "|" in line[1:]:
            rows = []
            while i < n and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not re.match(r"^[\s:|-]+$", lines[i].strip().strip("|")):
                    rows.append(cells)
                i += 1
            blocks.append(("table", rows))
            continue

        # paragraph: gather until blank / new block
        buf = [line.strip()]
        i += 1
        while (
            i < n
            and lines[i].strip()
            and not _ATX.match(lines[i])
            and not _HR.match(lines[i])
            and not _BULLET.match(lines[i])
            and not _ORDERED.match(lines[i])
            and not lines[i].strip().startswith("```")
            and not lines[i].lstrip().startswith(">")
            and not lines[i].lstrip().startswith("|")
        ):
            buf.append(lines[i].strip())
            i += 1
        blocks.append(("p", " ".join(buf)))
    return blocks


# --------------------------- blocks -> PDF ---------------------------


def _render(pdf: FPDF, blocks: list, skip_first_h1: bool) -> None:
    first_h1_seen = not skip_first_h1
    for block in blocks:
        kind = block[0]

        if kind == "h1":
            if not first_h1_seen:
                first_h1_seen = True
                continue
            pdf.ln(3)
            pdf.set_x(pdf.l_margin)
            pdf.set_font("Helvetica", "B", 18)
            pdf.set_text_color(*BLUE)
            pdf.multi_cell(W, 9, _strip_marks(block[1]))
            pdf.ln(1)

        elif kind == "h2":
            if pdf.get_y() > PAGE_BOTTOM - 26:
                pdf.add_page()
            pdf.ln(4)
            pdf.set_x(pdf.l_margin)
            pdf.set_font("Helvetica", "B", 14)
            pdf.set_text_color(*BLUE)
            pdf.multi_cell(W, 8, _strip_marks(block[1]))
            pdf.set_draw_color(*BLUE)
            pdf.set_line_width(0.4)
            y = pdf.get_y()
            pdf.line(20, y, 191, y)
            pdf.ln(3)

        elif kind in ("h3", "h4", "h5", "h6"):
            if pdf.get_y() > PAGE_BOTTOM - 20:
                pdf.add_page()
            pdf.ln(2)
            pdf.set_x(pdf.l_margin)
            pdf.set_font("Helvetica", "B", 11.5)
            pdf.set_text_color(*BLACK)
            pdf.multi_cell(W, 6.5, _strip_marks(block[1]))
            pdf.ln(0.5)

        elif kind == "p":
            pdf.set_x(pdf.l_margin)
            pdf.set_font("Helvetica", "", 10.3)
            pdf.set_text_color(*TEXT)
            pdf.multi_cell(W, 5.5, _strip_marks(block[1]))
            pdf.ln(1.5)

        elif kind == "note":
            pdf.set_x(pdf.l_margin)
            pdf.set_font("Helvetica", "I", 9.5)
            pdf.set_text_color(*GRAY)
            pdf.multi_cell(W, 5, _strip_marks(block[1]))
            pdf.ln(2)

        elif kind == "list":
            pdf.set_font("Helvetica", "", 10.3)
            pdf.set_text_color(*TEXT)
            counters = {0: 0, 1: 0}
            for level, ordered, text in block[1]:
                counters[level] += 1
                if level == 0:
                    counters[1] = 0
                marker = ("%d. " % counters[level]) if ordered else "-  "
                pdf.set_x(pdf.l_margin + (8 if level else 0))
                pdf.multi_cell(
                    W - (8 if level else 0),
                    5.5,
                    marker + _strip_marks(text),
                )
            pdf.ln(1.5)

        elif kind == "code":
            _code_block(pdf, block[1])

        elif kind == "table":
            _table(pdf, block[1])

        elif kind == "hr":
            pdf.ln(1)
            pdf.set_draw_color(210, 210, 210)
            pdf.set_line_width(0.2)
            y = pdf.get_y()
            pdf.line(20, y, 191, y)
            pdf.ln(3)


def _code_block(pdf: FPDF, text: str) -> None:
    raw = _ascii(text).strip("\n").split("\n") or [""]
    longest = max((len(ln) for ln in raw), default=0)
    size = 8.3
    while size > 6.0 and longest * size * 0.60 / 2.83 > W - 6:
        size -= 0.3
    line_h = size * 0.52
    block_h = line_h * len(raw) + 4
    if pdf.get_y() + block_h > PAGE_BOTTOM:
        pdf.add_page()
    x, y = pdf.get_x(), pdf.get_y()
    pdf.set_fill_color(*LIGHT_BG)
    pdf.rect(x, y, W, block_h, style="F")
    pdf.set_font("Courier", "", size)
    pdf.set_text_color(20, 20, 20)
    for idx, ln in enumerate(raw):
        pdf.set_xy(x + 3, y + 2 + idx * line_h)
        pdf.cell(W - 6, line_h, ln)
    pdf.set_xy(x, y + block_h + 3)


def _table(pdf: FPDF, rows: list) -> None:
    if not rows:
        return
    cols = max(len(r) for r in rows)
    col_w = W / cols
    for r_idx, row in enumerate(rows):
        row = (row + [""] * cols)[:cols]
        header = r_idx == 0
        pdf.set_font("Helvetica", "B" if header else "", 9.5)
        pdf.set_text_color(*(BLUE if header else TEXT))
        line_h = 5.2
        y0 = pdf.get_y()
        if y0 + line_h > PAGE_BOTTOM:
            pdf.add_page()
            y0 = pdf.get_y()
        x0 = pdf.l_margin
        for c in row:
            pdf.set_xy(x0, y0)
            pdf.multi_cell(col_w, line_h, _strip_marks(c), border=0)
            x0 += col_w
        pdf.set_xy(pdf.l_margin, y0 + line_h)
        if header:
            pdf.set_draw_color(*BLUE)
            pdf.set_line_width(0.3)
            pdf.line(20, pdf.get_y(), 191, pdf.get_y())
            pdf.ln(1)
    pdf.ln(2)


def _build(source: Path, output: Path, subtitle: str, author: str) -> dict:
    md = source.read_text(encoding="utf-8")
    blocks = _parse(md)

    title = None
    for b in blocks:
        if b[0] == "h1":
            title = _strip_marks(b[1])
            break
    if not title:
        title = source.stem
    # "Alertis: System Design Document" -> big title "Alertis",
    # tail becomes the cover subtitle when the caller didn't pass one
    if ": " in title:
        head, _, tail = title.partition(": ")
        title = head
        if not subtitle:
            subtitle = tail

    # drop a leading "Prepared by ..." paragraph; the cover reprints it
    trimmed = []
    dropped_prepared = False
    for b in blocks:
        if (
            not dropped_prepared
            and b[0] == "p"
            and b[1].strip().lower().startswith("prepared by")
        ):
            dropped_prepared = True
            continue
        trimmed.append(b)
    blocks = trimmed

    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_margins(20, 18, 20)
    pdf.add_page()

    # ---- cover header ----
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*BLUE)
    pdf.multi_cell(W, 10, _ascii(title))
    pdf.ln(1)
    if subtitle:
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", "", 13)
        pdf.set_text_color(*GRAY)
        pdf.multi_cell(W, 7, _ascii(subtitle))
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*GRAY)
    pdf.multi_cell(
        W, 5.5,
        "Prepared by %s  |  %s"
        % (_ascii(author), _dt.date.today().strftime("%B %d, %Y")),
    )
    pdf.ln(2)
    pdf.set_draw_color(*BLUE)
    pdf.set_line_width(0.4)
    pdf.line(20, pdf.get_y(), 191, pdf.get_y())
    pdf.ln(4)

    _render(pdf, blocks, skip_first_h1=True)

    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output))
    return {
        "output": str(output),
        "bytes": output.stat().st_size,
        "pages": pdf.page_no(),
        "blocks": len(blocks),
    }


# --------------------------- MCP tools ---------------------------


@mcp.tool()
def build_pdf(
    source: str,
    output: str,
    subtitle: str = "",
    author: str = "Britley Hoff",
) -> str:
    """Render a Markdown file to a styled, client-facing PDF.

    source and output are absolute paths; output should end in .pdf. The
    first level-1 heading (# ...) becomes the cover title and is not
    repeated in the body. subtitle is the gray line under the title on the
    cover. Returns a one-line summary or an "Error: ..." string.

    Handles headings, paragraphs, bulleted/numbered lists with one nesting
    level, fenced code blocks, horizontal rules, blockquotes, and simple
    pipe tables. Inline bold/italic/code markers are flattened to plain
    text (as the old hand-written script did).
    """
    src = Path(source).expanduser()
    out = Path(output).expanduser()
    if not src.exists():
        return f"Error: source not found: {src}"
    try:
        info = _build(src, out, subtitle, author)
    except Exception as e:  # noqa: BLE001
        _log({"tool": "build_pdf", "source": str(src), "error": str(e)})
        return f"Error rendering {src.name}: {e}"
    _log({"tool": "build_pdf", "source": str(src), **info})
    return (
        f"Wrote {info['output']} ({info['pages']} pages, "
        f"{info['bytes']} bytes) from {src.name}"
    )


@mcp.tool()
def build_alertis_design_pdf() -> str:
    """Rebuild the Alertis System Design PDF from SYSTEM_DESIGN.md.

    Renders /Users/britleywrenhoff/git/stock-alarm-service/SYSTEM_DESIGN.md
    to "~/Documents/Alertis System Design.pdf". This replaces running
    docs/pdf-build/build_system_design_pdf.py by hand. Run it after editing
    SYSTEM_DESIGN.md, in the same commit, the way README and ROADMAP get
    updated. Returns a one-line summary or an "Error: ..." string.
    """
    if not DESIGN_SOURCE.exists():
        return f"Error: source not found: {DESIGN_SOURCE}"
    try:
        info = _build(DESIGN_SOURCE, DESIGN_OUTPUT, DESIGN_SUBTITLE, "Britley Hoff")
    except Exception as e:  # noqa: BLE001
        _log({"tool": "build_alertis_design_pdf", "error": str(e)})
        return f"Error rendering SYSTEM_DESIGN.md: {e}"
    _log({"tool": "build_alertis_design_pdf", **info})
    return (
        f"Wrote {info['output']} ({info['pages']} pages, "
        f"{info['bytes']} bytes) from SYSTEM_DESIGN.md"
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        print(build_alertis_design_pdf())
    else:
        mcp.run()
