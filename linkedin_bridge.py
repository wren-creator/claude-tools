import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("linkedin-bridge")

ENV_FILE = Path.home() / ".linkedin" / ".env"
LOG_PATH = Path(__file__).parent / "linkedin_log.jsonl"
POSTS_URL = "https://api.linkedin.com/rest/posts"
HTTP_TIMEOUT = 15
VALID_VISIBILITY = {"PUBLIC", "CONNECTIONS"}
URN_PATTERN = re.compile(r"urn:li:(?:share|ugcPost):\d+")
# SOLVED: what looked like random truncation was LinkedIn's "little" text
# format (https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/little-text-format)
# - the commentary field isn't plain text, it's a mini markup language for
# mentions/hashtags, and characters reserved for that markup must be
# backslash-escaped to appear as literal text. Checked every post logged
# today against this: every single one containing an unescaped '(' or ')'
# rendered truncated in the feed; every one without either character
# rendered in full. 11/11 posts matched this pattern with no exceptions.
# _escape_little_format() below escapes all reserved characters except
# '#word' sequences, which are left alone so intentional hashtags still
# render as hashtags instead of literal text. Confirmed fixed: a test post
# containing parentheses rendered in full after this escaping was added.
LITTLE_FORMAT_RESERVED = set("|{}@[]()<>\\*_~#")


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


def _extract_urn(post_url_or_urn: str) -> str | None:
    match = URN_PATTERN.search(post_url_or_urn)
    return match.group(0) if match else None


