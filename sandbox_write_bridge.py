import json
import re
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("sandbox-write-bridge")

LOG_PATH = Path(__file__).parent / "sandbox_write_log.jsonl"
OUTPUT_DIR = Path.home() / "ollama-output"
MAX_BYTES = 200_000
ALLOWED_EXTENSIONS = {".md", ".txt", ".csv", ".json"}  # text the chat writes; PDFs come from docs-pdf-bridge
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _target(filename: str) -> tuple[Path | None, str]:
    """Flat, allow-listed names only. A bare filename can't carry '..' or a
    slash, so there is nothing to resolve out of the sandbox; the symlink
    check covers a link someone planted in the folder."""
    if not NAME_RE.fullmatch(filename):
        return None, ("Error: filename must be a bare name (letters, digits, '.', '_', '-'; "
                      "no slashes, no leading dot), up to 100 chars.")
    if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        return None, f"Error: extension must be one of {', '.join(sorted(ALLOWED_EXTENSIONS))}."
    path = OUTPUT_DIR / filename
    if path.is_symlink():
        return None, "Error: refusing to write through a symlink."
    return path, ""


@mcp.tool()
def write_file(filename: str, content: str, overwrite: bool = False) -> str:
    """Write a text file into the sandbox output folder (~/ollama-output).
    This is the only place this tool can write; it can't touch the
    repo or anything else on disk.

    filename is a bare name ending in .md, .txt, .csv or .json, with no
    folders (e.g. 'report.md'). content is the full text. An existing file is
    only replaced when overwrite=True. Max 200KB. Returns the file's absolute
    path, which is what docs-pdf-bridge's build_pdf needs as its `source`
    (write the PDF into the same folder, e.g. ~/ollama-output/report.pdf).
    """
    path, err = _target(filename)
    if path is None:
        return err
    data = content.encode("utf-8")
    if len(data) > MAX_BYTES:
        return f"Error: content is {len(data)} bytes, over the {MAX_BYTES} byte limit."
    OUTPUT_DIR.mkdir(exist_ok=True)
    existed = path.exists()
    if existed and not overwrite:
        return f"Error: {path} already exists. Pass overwrite=True to replace it, or pick another name."
    path.write_bytes(data)
    _log({"tool": "write_file", "path": str(path), "bytes": len(data), "overwrote": existed})
    return f"Wrote {path} ({len(data)} bytes){' (replaced existing file)' if existed else ''}"


@mcp.tool()
def list_output_files() -> str:
    """List what is in the sandbox output folder (~/ollama-output): name,
    size and age. Use it to see what has been written, or to avoid a name
    clash before write_file."""
    if not OUTPUT_DIR.exists():
        return "The output folder is empty (nothing written yet)."
    now = time.time()
    rows = []
    for p in sorted(OUTPUT_DIR.iterdir()):
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            rows.append(f"{p.name}  {st.st_size} bytes  {int((now - st.st_mtime) // 60)} min ago")
    return "\n".join(rows) or "The output folder is empty (nothing written yet)."


if __name__ == "__main__":
    mcp.run()
