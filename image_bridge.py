import json
import mimetypes
import time
from pathlib import Path

from google import genai
from google.genai import types
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("image-bridge")

LOG_PATH = Path(__file__).parent / "log.jsonl"
GEMINI_ENV_FILE = Path.home() / ".gemini" / ".env"
IMAGE_MODEL = "gemini-2.5-flash-image"


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    entry["tool"] = "image_bridge." + entry.get("tool", "?")
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _api_key() -> str | None:
    if not GEMINI_ENV_FILE.exists():
        return None
    for line in GEMINI_ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("GEMINI_API_KEY="):
            return line.partition("=")[2].strip()
    return None


def _client() -> genai.Client:
    key = _api_key()
    if not key:
        raise RuntimeError(f"No GEMINI_API_KEY found in {GEMINI_ENV_FILE}")
    return genai.Client(api_key=key)


@mcp.tool()
def generate_image(
    prompt: str,
    output_path: str,
    reference_image_paths: list[str] = [],
) -> str:
    """Generate an image with Gemini's native image model and save it to disk.

    Pass reference_image_paths (absolute paths to existing images, e.g. a book
    cover) to guide style, character likeness, and composition, the model will
    match the visual style of the reference(s) rather than starting from
    nothing. This is the right tool for a series of illustrations that need to
    look like they belong to the same book: pass the same reference image(s)
    (or a previously generated page) across calls for consistency.

    output_path must be an absolute path ending in .png or .jpg. Returns the
    saved path on success, or an error string.
    """
    try:
        client = _client()
    except RuntimeError as e:
        return f"Error: {e}"

    parts: list = []
    for ref_path in reference_image_paths:
        p = Path(ref_path)
        if not p.exists():
            return f"Error: reference image not found: {ref_path}"
        mime, _ = mimetypes.guess_type(str(p))
        parts.append(types.Part.from_bytes(data=p.read_bytes(), mime_type=mime or "image/jpeg"))
    parts.append(types.Part.from_text(text=prompt))

    try:
        response = client.models.generate_content(
            model=IMAGE_MODEL,
            contents=[types.Content(role="user", parts=parts)],
        )
    except Exception as e:
        _log({"tool": "generate_image", "prompt": prompt, "reference_image_paths": reference_image_paths, "error": str(e)})
        return f"Error calling Gemini image model: {e}"

    image_bytes = None
    mime_type = None
    for candidate in response.candidates or []:
        for part in candidate.content.parts or []:
            if part.inline_data is not None:
                image_bytes = part.inline_data.data
                mime_type = part.inline_data.mime_type
                break
        if image_bytes:
            break

    if not image_bytes:
        text = getattr(response, "text", None) or "no image returned, no text explanation either"
        _log({"tool": "generate_image", "prompt": prompt, "reference_image_paths": reference_image_paths, "error": "no image in response", "model_text": text})
        return f"Error: model did not return an image. Model said: {text}"

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(image_bytes)

    _log({
        "tool": "generate_image",
        "prompt": prompt,
        "reference_image_paths": reference_image_paths,
        "output_path": str(out),
        "mime_type": mime_type,
        "bytes": len(image_bytes),
    })
    return f"Saved image to {out} ({len(image_bytes)} bytes, {mime_type})"


if __name__ == "__main__":
    mcp.run()
