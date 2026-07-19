import json
import mimetypes
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("youtube-bridge")

ENV_FILE = Path.home() / ".youtube" / ".env"
LOG_PATH = Path(__file__).parent / "youtube_log.jsonl"
STATE_PATH = Path(__file__).parent / "youtube_schedule_state.json"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
HTTP_TIMEOUT = 30
UPLOAD_TIMEOUT = 1800
DEFAULT_CATEGORY_ID = "28"  # Science & Technology
VALID_PRIVACY = {"private", "unlisted", "public"}

# auto-editor is only installed inside this project's venv, not on the
# MCP server's inherited PATH - resolve it explicitly rather than relying
# on PATH to include .venv/bin.
AUTO_EDITOR_BIN = shutil.which("auto-editor") or str(Path(sys.executable).parent / "auto-editor")

# Standard rate for tighten_video's output timeline (see comment at its
# call site) - 60fps matches what macOS screen recordings target and is
# natively supported by YouTube.
TIGHTEN_OUTPUT_FPS = 60

# Unicode space variants that look identical to a normal space but break
# exact-match filename lookups (e.g. macOS Screenshot/Screen Recording
# filenames use U+202F narrow no-break space before AM/PM).
_SPACE_VARIANTS = (" ", " ", " ", " ")


def _normalize_spaces(name: str) -> str:
    for ch in _SPACE_VARIANTS:
        name = name.replace(ch, " ")
    return name


def _resolve_video_path(video_path: str) -> Path | None:
    """Resolve a video path, tolerating unicode space variants that get
    silently flattened to a regular space when a path is retyped instead
    of copied from a directory listing.
    """
    path = Path(video_path)
    if path.exists():
        return path

    parent = path.parent
    if not parent.exists():
        return None
    target = _normalize_spaces(path.name)
    for candidate in parent.iterdir():
        if candidate.is_file() and _normalize_spaces(candidate.name) == target:
            return candidate
    return None


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def _refresh_access_token(env: dict) -> str | None:
    client_id = env.get("YOUTUBE_CLIENT_ID")
    client_secret = env.get("YOUTUBE_CLIENT_SECRET")
    refresh_token = env.get("YOUTUBE_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        return None

    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())["access_token"]


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            cmd, 127, stdout="",
            stderr=f"'{cmd[0]}' not found - is it installed and on PATH?",
        )


def _ffprobe_duration(path: str) -> float:
    result = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ])
    try:
        return float(result.stdout.strip())
    except ValueError:
        return -1.0


