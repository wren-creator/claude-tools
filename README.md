# claude-tools

Local MCP servers that give Claude Code access to other tools mid-session.

## gemini-bridge

Exposes two tools backed by the [Gemini CLI](https://google-gemini.github.io/gemini-cli/):

- `ask_gemini(prompt, context="")` — ask Gemini a question, e.g. for a second
  opinion on an approach.
- `review_diff(repo_path, instructions="")` — runs `git diff` in `repo_path`
  and sends it to Gemini for critique. Pass the absolute path of the repo you
  want reviewed; the bridge runs as its own background process and does not
  share Claude Code's working directory.

Every call is logged to `log.jsonl` (gitignored) as an audit trail of what
was asked and answered.

### Setup

1. Install the Gemini CLI:
   ```
   npm install -g @google/gemini-cli
   ```
2. Authenticate with an API key — as of gemini-cli 0.50.0, the free
   `oauth-personal` login tier ("Gemini Code Assist for individuals") is no
   longer accepted; Google points individual users at a separate product
   (Antigravity) instead. Use an API key:
   - Get a free key from [Google AI Studio](https://aistudio.google.com/apikey).
   - Put it in `~/.gemini/.env`:
     ```
     GEMINI_API_KEY=your-key-here
     ```
   - Set `~/.gemini/settings.json` to use it:
     ```json
     {
       "security": { "auth": { "selectedType": "gemini-api-key" } }
     }
     ```
3. Install this project's dependencies:
   ```
   cd ~/git/claude-tools
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
4. Register the server with Claude Code (user scope, so it's available in
   every project):
   ```
   claude mcp add gemini-bridge --scope user -- \
     ~/git/claude-tools/.venv/bin/python ~/git/claude-tools/gemini_bridge.py
   ```
5. Restart Claude Code / reload the window. `ask_gemini` and `review_diff`
   should show up as callable tools.

### Notes

- `gemini_bridge.py` reads `~/.gemini/.env` itself and sets
  `GEMINI_CLI_TRUST_WORKSPACE=true` on every call, rather than relying on
  gemini-cli's own env-file auto-discovery (unreliable when invoked as a
  subprocess from an arbitrary cwd) or its interactive trusted-folder
  prompt (which headless calls can't answer).
- Gemini CLI's `-p`/`--prompt` non-interactive flag is flagged upstream as a
  candidate for future deprecation ([gemini-cli#16025](https://github.com/google-gemini/gemini-cli/issues/16025)).
  If it's renamed, only `_call_gemini()` in `gemini_bridge.py` needs updating.
- Context and diffs are truncated to 60k characters before being sent to
  Gemini to avoid blowing past its context window.

## tn3270-bridge

Exposes five tools backed by [py3270](https://pypi.org/project/py3270/) (which
drives `s3270` under the hood) for automating TN3270 mainframe green-screen
sessions:

- `connect(host, port=23)` — opens a session, returns a `session_id`.
- `send_keys(session_id, keys)` — types `keys` into the current field. A
  `"\n"` in `keys` presses Enter (e.g. `"myuser\n"` types `myuser` and
  submits it). Returns the resulting screen.
- `read_screen(session_id)` — returns the current screen as plain text.
- `send_function_key(session_id, key)` — sends `PFn`/`PAn`/`Clear`/`Enter`.
  Returns the resulting screen.
- `disconnect(session_id)` — closes the session.

Sessions live in memory for the lifetime of the server process (one per
Claude Code session) — `disconnect` any session you're done with rather than
letting it leak. Every call is logged to `tn3270_log.jsonl` (gitignored).

### Setup

1. Install `s3270` (part of the x3270 suite):
   ```
   brew install x3270
   ```
2. Install this project's dependencies (shared `.venv` with gemini-bridge):
   ```
   cd ~/git/claude-tools
   .venv/bin/pip install -r requirements.txt
   ```
3. Register the server with Claude Code:
   ```
   claude mcp add tn3270-bridge --scope user -- \
     ~/git/claude-tools/.venv/bin/python ~/git/claude-tools/tn3270_bridge.py
   ```
4. Restart Claude Code / reload the window.

### Notes

- `read_screen` dumps the full screen buffer via x3270's `Ascii()` script
  command — plain text, one line per row, no field/attribute metadata. If an
  agent needs to know *where* fields are (protected vs. unprotected, cursor
  position) rather than just what's on screen, that's the next layer to add.
- This is a scaffold: connect/send_keys/read_screen/send_function_key/
  disconnect all work against a real `s3270` process, but it's only had
  smoke-level testing so far, not a real mainframe session.

## Roadmap

- [ ] Add a third gemini-bridge tool for querying Gemini's larger context
      window on full files, not just diffs.
- [ ] repo-aware MCP server: `search_codebase`, `get_file`, `list_structure`,
      and (tree-sitter-backed) `get_symbol` tools, for cases where Claude
      needs codebase context outside its own working directory.
- [ ] tn3270-bridge: structured `read_screen` mode (field positions,
      protected/unprotected, cursor location) alongside the plain-text dump,
      for when an agent needs to know where to type, not just what's shown.
- [ ] Front the MCP servers with an [`mcpo`](https://github.com/open-webui/mcpo)
      (MCP-to-OpenAPI) proxy so tool-calling harnesses built on Ollama or
      llama.cpp — which don't speak MCP natively — can call these tools over
      plain HTTP.
