import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("linkedin-bridge")

ENV_FILE = Path.home() / ".linkedin" / ".env"
LOG_PATH = Path(__file__).parent / "linkedin_log.jsonl"
POSTS_URL = "https://api.linkedin.com/rest/posts"
HTTP_TIMEOUT = 15
VALID_VISIBILITY = {"PUBLIC", "CONNECTIONS"}
# LinkedIn documents a 3,000-char limit for the Posts API, but that appears to
# apply to reviewed/approved partner apps. Empirically, this app (on the free
# "Share on LinkedIn" consumer product) silently truncates posts past ~574
# chars in the feed with no error from the create call - discovered by
# posting a ~2000-char post twice and getting the same cutoff both times.
# Refuse past a conservative margin below that rather than repeat the mistake.
MAX_COMMENTARY_CHARS = 550


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


@mcp.tool()
def post_to_linkedin(text: str, visibility: str = "PUBLIC") -> str:
    """Publish a text post to LinkedIn on behalf of the authenticated member.
    This posts publicly (or to connections-only if visibility="CONNECTIONS")
    under the user's real identity - always confirm the exact text with the
    user before calling this, never call it unprompted.
    Requires LINKEDIN_ACCESS_TOKEN and LINKEDIN_PERSON_URN in ~/.linkedin/.env,
    set up via linkedin_oauth_setup.py. If the token has expired (~60 days),
    re-run that script to refresh it.
    """
    if visibility not in VALID_VISIBILITY:
        return f"Error: visibility must be one of {sorted(VALID_VISIBILITY)}, got {visibility!r}"

    if len(text) > MAX_COMMENTARY_CHARS:
        return (
            f"Error: text is {len(text)} chars, over the {MAX_COMMENTARY_CHARS}-char limit this "
            "app's posts appear to be silently truncated at. Shorten it or split it into multiple posts."
        )

    creds = _load_credentials()
    access_token = creds.get("LINKEDIN_ACCESS_TOKEN")
    person_urn = creds.get("LINKEDIN_PERSON_URN")
    if not access_token or not person_urn:
        return (
            f"Error: LINKEDIN_ACCESS_TOKEN / LINKEDIN_PERSON_URN not found in {ENV_FILE}. "
            "Run linkedin_oauth_setup.py first."
        )

    body = {
        "author": person_urn,
        "commentary": text,
        "visibility": visibility,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }

    req = urllib.request.Request(POSTS_URL, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Restli-Protocol-Version", "2.0.0")
    req.add_header("Linkedin-Version", time.strftime("%Y%m"))

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            post_urn = resp.headers.get("x-restli-id", "")
            status = resp.status
    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="replace")
        _log({"tool": "post_to_linkedin", "text": text, "visibility": visibility, "error": error_body, "status": e.code})
        return f"Error posting to LinkedIn ({e.code}): {error_body}"
    except urllib.error.URLError as e:
        _log({"tool": "post_to_linkedin", "text": text, "visibility": visibility, "error": str(e)})
        return f"Error posting to LinkedIn: {e}"

    post_url = f"https://www.linkedin.com/feed/update/{post_urn}/" if post_urn else "(no post URN returned)"
    _log({"tool": "post_to_linkedin", "text": text, "visibility": visibility, "status": status, "post_urn": post_urn})
    return f"Posted successfully ({status}). {post_url}"


if __name__ == "__main__":
    mcp.run()
