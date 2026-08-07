import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("discord-bridge")

ENV_FILE = Path.home() / ".discord" / ".env"
LOG_PATH = Path(__file__).parent / "discord_log.jsonl"
DISCORD_API = "https://discord.com/api/v10"
HTTP_TIMEOUT = 30
USER_AGENT = "DiscordBot (https://github.com/britleyhoff/claude-tools, 1.0)"

ERROR_HINTS = {
    401: f" - check DISCORD_BOT_TOKEN in {ENV_FILE}",
    403: " - bot is missing a permission in this channel (View Channel / Read Message History / Send Messages), or hasn't been invited to this server",
    404: " - channel or guild ID not found, or the bot can't see it",
}


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _load_token() -> str | None:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def _discord_request(method: str, path: str, token: str, body: dict | None = None) -> dict | list:
    url = f"{DISCORD_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def _format_discord_error(e: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(e.read())
        message = body.get("message", "unknown_error")
        code = body.get("code", "")
        detail = f"{message} (code {code})" if code else message
    except (json.JSONDecodeError, UnicodeDecodeError):
        detail = f"HTTP {e.code}"
    hint = ERROR_HINTS.get(e.code, "")
    return f"Error from Discord API: {detail}{hint}"


@mcp.tool()
def list_channels(guild_id: str) -> str:
    """List the text channels in a Discord server (guild), so you can find a
    channel's ID from its name before calling read_channel/post_message —
    Discord's API addresses channels by numeric ID, not name.

    guild_id is the server's numeric ID (right-click the server icon ->
    Copy Server ID, with Developer Mode on in Discord's settings). Requires
    DISCORD_BOT_TOKEN in ~/.discord/.env, and the bot must already be a
    member of the server.
    """
    token = _load_token()
    if not token:
        return f"Error: DISCORD_BOT_TOKEN not found in {ENV_FILE}"

    try:
        channels = _discord_request("GET", f"/guilds/{guild_id}/channels", token)
    except urllib.error.HTTPError as e:
        return _format_discord_error(e)
    except urllib.error.URLError as e:
        return f"Error calling Discord API: {e.reason}"

    # type 0 = text channel, 5 = announcement channel
    text_channels = [c for c in channels if c.get("type") in (0, 5)]
    if not text_channels:
        return "No text channels found in this server."

    lines = [f"#{c['name']} (id: {c['id']})" for c in text_channels]
    return "\n".join(lines)


@mcp.tool()
def create_channel(guild_id: str, name: str, topic: str = "") -> str:
    """Create a new text channel in a Discord server. Requires
    DISCORD_BOT_TOKEN in ~/.discord/.env, and the bot must have the Manage
    Channels permission in this server.

    name is auto-formatted by Discord (lowercased, spaces become hyphens).
    topic is optional and shows under the channel name in Discord's UI.

    This creates a real, visible channel immediately — always confirm the
    exact server and channel name with the user before calling this, never
    call it unprompted, same rule as post_message.
    """
    token = _load_token()
    if not token:
        return f"Error: DISCORD_BOT_TOKEN not found in {ENV_FILE}"

    body = {"name": name, "type": 0}
    if topic:
        body["topic"] = topic

    try:
        result = _discord_request("POST", f"/guilds/{guild_id}/channels", token, body=body)
    except urllib.error.HTTPError as e:
        return _format_discord_error(e)
    except urllib.error.URLError as e:
        return f"Error calling Discord API: {e.reason}"

    _log({"tool": "create_channel", "guild_id": guild_id, "name": name, "channel_id": result.get("id")})
    return f"Created #{result.get('name')} (id: {result.get('id')})."


@mcp.tool()
def read_channel(channel_id: str, limit: int = 20) -> str:
    """Read the most recent messages from a Discord text channel, newest
    first. Requires DISCORD_BOT_TOKEN in ~/.discord/.env, the bot must be a
    member of the server with View Channel + Read Message History
    permissions on this channel, and the Message Content privileged intent
    must be enabled for the bot (Discord Developer Portal -> Bot tab) —
    without it, content on messages the bot didn't author comes back empty
    even with the right permissions.

    limit is capped at 100 (Discord's own per-request max).
    """
    token = _load_token()
    if not token:
        return f"Error: DISCORD_BOT_TOKEN not found in {ENV_FILE}"

    limit = max(1, min(limit, 100))

    try:
        messages = _discord_request(
            "GET", f"/channels/{channel_id}/messages?limit={limit}", token,
        )
    except urllib.error.HTTPError as e:
        return _format_discord_error(e)
    except urllib.error.URLError as e:
        return f"Error calling Discord API: {e.reason}"

    if not messages:
        return "No messages found in this channel."

    lines = []
    for m in messages:
        author = m.get("author", {}).get("username", "unknown")
        content = m.get("content", "").strip() or "[no text content]"
        ts = m.get("timestamp", "")
        line = f"[{ts}] {author}: {content}"
        for att in m.get("attachments", []):
            line += f"\n  attachment: {att.get('url', '')}"
        lines.append(line)

    _log({"tool": "read_channel", "channel_id": channel_id, "message_count": len(messages)})
    return "\n".join(lines)


@mcp.tool()
def post_message(channel_id: str, content: str) -> str:
    """Post a message to a Discord text channel under the bot's own
    identity. Requires DISCORD_BOT_TOKEN in ~/.discord/.env, and the bot
    must have Send Messages permission in this channel.

    This posts live and immediately, visible to everyone in the channel —
    always confirm the exact channel and text with the user before calling
    this, never call it unprompted.
    """
    token = _load_token()
    if not token:
        return f"Error: DISCORD_BOT_TOKEN not found in {ENV_FILE}"

    try:
        result = _discord_request(
            "POST", f"/channels/{channel_id}/messages", token,
            body={"content": content},
        )
    except urllib.error.HTTPError as e:
        return _format_discord_error(e)
    except urllib.error.URLError as e:
        return f"Error calling Discord API: {e.reason}"

    _log({"tool": "post_message", "channel_id": channel_id, "content": content, "message_id": result.get("id")})
    return f"Posted successfully (message id: {result.get('id')})."


if __name__ == "__main__":
    mcp.run()
