import hashlib
import json
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("rag-bridge")

LOG_PATH = Path(__file__).parent / "rag_log.jsonl"
DATA_DIR = Path(__file__).parent / "rag_data"
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_TIMEOUT = 120
EMBED_MODEL = "nomic-embed-text"
EMBED_BATCH = 16
CHUNK_CHARS = 1500  # nomic-embed-text's window is ~2k tokens; code runs ~3 chars/token
CHUNK_OVERLAP_LINES = 3
MAX_FILE_BYTES = 200_000
MAX_FILES = 3000  # bounds one index_directory call; a bigger tree needs a narrower path
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target"}
# Never embed secrets: an index is plain text on disk and its hits come back
# through the bridge, so anything indexed can end up in a chat transcript.
SKIP_NAME_RE = re.compile(
    r"(^\.env|\.pem$|\.key$|\.p12$|\.pfx$|^id_(rsa|ed25519|ecdsa)|credentials|secrets?\.|\.sqlite3?$|\.db$)",
    re.IGNORECASE,
)


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _embed(texts: list[str], prefix: str) -> np.ndarray:
    """Embed via Ollama's /api/embed. nomic-embed-text wants a task prefix:
    'search_document: ' for stored chunks, 'search_query: ' for the question.
    Raises RuntimeError with a readable message on any failure."""
    payload = json.dumps({"model": EMBED_MODEL, "input": [prefix + t for t in texts]}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/embed", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Ollama HTTP {e.code}: {e.read().decode(errors='replace')[:300]} "
                           f"(is `{EMBED_MODEL}` pulled?)")
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise RuntimeError(f"Ollama timed out after {OLLAMA_TIMEOUT}s")
        raise RuntimeError(f"Ollama not reachable at {OLLAMA_HOST} - is `ollama serve` running?")
    except TimeoutError:
        raise RuntimeError(f"Ollama timed out after {OLLAMA_TIMEOUT}s")
    if "error" in body:
        raise RuntimeError(f"Ollama: {body['error']}")
    vecs = np.asarray(body["embeddings"], dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.where(norms == 0, 1, norms)  # unit length, so dot product == cosine


def _db_path(name: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) or name.startswith("."):
        return None
    return DATA_DIR / f"{name}.db"


def _open_db(path: Path) -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, sha TEXT)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, path TEXT, "
        "start_line INTEGER, end_line INTEGER, text TEXT, vec BLOB)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path)")
    return db


def _list_files(root: Path) -> list[Path]:
    """git's own view (tracked plus untracked-but-not-ignored) when root is a
    repo, so .gitignore is respected for free; plain walk otherwise."""
    r = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard"],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode == 0:
        paths = [root / p for p in r.stdout.splitlines() if p]
    else:
        paths = [p for p in root.rglob("*") if p.is_file()]
    return [
        p for p in paths
        if p.is_file()
        and not any(part in SKIP_DIRS for part in p.relative_to(root).parts)
        and not SKIP_NAME_RE.search(p.name)
    ]


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None  # binary
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _chunk(text: str) -> list[tuple[int, int, str]]:
    """Line-aligned chunks of about CHUNK_CHARS with a few lines of overlap,
    so a hit can be cited as file:start-end."""
    lines = text.splitlines()
    chunks, i = [], 0
    while i < len(lines):
        j, size = i, 0
        while j < len(lines) and (size + len(lines[j]) + 1 <= CHUNK_CHARS or j == i):
            size += len(lines[j]) + 1
            j += 1
        body = "\n".join(lines[i:j]).strip()
        if body:
            chunks.append((i + 1, j, body[:CHUNK_CHARS * 2]))
        if j >= len(lines):
            break
        i = max(j - CHUNK_OVERLAP_LINES, i + 1)
    return chunks


