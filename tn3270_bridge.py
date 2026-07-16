import json
import re
import time
import uuid
from pathlib import Path

import py3270
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tn3270-bridge")

LOG_PATH = Path(__file__).parent / "tn3270_log.jsonl"
CONNECT_TIMEOUT = 15

_SF_RE = re.compile(r"SF\(([^)]*)\)")

# session_id -> py3270.Emulator. This process is long-lived (one per Claude
# Code session), so sessions persist across tool calls until disconnect() or
# the server itself is restarted.
SESSIONS: dict[str, py3270.Emulator] = {}


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _get_session(session_id: str) -> py3270.Emulator | None:
    return SESSIONS.get(session_id)


def _dump_screen(emulator: py3270.Emulator) -> str:
    # Ascii() with no args returns the full 24x80 (or model-specific) screen
    # buffer as plain text, one "data:" line per row.
    result = emulator.exec_command(b"Ascii()")
    return "\n".join(line.decode("ascii", errors="replace") for line in result.data)


def _decode_basic_attr(byte: int) -> dict:
    # Bit layout of the 3270 basic field-attribute byte (IBM GA23-0059):
    # bit5=protected, bit4=numeric/autoskip, bits3-2=display code
    # (00 normal, 01 normal+detectable, 10 intensified, 11 hidden), bit0=MDT.
    return {
        "protected": bool(byte & 0x20),
        "autoskip": bool(byte & 0x10),
        "intensified": ((byte >> 2) & 0x03) == 2,
        "hidden": ((byte >> 2) & 0x03) == 3,
        "modified": bool(byte & 0x01),
    }


def _structured_screen(emulator: py3270.Emulator) -> dict:
    # ReadBuffer(Ascii) returns the buffer annotated with SF(...) markers at
    # each field's attribute position (type "c0" is the basic attribute
    # byte, per the SFE order's attribute-type codes); everything between
    # one SF() and the next belongs to that field, and fields can span rows.
    result = emulator.exec_command(b"ReadBuffer(Ascii)")
    fields = []
    current = None

    for row_idx, raw_line in enumerate(result.data):
        col = 0
        for token in raw_line.decode("ascii", errors="replace").split():
            m = _SF_RE.fullmatch(token)
            if m:
                if current is not None:
                    fields.append(current)
                attr_types = dict(
                    pair.split("=", 1) for pair in m.group(1).split(",") if "=" in pair
                )
                byte = int(attr_types.get("c0", "0"), 16)
                current = {
                    "row": row_idx + 1,
                    "col": col + 1,
                    **_decode_basic_attr(byte),
                    "text": "",
                }
                col += 1
                continue

            code = int(token, 16)
            if current is None:
                # Data appearing before any SF() marker - an unformatted
                # screen, or wraparound from the last field. Treat as an
                # untyped field starting at 1,1 rather than dropping it.
                current = {
                    "row": 1, "col": 1, "protected": True, "autoskip": False,
                    "intensified": False, "hidden": False, "modified": False,
                    "text": "",
                }
            current["text"] += " " if code == 0 else chr(code)
            col += 1

    if current is not None:
        fields.append(current)
    for field in fields:
        field["text"] = field["text"].rstrip()

    emulator.exec_command(b"Query(Cursor)")
    status = emulator.status
    # Query(Cursor) reports 0-indexed row/col; +1 to match the 1-indexed
    # row/col used for fields above (which come from ReadBuffer(Ascii)).
    cursor = {"row": int(status.cursor_row) + 1, "col": int(status.cursor_col) + 1}
    return {"cursor": cursor, "fields": fields}


@mcp.tool()
def connect(host: str, port: int = 23) -> str:
    """Open a TN3270 session to a mainframe host and return a session_id.
    Pass that session_id to send_keys/read_screen/send_function_key/disconnect.
    """
    emulator = py3270.Emulator(visible=False, timeout=CONNECT_TIMEOUT)
    try:
        emulator.connect(f"{host}:{port}")
    except Exception as e:
        emulator.terminate()
        _log({"tool": "connect", "host": host, "port": port, "error": str(e)})
        return f"Error connecting to {host}:{port}: {e}"

    session_id = uuid.uuid4().hex
    SESSIONS[session_id] = emulator
    _log({"tool": "connect", "host": host, "port": port, "session_id": session_id})
    return session_id


@mcp.tool()
def send_keys(session_id: str, keys: str) -> str:
    """Type a string into the current field of an open session, then return
    the resulting screen. A "\\n" in keys presses Enter (e.g. "myuser\\n"
    types "myuser" and submits it) - use send_function_key for PF keys.
    """
    emulator = _get_session(session_id)
    if emulator is None:
        return f"Error: no active session with id {session_id}"

    try:
        for i, chunk in enumerate(keys.split("\n")):
            if i > 0:
                emulator.send_enter()
            if chunk:
                emulator.send_string(chunk)
    except Exception as e:
        _log({"tool": "send_keys", "session_id": session_id, "error": str(e)})
        return f"Error sending keys: {e}"

    screen = _dump_screen(emulator)
    _log({"tool": "send_keys", "session_id": session_id, "keys": keys, "screen": screen})
    return screen


@mcp.tool()
def read_screen(session_id: str, structured: bool = False) -> str:
    """Return the current screen contents of an open session. By default
    returns plain text. With structured=True, returns a JSON object
    {"cursor": {"row", "col"}, "fields": [{"row", "col", "protected",
    "hidden", "autoskip", "modified", "text"}, ...]} instead - use this when
    you need to know *where* to type (e.g. which field is unprotected, or
    which one is a hidden password field) rather than just what's on screen.
    """
    emulator = _get_session(session_id)
    if emulator is None:
        return f"Error: no active session with id {session_id}"

    try:
        screen = json.dumps(_structured_screen(emulator)) if structured else _dump_screen(emulator)
    except Exception as e:
        _log({"tool": "read_screen", "session_id": session_id, "error": str(e)})
        return f"Error reading screen: {e}"

    _log({"tool": "read_screen", "session_id": session_id, "structured": structured, "screen": screen})
    return screen


@mcp.tool()
def send_function_key(session_id: str, key: str) -> str:
    """Send a function/attention key (e.g. "PF3", "PF8", "PA1", "Clear",
    "Enter") to an open session and return the resulting screen.
    """
    emulator = _get_session(session_id)
    if emulator is None:
        return f"Error: no active session with id {session_id}"

    try:
        normalized = key.strip().upper()
        if normalized == "ENTER":
            emulator.send_enter()
        elif normalized.startswith("PF"):
            emulator.send_pf(int(normalized[2:]))
        elif normalized.startswith("PA"):
            emulator.exec_command(f"PA({normalized[2:]})".encode("ascii"))
        elif normalized == "CLEAR":
            emulator.exec_command(b"Clear()")
        else:
            return f"Error: unrecognized key '{key}' (expected PFn, PAn, Clear, or Enter)"
    except Exception as e:
        _log({"tool": "send_function_key", "session_id": session_id, "key": key, "error": str(e)})
        return f"Error sending key '{key}': {e}"

    screen = _dump_screen(emulator)
    _log({"tool": "send_function_key", "session_id": session_id, "key": key, "screen": screen})
    return screen


@mcp.tool()
def disconnect(session_id: str) -> str:
    """Close a TN3270 session and free its resources."""
    emulator = SESSIONS.pop(session_id, None)
    if emulator is None:
        return f"Error: no active session with id {session_id}"

    emulator.terminate()
    _log({"tool": "disconnect", "session_id": session_id})
    return f"Disconnected session {session_id}"


if __name__ == "__main__":
    mcp.run()
