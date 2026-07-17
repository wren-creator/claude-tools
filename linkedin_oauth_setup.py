#!/usr/bin/env python3
"""One-time OAuth setup for linkedin-bridge.

Run this once (and again whenever the access token expires) to authorize
posting as yourself. It opens LinkedIn's consent screen in your browser,
catches the redirect on localhost, exchanges the code for an access token,
fetches your person URN, and writes both back into ~/.linkedin/.env for
linkedin_bridge.py to read.

Requires LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET to already be set
in ~/.linkedin/.env (from the app's Auth tab at linkedin.com/developers/apps).
"""
import http.server
import json
import secrets
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ENV_FILE = Path.home() / ".linkedin" / ".env"
REDIRECT_URI = "http://localhost:8765/callback"
SCOPE = "openid profile w_member_social"
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
CALLBACK_TIMEOUT = 180


def _read_env() -> dict:
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
    env = _read_env()
    client_id = env.get("LINKEDIN_CLIENT_ID")
    client_secret = env.get("LINKEDIN_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(f"Missing LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET in {ENV_FILE}")

    state = secrets.token_urlsafe(16)
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode({
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': REDIRECT_URI,
        'scope': SCOPE,
        'state': state,
    })}"

    server = http.server.HTTPServer(("localhost", 8765), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()

    print(f"Opening browser for LinkedIn authorization...\n{auth_url}\n")
    webbrowser.open(auth_url)

    thread.join(timeout=CALLBACK_TIMEOUT)
    server.server_close()

    result = _CallbackHandler.result
    if not result.get("code"):
        raise SystemExit(f"Authorization failed: {result.get('error') or 'timed out waiting for redirect'}")
    if result.get("state") != state:
        raise SystemExit("State mismatch on redirect - possible CSRF, aborting")

    print("Got authorization code, exchanging for access token...")
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

    access_token = token_response["access_token"]
    expires_in = token_response.get("expires_in")

    print("Fetching person URN...")
    req = urllib.request.Request(USERINFO_URL)
    req.add_header("Authorization", f"Bearer {access_token}")
    with urllib.request.urlopen(req) as resp:
        userinfo = json.loads(resp.read())

    person_urn = f"urn:li:person:{userinfo['sub']}"

    env["LINKEDIN_ACCESS_TOKEN"] = access_token
    env["LINKEDIN_PERSON_URN"] = person_urn
    if expires_in:
        env["LINKEDIN_TOKEN_EXPIRES_IN"] = str(expires_in)
    _write_env(env)

    print(f"\nDone. Access token and person URN saved to {ENV_FILE}.")
    print(f"Person URN: {person_urn}")
    if expires_in:
        days = int(expires_in) // 86400
        print(f"Token valid for ~{days} days. Re-run this script once it expires - "
              f"standard LinkedIn apps don't get a refresh token without extra approval.")


if __name__ == "__main__":
    main()