def _escape_little_format(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        is_hashtag = ch == "#" and i + 1 < len(text) and (text[i + 1].isalnum() or text[i + 1] == "_")
        if is_hashtag or ch not in LITTLE_FORMAT_RESERVED:
            out.append(ch)
        else:
            out.append("\\" + ch)
        i += 1
    return "".join(out)


@mcp.tool()
def post_to_linkedin(text: str, visibility: str = "PUBLIC") -> str:
    """Publish a text post to LinkedIn on behalf of the authenticated member.
    This posts publicly (or to connections-only if visibility="CONNECTIONS")
    under the user's real identity - always confirm the exact text with the
    user before calling this, never call it unprompted.
    Requires LINKEDIN_ACCESS_TOKEN and LINKEDIN_PERSON_URN in ~/.linkedin/.env,
    set up via linkedin_oauth_setup.py. If the token has expired (~60 days),
    re-run that script to refresh it.

    Automatically escapes characters reserved by LinkedIn's "little" text
    format (parentheses, brackets, etc. - see the module-level comment above
    LITTLE_FORMAT_RESERVED) so they appear as literal text instead of being
    misparsed as markup, which is what caused posts to render truncated
    before this was root-caused.
    """
    if visibility not in VALID_VISIBILITY:
        return f"Error: visibility must be one of {sorted(VALID_VISIBILITY)}, got {visibility!r}"

    creds = _load_credentials()
    access_token = creds.get("LINKEDIN_ACCESS_TOKEN")
    person_urn = creds.get("LINKEDIN_PERSON_URN")
    if not access_token or not person_urn:
        return (
            f"Error: LINKEDIN_ACCESS_TOKEN / LINKEDIN_PERSON_URN not found in {ENV_FILE}. "
            "Run linkedin_oauth_setup.py first."
        )

    escaped_text = _escape_little_format(text)
    body = {
        "author": person_urn,
        "commentary": escaped_text,
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
        _log({"tool": "post_to_linkedin", "text": text, "escaped_text": escaped_text, "visibility": visibility, "error": error_body, "status": e.code})
        return f"Error posting to LinkedIn ({e.code}): {error_body}"
    except urllib.error.URLError as e:
        _log({"tool": "post_to_linkedin", "text": text, "escaped_text": escaped_text, "visibility": visibility, "error": str(e)})
        return f"Error posting to LinkedIn: {e}"

    post_url = f"https://www.linkedin.com/feed/update/{post_urn}/" if post_urn else "(no post URN returned)"
    _log({"tool": "post_to_linkedin", "text": text, "escaped_text": escaped_text, "visibility": visibility, "status": status, "post_urn": post_urn})
    return f"Posted successfully ({status}). {post_url}"


@mcp.tool()
def update_linkedin_post(post_url_or_urn: str, text: str) -> str:
    """Replace the text of an existing LinkedIn post, identified by its live
    URL (e.g. https://www.linkedin.com/feed/update/urn:li:share:.../) or bare
    URN (e.g. urn:li:share:...). This edits a real, public post under the
    user's identity - always confirm the exact replacement text with the
    user before calling this, never call it unprompted.
    Only needs the w_member_social scope this app already has - unlike
    reading a post back to verify its content, which needs r_member_social
    (currently closed by LinkedIn for new access requests, so there's no
    read-back tool yet - ask the user to check the live URL instead).
    Automatically escapes characters reserved by LinkedIn's "little" text
    format (see the module-level comment above LITTLE_FORMAT_RESERVED).
    """
    urn = _extract_urn(post_url_or_urn)
    if urn is None:
        return f"Error: could not find a urn:li:share:... or urn:li:ugcPost:... in {post_url_or_urn!r}"

    creds = _load_credentials()
    access_token = creds.get("LINKEDIN_ACCESS_TOKEN")
    if not access_token:
        return f"Error: LINKEDIN_ACCESS_TOKEN not found in {ENV_FILE}. Run linkedin_oauth_setup.py first."

    escaped_text = _escape_little_format(text)
    url = f"{POSTS_URL}/{urllib.parse.quote(urn, safe='')}"
    body = json.dumps({"patch": {"$set": {"commentary": escaped_text}}}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Restli-Protocol-Version", "2.0.0")
    req.add_header("Linkedin-Version", time.strftime("%Y%m"))
    req.add_header("X-RestLi-Method", "PARTIAL_UPDATE")

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="replace")
        _log({"tool": "update_linkedin_post", "urn": urn, "text": text, "escaped_text": escaped_text, "error": error_body, "status": e.code})
        return f"Error updating LinkedIn post ({e.code}): {error_body}"
    except urllib.error.URLError as e:
        _log({"tool": "update_linkedin_post", "urn": urn, "text": text, "escaped_text": escaped_text, "error": str(e)})
        return f"Error updating LinkedIn post: {e}"

    post_url = f"https://www.linkedin.com/feed/update/{urn}/"
    _log({"tool": "update_linkedin_post", "urn": urn, "text": text, "escaped_text": escaped_text, "status": status})
    return f"Updated successfully ({status}). {post_url}"


@mcp.tool()
def delete_linkedin_post(post_url_or_urn: str) -> str:
    """Permanently delete a LinkedIn post, identified by its live URL (e.g.
    https://www.linkedin.com/feed/update/urn:li:share:.../) or bare URN
    (e.g. urn:li:share:...). This is irreversible and affects the user's
    real public profile - always confirm with the user before calling this,
    never call it unprompted.
    """
    urn = _extract_urn(post_url_or_urn)
    if urn is None:
        return f"Error: could not find a urn:li:share:... or urn:li:ugcPost:... in {post_url_or_urn!r}"

    creds = _load_credentials()
    access_token = creds.get("LINKEDIN_ACCESS_TOKEN")
    if not access_token:
        return f"Error: LINKEDIN_ACCESS_TOKEN not found in {ENV_FILE}. Run linkedin_oauth_setup.py first."

    url = f"{POSTS_URL}/{urllib.parse.quote(urn, safe='')}"
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("X-Restli-Protocol-Version", "2.0.0")
    req.add_header("Linkedin-Version", time.strftime("%Y%m"))

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="replace")
        _log({"tool": "delete_linkedin_post", "urn": urn, "error": error_body, "status": e.code})
        return f"Error deleting LinkedIn post ({e.code}): {error_body}"
    except urllib.error.URLError as e:
        _log({"tool": "delete_linkedin_post", "urn": urn, "error": str(e)})
        return f"Error deleting LinkedIn post: {e}"

    _log({"tool": "delete_linkedin_post", "urn": urn, "status": status})
    return f"Deleted successfully ({status})."


if __name__ == "__main__":
    mcp.run()