@mcp.tool()
def transcribe_video(video_path: str) -> str:
    """Transcribe a video's audio with word-level timestamps using local
    faster-whisper. Writes a JSON transcript to '<video_path>.transcript.json'
    (list of {start, end, text} segments) and returns the transcript as
    readable "[MM:SS - MM:SS] text" lines for reasoning about cuts.

    Use this first in the editing pipeline. After reading the returned
    transcript, decide which segments to KEEP (cut dead air, restarts,
    mistakes, rambling) and pass those as `keep_segments` to cut_video -
    this tool only transcribes, it does not decide or execute cuts.
    """
    path = _resolve_video_path(video_path)
    if path is None:
        return f"Error: {video_path} does not exist"

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return "Error: faster-whisper not installed. Run: .venv/bin/pip install faster-whisper"

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(path), word_timestamps=False)

    lines = []
    transcript = []
    for seg in segments:
        entry = {"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()}
        transcript.append(entry)
        lines.append(f"[{_fmt_ts(seg.start)} - {_fmt_ts(seg.end)}] {entry['text']}")

    transcript_path = path.with_suffix(path.suffix + ".transcript.json")
    transcript_path.write_text(json.dumps(transcript, indent=2))

    _log({"tool": "transcribe_video", "video_path": video_path, "segments": len(transcript)})
    return "\n".join(lines)


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


@mcp.tool()
def tighten_video(video_path: str, output_path: str = "") -> str:
    """Run auto-editor over a video to mechanically tighten it - cuts silence
    and long dead air. Use this as a first pass before any semantic cuts from
    cut_video. Does NOT understand meaning (won't cut a restarted sentence or
    a mistake), only audio/motion silence.

    Returns the output path and the before/after duration.
    """
    path = _resolve_video_path(video_path)
    if path is None:
        return f"Error: {video_path} does not exist"

    out = Path(output_path) if output_path else path.with_name(f"{path.stem}.tightened{path.suffix}")
    before = _ffprobe_duration(str(path))

    # macOS screen recordings are variable-frame-rate; left to its default,
    # auto-editor times the output timeline off the source's *average* fps,
    # which lands on an arbitrary non-standard rate (e.g. 52.41) that judders
    # on playback. Pin the timeline to a standard rate instead.
    result = _run(
        [AUTO_EDITOR_BIN, str(path), "-o", str(out), "--no-open", "--frame-rate", str(TIGHTEN_OUTPUT_FPS)],
        timeout=1800,
    )
    if result.returncode != 0:
        _log({"tool": "tighten_video", "video_path": video_path, "error": result.stderr[-2000:]})
        return f"Error running auto-editor: {result.stderr[-2000:]}"

    after = _ffprobe_duration(str(out))
    _log({"tool": "tighten_video", "video_path": video_path, "output_path": str(out), "before_s": before, "after_s": after})
    return f"Tightened: {str(out)}\nDuration {before:.0f}s -> {after:.0f}s"


@mcp.tool()
def cut_video(video_path: str, keep_segments: list[list[float]], output_path: str = "") -> str:
    """Cut a video down to only the given [start, end] second ranges (from
    transcribe_video's output) and concatenate what's kept, re-encoding at
    each cut for frame-accurate boundaries. `keep_segments` must be sorted,
    non-overlapping, and in seconds, e.g. [[0, 12.4], [18.0, 45.2]].

    This executes cuts you (Claude) have already decided on by reading the
    transcript - pick semantic boundaries (skip restarts, mistakes, dead
    rambling), don't try to do frame-level trimming by hand; this tool does
    the actual mechanical execution via ffmpeg.
    """
    path = _resolve_video_path(video_path)
    if path is None:
        return f"Error: {video_path} does not exist"
    if not keep_segments:
        return "Error: keep_segments is empty"

    last_end = -1.0
    for i, seg in enumerate(keep_segments):
        if len(seg) != 2:
            return f"Error: segment {i} must be [start, end], got {seg!r}"
        start, end = seg
        if end <= start:
            return f"Error: segment {i} has end ({end}) <= start ({start})"
        if start < last_end:
            return f"Error: segment {i} starts at {start}, before segment {i - 1} ended at {last_end} - segments must be sorted and non-overlapping"
        last_end = end

    out = Path(output_path) if output_path else path.with_name(f"{path.stem}.cut{path.suffix}")

    filter_parts = []
    concat_inputs = []
    for i, (start, end) in enumerate(keep_segments):
        filter_parts.append(f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]")
        filter_parts.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]")
        concat_inputs.append(f"[v{i}][a{i}]")
    filter_complex = ";".join(filter_parts) + ";" + "".join(concat_inputs) + \
        f"concat=n={len(keep_segments)}:v=1:a=1[outv][outa]"

    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning", "-i", str(path),
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac",
        str(out),
    ]
    result = _run(cmd, timeout=1800)
    if result.returncode != 0:
        _log({"tool": "cut_video", "video_path": video_path, "keep_segments": keep_segments, "error": result.stderr[-2000:]})
        return f"Error running ffmpeg: {result.stderr[-2000:]}"

    after = _ffprobe_duration(str(out))
    _log({"tool": "cut_video", "video_path": video_path, "output_path": str(out), "keep_segments": keep_segments, "after_s": after})
    return f"Cut video written: {str(out)}\nDuration: {after:.0f}s ({len(keep_segments)} segments kept)"


def _parse_post_times(env: dict) -> list[dtime]:
    raw = env.get("YOUTUBE_POST_TIMES", "10:00")
    times = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        hh, mm = part.split(":")
        times.append(dtime(int(hh), int(mm)))
    return sorted(times) or [dtime(10, 0)]


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _next_publish_slot(env: dict) -> datetime:
    post_times = _parse_post_times(env)
    now = datetime.now().astimezone()
    state = _load_state()
    last_raw = state.get("last_scheduled")
    baseline = now
    if last_raw:
        last = datetime.fromisoformat(last_raw)
        if last > baseline:
            baseline = last

    day = baseline.date()
    for _ in range(366):
        for t in post_times:
            candidate = datetime.combine(day, t, tzinfo=baseline.tzinfo)
            if candidate > baseline:
                return candidate
        day = day + timedelta(days=1)
    return baseline + timedelta(days=1)  # unreachable fallback


