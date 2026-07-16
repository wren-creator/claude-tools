import json
import time
import uuid
from pathlib import Path

import py3270
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tn3270-bridge")

LOG_PATH = Path(__file__).parent / "tn3270_log.jsonl"
CONNECT_TIMEOUT = 15

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
def read_screen(session_id: str) -> str:
    """Return the current screen contents (plain text) of an open session."""
    emulator = _get_session(session_id)
    if emulator is None:
        return f"Error: no active session with id {session_id}"

    try:
        screen = _dump_screen(emulator)
    except Exception as e:
        _log({"tool": "read_screen", "session_id": session_id, "error": str(e)})
        return f"Error reading screen: {e}"

    _log({"tool": "read_screen", "session_id": session_id, "screen": screen})
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