@mcp.tool()
def index_directory(path: str, name: str = "") -> str:
    """Build or refresh a local semantic-search index of a directory, using
    the local nomic-embed-text model via Ollama (nothing leaves the machine).

    path is an absolute directory. name is the index name (defaults to the
    directory's own name). Re-running is cheap: only files whose content
    changed are re-embedded, and files that disappeared are dropped. In a git
    repo it honors .gitignore; it always skips .env files, keys, databases,
    binaries, files over 200KB and trees over 3000 files. Then use
    semantic_search to query it. Returns a one-line summary or "Error: ...".
    """
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        return f"Error: not a directory: {root}"
    name = name or root.name
    dbp = _db_path(name)
    if dbp is None:
        return "Error: name must be 1-64 chars of letters, digits, '.', '_' or '-' and not start with '.'"
    files = _list_files(root)
    if len(files) > MAX_FILES:
        return f"Error: {len(files)} files under {root}, over the {MAX_FILES} limit. Index a narrower subdirectory."

    db = _open_db(dbp)
    try:
        known = dict(db.execute("SELECT path, sha FROM files"))
        seen, changed, skipped = set(), 0, 0
        for f in files:
            text = _read_text(f)
            if text is None:
                skipped += 1
                continue
            rel = str(f.relative_to(root))
            seen.add(rel)
            sha = hashlib.sha256(text.encode()).hexdigest()
            if known.get(rel) == sha:
                continue
            chunks = _chunk(text)
            vecs = [
                _embed([c[2] for c in chunks[k:k + EMBED_BATCH]], "search_document: ")
                for k in range(0, len(chunks), EMBED_BATCH)
            ]
            db.execute("DELETE FROM chunks WHERE path = ?", (rel,))
            if chunks:
                matrix = np.vstack(vecs)
                db.executemany(
                    "INSERT INTO chunks (path, start_line, end_line, text, vec) VALUES (?,?,?,?,?)",
                    [(rel, s, e, t, matrix[n].tobytes()) for n, (s, e, t) in enumerate(chunks)],
                )
            db.execute("INSERT OR REPLACE INTO files (path, sha) VALUES (?, ?)", (rel, sha))
            db.commit()  # per file, so an interrupted run keeps its progress
            changed += 1
        gone = [p for p in known if p not in seen]
        for p in gone:
            db.execute("DELETE FROM chunks WHERE path = ?", (p,))
            db.execute("DELETE FROM files WHERE path = ?", (p,))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('root', ?)", (str(root),))
        db.commit()
        total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    except RuntimeError as e:
        _log({"tool": "index_directory", "path": str(root), "error": str(e)})
        return f"Error: {e}"
    finally:
        db.close()

    _log({"tool": "index_directory", "path": str(root), "name": name,
          "changed": changed, "removed": len(gone), "skipped": skipped, "chunks": total})
    return (f"Index '{name}': {changed} files (re)embedded, {len(gone)} removed, "
            f"{skipped} skipped (binary/large), {total} chunks total.")


@mcp.tool()
def semantic_search(query: str, index: str, top_k: int = 5) -> str:
    """Search a local index (built by index_directory) by meaning rather than
    exact text. Good for 'where do we handle retries' or 'what does the
    prefilter do' when you don't know the right keyword; use repo-bridge's
    search_codebase when you do. Returns the top_k chunks (max 10) as
    path:start-end with a similarity score and the chunk text.
    """
    dbp = _db_path(index)
    if dbp is None or not dbp.exists():
        return f"Error: no index named '{index}'. Use list_indexes, or build one with index_directory."
    top_k = max(1, min(top_k, 10))
    db = sqlite3.connect(dbp)
    try:
        rows = db.execute("SELECT path, start_line, end_line, text, vec FROM chunks").fetchall()
    finally:
        db.close()
    if not rows:
        return f"Index '{index}' is empty."
    try:
        q = _embed([query], "search_query: ")[0]
    except RuntimeError as e:
        return f"Error: {e}"
    matrix = np.vstack([np.frombuffer(r[4], dtype=np.float32) for r in rows])
    scores = matrix @ q
    best = np.argsort(-scores)[:top_k]
    out = []
    for i in best:
        path, s, e, text, _ = rows[i]
        out.append(f"{path}:{s}-{e}  (score {scores[i]:.3f})\n{text}")
    _log({"tool": "semantic_search", "index": index, "query": query, "hits": len(out)})
    return "\n\n---\n\n".join(out)


@mcp.tool()
def list_indexes() -> str:
    """List the local semantic-search indexes: name, source directory, file
    and chunk counts."""
    if not DATA_DIR.exists() or not list(DATA_DIR.glob("*.db")):
        return "No indexes yet. Build one with index_directory."
    lines = []
    for p in sorted(DATA_DIR.glob("*.db")):
        db = sqlite3.connect(p)
        try:
            root = db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
            files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        finally:
            db.close()
        lines.append(f"{p.stem}: {files} files, {chunks} chunks, from {root[0] if root else '?'}")
    return "\n".join(lines)


@mcp.tool()
def delete_index(index: str) -> str:
    """Delete one local semantic-search index (the embeddings only, never the
    source files it was built from)."""
    dbp = _db_path(index)
    if dbp is None or not dbp.exists():
        return f"Error: no index named '{index}'."
    dbp.unlink()
    _log({"tool": "delete_index", "index": index})
    return f"Deleted index '{index}'."


if __name__ == "__main__":
    mcp.run()
