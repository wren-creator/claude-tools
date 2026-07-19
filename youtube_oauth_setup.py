#!/usr/bin/env python3
"""One-time OAuth setup for youtube-bridge.

Run this once to authorize uploading to your channel. It opens Google's
consent screen in your browser, catches the redirect on localhost, exchanges
the code for a refresh token, and writes it into ~/.youtube/.env for
youtube_bridge.py to read. Unlike LinkedIn's ~60-day access token, Google's
refresh token doesn't expire from normal use, so this should only need to run
once (re-run only if you revoke access or Google invalidates it).

Requires YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET to already be set in
~/.youtube/.env (from a Google Cloud project's OAuth client, type "Desktop
app", with the YouTube Data API v3 enabled).
"""
import http.server
import json
import secrets
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ENV_FILE = Path.home() / ".youtube" / ".env"
REDIRECT_URI = "http://localhost:8766/callback"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true"
CALLBACK_TIMEOUT = 180


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


def _write_env(env: dict) -> None:
    ENV_FILE.parent.mkdir(exist_ok=True)
    lines = [f"{key}={value}" for key, value in env.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result["code"] = params.get("code", [None])[0]
        _CallbackHandler.result["state"] = params.get("state", [None])[0]
        _CallbackHandler.result["error"] = params.get("error_description", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if _CallbackHandler.result["code"]:
            body = "<h1>Authorized</h1><p>You can close this tab and return to the terminal.</p>"
        else:
            body = f"<h1>Error</h1><p>{_CallbackHandler.result['error'] or 'No code returned'}</p>"
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        pass


def main():
    env = _load_env()
    client_id = env.get("YOUTUBE_CLIENT_ID")
    client_secret = env.get("YOUTUBE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(f"Missing YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET in {ENV_FILE}")

    state = secrets.token_urlsafe(16)
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode({
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': REDIRECT_URI,
        'scope': SCOPE,
        'state': state,
        'access_type': 'offline',
        'prompt': 'consent',
    })}"

    server = http.server.HTTPServer(("localhost", 8766), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()

    print(f"Opening browser for Google authorization...\n{auth_url}\n")
    webbrowser.open(auth_url)

    thread.join(timeout=CALLBACK_TIMEOUT)
    server.server_close()

    result = _CallbackHandler.result
    if not result.get("code"):
        raise SystemExit(f"Authorization failed: {result.get('error') or 'timed out waiting for redirect'}")
    if result.get("state") != state:
        raise SystemExit("State mismatch on redirect - possible CSRF, aborting")

    print("Got authorization code, exchanging for tokens...")
    token_data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=token_data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        token_response = json.loads(resp.read())

    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    if not refresh_token:
        raise SystemExit(
            "No refresh_token returned. This usually means the Google account "
            "already granted this app consent previously without access_type=offline. "
            "Revoke access at https://myaccount.google.com/permissions and re-run this script."
        )

    env["YOUTUBE_CLIENT_ID"] = client_id
    env["YOUTUBE_CLIENT_SECRET"] = client_secret
    env["YOUTUBE_REFRESH_TOKEN"] = refresh_token
    _write_env(env)
    print(f"\nDone. Refresh token saved to {ENV_FILE}.")

    print("Fetching channel info to confirm...")
    try:
        req = urllib.request.Request(CHANNELS_URL)
        req.add_header("Authorization", f"Bearer {access_token}")
        with urllib.request.urlopen(req) as resp:
            channels = json.loads(resp.read())
        channel_title = (
            channels.get("items", [{}])[0].get("snippet", {}).get("title", "(unknown channel)")
            if channels.get("items") else "(no channel found on this account)"
        )
        print(f"Authorized channel: {channel_title}")
    except urllib.error.HTTPError as e:
        print(
            f"(Skipped channel confirmation - {e.code} {e.reason}. This is just a "
            "nice-to-have check and doesn't affect uploads, which use the "
            "youtube.upload scope you already granted.)"
        )
    print(
        "\nAlso set YOUTUBE_VIDEO_DIR (folder you drop raw recordings into) and "
        "YOUTUBE_POST_TIMES (comma-separated HH:MM local times, e.g. 10:00 or "
        "10:00,17:00 for 2/day) in that same .env file."
    )


if __name__ == "__main__":
    main()
