"""bambu-bridge: send a sliced model straight to the Bambu Lab A1 mini over
the local network, in LAN-only mode - no Bambu cloud account involved.

Five tools:
- slice_model(model_path, ...) - headless slice via the OrcaSlicer CLI into
  a .gcode.3mf. model_path is normally an OrcaSlicer project (.3mf saved
  with the object on the plate and presets already picked in the GUI),
  which carries its own settings - no exported config files needed. A bare
  STL/STEP is also accepted but then needs settings_files/filament_files.
- send_and_print(file_path, ...) - FTPS-upload a sliced file to the printer
  and start the print.
- get_printer_status() - current state, progress, temps, current file,
  error code.
- control_print(action) - pause / resume / stop the active print.
- open_in_bambu_studio(file_path) - hand a model or sliced file to the
  Bambu Studio GUI instead, for manual adjustment before printing.

Printer credentials (IP, serial, access code - all from the printer's own
network settings screen with LAN-only mode on) live in ~/.bambu/.env, same
pattern as every other bridge's credential file:
    BAMBU_IP=192.168.1.50
    BAMBU_SERIAL=00M00A000000000
    BAMBU_ACCESS_CODE=12345678
    BAMBU_USE_AMS=false          # optional, default false (A1 mini has no AMS)
    ORCASLICER_BIN=/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer  # required: no CLI symlink on PATH
    BAMBU_STUDIO_APP=/Applications/BambuStudio.app  # optional override

NOTE: both halves of this bridge are now verified against real hardware
and a real install, not just documentation:
- get_printer_status: verified live against a real A1 mini on 2026-11-09
  (real MQTT handshake, real bed/nozzle temps, real IDLE state).
- slice_model: verified end-to-end the same day by slicing a real
  OrcaSlicer project (.3mf, object + Bambu A1 mini presets saved via the
  GUI) - produced real Bambu-flavored G-code (433 layers, ~1h18m estimated
  print time). OrcaSlicer's vendor presets ship packed inside a
  proprietary container (profiles/BBL.opc, magic bytes "ZCRO") that it
  only unpacks internally at runtime - no per-preset JSON export option
  actually worked in this build (Export Preset Bundle's picker errored
  with "select at least one printer or filament" regardless of selection),
  so the project-file path above is the one that's proven, not a
  workaround.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("bambu-bridge")

ENV_FILE = Path.home() / ".bambu" / ".env"
LOG_PATH = Path(__file__).parent / "bambu_log.jsonl"
MQTT_READY_TIMEOUT = 10
VALID_ACTIONS = {"pause", "resume", "stop"}

_PRINTER = None  # cached bambulabs_api.Printer, reused across tool calls


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _load_credentials() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def _get_printer():
    """Return the cached Printer, connecting (MQTT only, no camera - the A1
    mini has none) on first use."""
    global _PRINTER
    if _PRINTER is not None:
        return _PRINTER

    creds = _load_credentials()
    missing = [k for k in ("BAMBU_IP", "BAMBU_SERIAL", "BAMBU_ACCESS_CODE") if not creds.get(k)]
    if missing:
        raise RuntimeError(
            f"Missing {', '.join(missing)} in {ENV_FILE}. Find these on the printer: "
            f"Settings > Network (enable LAN Only Mode) for the IP and access code, "
            f"and the serial sticker / Settings > Device for the serial number."
        )

    import bambulabs_api as bl

    printer = bl.Printer(creds["BAMBU_IP"], creds["BAMBU_ACCESS_CODE"], creds["BAMBU_SERIAL"])
    printer.mqtt_start()  # skip camera_start(): A1 mini has no camera

    deadline = time.time() + MQTT_READY_TIMEOUT
    while time.time() < deadline and not printer.mqtt_client_ready():
        time.sleep(0.25)
    if not printer.mqtt_client_ready():
        printer.mqtt_stop()
        raise RuntimeError(
            f"Connected but MQTT never reported ready within {MQTT_READY_TIMEOUT}s - "
            f"check the printer is on, on the same network, and LAN Only Mode is on."
        )

    _PRINTER = printer
    return printer


def _use_ams_default() -> bool:
    return _load_credentials().get("BAMBU_USE_AMS", "false").strip().lower() in ("1", "true", "yes")


@mcp.tool()
def slice_model(
    model_path: str,
    plate: int = 1,
    output_path: str | None = None,
    settings_files: list[str] | None = None,
    filament_files: list[str] | None = None,
) -> str:
    """Headlessly slice a model into a .gcode.3mf via the OrcaSlicer CLI,
    ready for send_and_print().

    model_path: either an OrcaSlicer *project* file (.3mf saved via File >
    Save Project As, with a real object on the plate and the right
    printer/filament/process presets already picked in the GUI) - the
    normal, verified path, since the project already embeds both geometry
    and settings and needs nothing else - or a bare STL/STEP, in which case
    settings_files and filament_files become required (exported print+
    machine / filament config JSON, via OrcaSlicer's preset export tools).
    plate: which plate to slice (1 for a single-plate file). output_path:
    where to write the .gcode.3mf, defaults next to model_path.

    Requires OrcaSlicer installed; set ORCASLICER_BIN in ~/.bambu/.env to
    its binary's full path (no CLI symlink is installed on PATH by the
    Homebrew cask).
    """
    model = Path(model_path).expanduser().resolve()
    if not model.exists():
        return f"Error: {model} does not exist."
    if model.suffix.lower() != ".3mf" and not (settings_files and filament_files):
        return (
            "Error: a bare STL/STEP needs both settings_files and filament_files. "
            "Easier: save an OrcaSlicer project (.3mf) with the object on the plate "
            "and presets already picked, and pass that as model_path instead."
        )

    creds = _load_credentials()
    binary = creds.get("ORCASLICER_BIN") or shutil.which("orca-slicer") or shutil.which("OrcaSlicer")
    if not binary:
        return (
            "Error: no OrcaSlicer binary found on PATH as orca-slicer/OrcaSlicer. "
            "Set ORCASLICER_BIN in ~/.bambu/.env to its full path."
        )

    out = Path(output_path).expanduser().resolve() if output_path else model.with_suffix(".gcode.3mf")

    cmd = [binary, "--slice", str(plate)]
    if settings_files:
        cmd += ["--load-settings", ";".join(settings_files)]
    if filament_files:
        cmd += ["--load-filaments", ";".join(filament_files)]
    cmd += ["--export-3mf", str(out), "--", str(model)]

    _log({"tool": "slice_model", "cmd": cmd})
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return "Error: OrcaSlicer timed out after 300s."
    except FileNotFoundError:
        return f"Error: could not execute {binary}."

    if result.returncode != 0 or not out.exists():
        return (
            f"Slice failed (exit {result.returncode}).\n"
            f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"
        )
    return f"Sliced {model.name} -> {out}"


@mcp.tool()
def send_and_print(
    file_path: str,
    plate_number: int = 1,
    use_ams: bool | None = None,
    start: bool = True,
) -> str:
    """Upload a sliced .gcode.3mf (or .3mf) to the Bambu A1 mini over local
    FTPS and, unless start=False, start printing it. use_ams defaults to
    BAMBU_USE_AMS from ~/.bambu/.env (false, since the A1 mini has no AMS by
    default) - override only if yours has one attached.
    """
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return f"Error: {path} does not exist."

    try:
        printer = _get_printer()
        with path.open("rb") as f:
            uploaded = printer.upload_file(f, filename=path.name)
        if not start:
            return f"Uploaded to printer: {uploaded}"
        ok = printer.start_print(
            path.name, plate_number, use_ams=use_ams if use_ams is not None else _use_ams_default()
        )
        _log({"tool": "send_and_print", "file": str(path), "started": ok})
        return f"Uploaded and {'started print of' if ok else 'FAILED to start print of'} {path.name}"
    except Exception as e:
        _log({"tool": "send_and_print", "file": str(path), "error": str(e)})
        return f"Error: {e}"


@mcp.tool()
def get_printer_status() -> str:
    """Current printer state: idle/printing/paused/error, progress percent,
    remaining time, bed + nozzle temperature, current file name, and any
    HMS error code."""
    try:
        printer = _get_printer()
        return (
            f"state: {printer.get_current_state()}\n"
            f"file: {printer.get_file_name()}\n"
            f"progress: {printer.get_percentage()}%\n"
            f"time remaining: {printer.get_time()}s\n"
            f"bed temp: {printer.get_bed_temperature()}C\n"
            f"nozzle temp: {printer.get_nozzle_temperature()}C\n"
            f"error code: {printer.print_error_code()}"
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def control_print(action: str) -> str:
    """Pause, resume, or stop the print currently running on the printer.
    action must be one of: pause, resume, stop."""
    if action not in VALID_ACTIONS:
        return f"Error: action must be one of {sorted(VALID_ACTIONS)}, got {action!r}."
    try:
        printer = _get_printer()
        ok = {"pause": printer.pause_print, "resume": printer.resume_print, "stop": printer.stop_print}[action]()
        _log({"tool": "control_print", "action": action, "ok": ok})
        return f"{action} {'succeeded' if ok else 'FAILED'}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def open_in_bambu_studio(file_path: str) -> str:
    """Hand a model or already-sliced file to the Bambu Studio GUI instead
    of printing it directly, so you can make manual adjustments (orientation,
    supports, per-plate settings) before printing it yourself from there."""
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return f"Error: {path} does not exist."
    if sys.platform != "darwin":
        return "Error: open_in_bambu_studio only supports macOS (`open -a`) in this bridge."
    app = _load_credentials().get("BAMBU_STUDIO_APP", "/Applications/BambuStudio.app")
    # Opening by the app's registered display name ("Bambu Studio") is
    # unreliable right after a fresh install - Launch Services hasn't always
    # indexed it yet. The bundle path always works.
    result = subprocess.run(["open", "-a", app, str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        return f"Error opening Bambu Studio: {result.stderr.strip()}"
    return f"Opened {path.name} in Bambu Studio."


if __name__ == "__main__":
    mcp.run()