@mcp.tool()
def queue_video_for_upload(
    video_path: str,
    title: str,
    description: str,
    tags: list[str],
    category_id: str = DEFAULT_CATEGORY_ID,
    publish_at: str = "",
) -> str:
    """Upload a finished video to YouTube as private, scheduled to go public
    automatically at `publish_at` (ISO 8601, e.g. "2026-07-18T14:00:00-05:00").
    If `publish_at` is omitted, computes the next open slot from
    YOUTUBE_POST_TIMES in ~/.youtube/.env (comma-separated HH:MM, local time -
    one entry = 1/day, two entries = 2/day), walking forward from the later of
    now or the last video queued, so repeated calls across a batch spread out
    correctly without double-booking a slot.

    On success, moves `video_path` into a 'posted/' subfolder next to it
    (renamed with today's date prefix) so it drops out of any "still needs
    posting" queue, and logs the mapping to youtube_log.jsonl.

    This schedules the video to go live publicly with NO further confirmation
    at publish time - always confirm the exact title, description, tags, and
    publish time with the user before calling this, never call it unprompted.
    Requires YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN
    in ~/.youtube/.env, set up via youtube_oauth_setup.py.
    """
    path = _resolve_video_path(video_path)
    if path is None:
        return f"Error: {video_path} does not exist"

    env = _load_env()
    try:
        access_token = _refresh_access_token(env)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        return f"Error refreshing YouTube access token: {e}"
    if not access_token:
        return (
            f"Error: YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN "
            f"not found in {ENV_FILE}. Run youtube_oauth_setup.py first."
        )

    if publish_at:
        publish_dt = datetime.fromisoformat(publish_at)
    else:
        publish_dt = _next_publish_slot(env)
    publish_iso = publish_dt.astimezone().isoformat()

    if publish_dt.astimezone() <= datetime.now().astimezone() + timedelta(minutes=5):
        return f"Error: publish_at ({publish_iso}) must be at least 5 minutes in the future"

    metadata = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_iso,
            "selfDeclaredMadeForKids": False,
        },
    }

    content_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    file_size = path.stat().st_size

    init_req = urllib.request.Request(UPLOAD_URL, data=json.dumps(metadata).encode(), method="POST")
    init_req.add_header("Authorization", f"Bearer {access_token}")
    init_req.add_header("Content-Type", "application/json; charset=UTF-8")
    init_req.add_header("X-Upload-Content-Type", content_type)
    init_req.add_header("X-Upload-Content-Length", str(file_size))

    try:
        with urllib.request.urlopen(init_req, timeout=HTTP_TIMEOUT) as resp:
            upload_location = resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="replace")
        _log({"tool": "queue_video_for_upload", "video_path": video_path, "error": error_body, "status": e.code})
        return f"Error initiating YouTube upload ({e.code}): {error_body}"

    if not upload_location:
        return "Error: YouTube did not return a resumable upload Location header"

    with path.open("rb") as f:
        put_req = urllib.request.Request(upload_location, data=f, method="PUT")
        put_req.add_header("Content-Type", content_type)
        put_req.add_header("Content-Length", str(file_size))
        try:
            with urllib.request.urlopen(put_req, timeout=UPLOAD_TIMEOUT) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode(errors="replace")
            _log({"tool": "queue_video_for_upload", "video_path": video_path, "error": error_body, "status": e.code})
            return f"Error uploading video bytes ({e.code}): {error_body}"

    video_id = body.get("id", "")
    video_url = f"https://youtu.be/{video_id}" if video_id else "(no video id returned)"

    posted_dir = path.parent / "posted"
    posted_dir.mkdir(exist_ok=True)
    date_prefix = datetime.now().strftime("%Y-%m-%d")
    dest = posted_dir / f"{date_prefix}_{path.name}"
    path.rename(dest)

    transcript_path = path.with_suffix(path.suffix + ".transcript.json")
    if transcript_path.exists():
        transcript_path.rename(posted_dir / f"{date_prefix}_{transcript_path.name}")

    state = _load_state()
    state["last_scheduled"] = publish_iso
    _save_state(state)

    _log({
        "tool": "queue_video_for_upload", "video_path": video_path, "moved_to": str(dest),
        "video_id": video_id, "title": title, "publish_at": publish_iso,
    })
    return f"Queued: {video_url}\nScheduled to go public: {publish_iso}\nSource moved to: {dest}"


@mcp.tool()
def list_pending_videos(folder: str = "") -> str:
    """List video files in `folder` that have not yet been queued for upload
    (anything not already inside its 'posted/' subfolder). Defaults to
    YOUTUBE_VIDEO_DIR from ~/.youtube/.env if `folder` is omitted. Use this
    to see what's left in a batch-recording day's queue.
    """
    env = _load_env()
    target = folder or env.get("YOUTUBE_VIDEO_DIR", "")
    if not target:
        return "Error: no folder given and YOUTUBE_VIDEO_DIR not set in ~/.youtube/.env"

    base = Path(target).expanduser()
    if not base.exists():
        return f"Error: {base} does not exist"

    exts = {".mp4", ".mov", ".mkv", ".webm"}
    pending = sorted(
        p for p in base.iterdir()
        if p.is_file() and p.suffix.lower() in exts
    )
    if not pending:
        return f"No pending videos in {base}"
    return "\n".join(str(p) for p in pending)


if __name__ == "__main__":
    mcp.run()
