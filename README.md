# claude-tools

Local MCP servers that give Claude Code access to other tools mid-session.

## gemini-bridge

Exposes three tools backed by the [Gemini CLI](https://google-gemini.github.io/gemini-cli/):

- `ask_gemini(prompt, context="")` — ask Gemini a question, e.g. for a second
  opinion on an approach.
- `review_diff(repo_path, instructions="")` — runs `git diff` in `repo_path`
  and sends it to Gemini for critique. Pass the absolute path of the repo you
  want reviewed; the bridge runs as its own background process and does not
  share Claude Code's working directory.
- `ask_gemini_about_files(file_paths, question)` — reads one or more full
  files and asks Gemini a question about them. Use this instead of
  `ask_gemini`'s `context` param when the files are too large for Claude's
  own context, or when you want Gemini's take on whole files/modules rather
  than a truncated excerpt — Gemini's window is large enough to hold much
  more (up to 500k chars) than `ask_gemini`/`review_diff` allow (60k chars).
  Pass absolute paths; the bridge runs as its own process and does not share
  Claude Code's cwd.

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
5. Restart Claude Code / reload the window. `ask_gemini`, `review_diff`, and
   `ask_gemini_about_files` should show up as callable tools.

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
  Gemini to avoid blowing past its context window. `ask_gemini_about_files`
  uses a much higher 500k-character ceiling, since its whole point is to use
  Gemini's larger window on full files.
- Before invoking the Gemini CLI, `_call_gemini()` does a quick 5s TCP
  preflight check against Gemini's API host. On a flaky connection (e.g. a
  phone hotspot), this fails fast with a clear error instead of blocking for
  the full `GEMINI_TIMEOUT` (120s). Restart Claude Code / reload the window
  after pulling this change, since the running server process won't pick it
  up otherwise.

## tn3270-bridge

Exposes five tools backed by [py3270](https://pypi.org/project/py3270/) (which
drives `s3270` under the hood) for automating TN3270 mainframe green-screen
sessions:

- `connect(host, port=23)` — opens a session, returns a `session_id`.
- `send_keys(session_id, keys)` — types `keys` into the current field. A
  `"\n"` in `keys` presses Enter (e.g. `"myuser\n"` types `myuser` and
  submits it). Returns the resulting screen.
- `read_screen(session_id, structured=False)` — returns the current screen
  as plain text. With `structured=True`, returns JSON instead:
  `{"cursor": {"row", "col"}, "fields": [{"row", "col", "protected",
  "hidden", "autoskip", "modified", "text"}, ...]}` — use this when an agent
  needs to know *where* to type (which field is unprotected, which one is a
  hidden password field) rather than just what's on screen. Row/col are
  1-indexed and refer to the field's attribute-byte position, so the first
  typeable character is one column to the right of `col`.
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

- Plain-text `read_screen` uses x3270's `Ascii()` script command. Structured
  mode uses `ReadBuffer(Ascii)`, which annotates the buffer with `SF(...)`
  markers at each field's attribute byte; the byte's bits are decoded per
  the 3270 field-attribute spec (IBM GA23-0059) into `protected`/`hidden`/
  `autoskip`/`modified`. `hidden` is what marks password fields — verified
  against a live TSO logon screen, where `Password`/`New Password`/`MFA
  Token` all came back `hidden: true` and `Userid` did not.
- Field `row`/`col` and cursor `row`/`col` come from two different x3270
  commands with different indexing (`ReadBuffer` is 1-indexed, `Query
  (Cursor)` is 0-indexed) — `_structured_screen()` normalizes both to
  1-indexed. Confirmed by checking the cursor lands one column past an
  unprotected field's attribute byte, which is where typing actually starts.
- All five tools have been exercised against a live TN3270 host (a local
  test mainframe on `localhost:3270`): `connect` + `read_screen` pulled back
  the logon banner, `send_keys("TSO\n")` advanced from the logon-type prompt
  to the TSO/E LOGON screen, `send_function_key("PF3")` logged off back to
  the banner, and structured `read_screen` correctly identified the one
  unprotected field on the banner screen and the hidden password fields on
  the TSO/E LOGON screen.

## repo-bridge

Exposes four tools for getting codebase context from a repo outside Claude
Code's own working directory:

- `search_codebase(repo_path, query, max_results=50, ignore_case=False)` —
  greps `repo_path` for `query` (a regex). Uses `git grep` when `repo_path`
  is a git repo (respects `.gitignore`), otherwise plain `grep -r`. Returns
  `path:line:content` per match.
- `get_file(repo_path, path)` — returns the full contents of `path`
  (relative to `repo_path`), truncated past 60k chars. Rejects paths that
  escape `repo_path` (e.g. `../../etc/passwd`).
- `list_structure(repo_path, max_entries=500)` — returns a directory tree as
  indented text. Uses `git ls-files` (tracked files only) when `repo_path`
  is a git repo, otherwise walks the filesystem skipping common junk dirs
  (`node_modules`, `.venv`, `__pycache__`, etc.).
- `get_symbol(repo_path, name, language="")` — finds a function/class/
  method/type definition named `name` and returns its source text via
  [tree-sitter](https://tree-sitter.github.io/tree-sitter/). Supports
  `python`, `javascript`, `typescript`, `tsx`, and `go`. Greps for files
  that reference `name` first rather than parsing the whole repo, and
  returns the first match found.

Pass the absolute path of the repo you want as `repo_path` for every tool —
this server runs as its own process and does not share Claude Code's
working directory.

### Setup

1. Install this project's dependencies (shared `.venv`):
   ```
   cd ~/git/claude-tools
   .venv/bin/pip install -r requirements.txt
   ```
2. Register the server with Claude Code:
   ```
   claude mcp add repo-bridge --scope user -- \
     ~/git/claude-tools/.venv/bin/python ~/git/claude-tools/repo_bridge.py
   ```
3. Restart Claude Code / reload the window.

### Notes

- Uses the official per-language `tree-sitter-*` packages (prebuilt wheels,
  compiled at install time), not the `tree-sitter-language-pack` package —
  that one downloads grammars over the network on first use, which is a bad
  fit for this repo's other lesson-learned (see gemini-bridge's network
  preflight check above) and just failed outright when tried on a flaky
  connection.
- `get_symbol` matches by each grammar's `name` field on definition-like
  node types (`function_definition`, `class_declaration`, etc.) — this
  works generically across languages without per-language field lookups,
  but only ever returns the *first* match, so an overloaded/duplicate name
  across files will only surface one of them.
- Verified against this repo (Python) and small standalone TypeScript/Go
  fixtures — `get_symbol` correctly pulled `_call_gemini`, a TS `class`, a
  TS `function`, and a Go `func`, each with correct line ranges.

## Roadmap

- [x] Add a third gemini-bridge tool for querying Gemini's larger context
      window on full files, not just diffs.
- [ ] repo-bridge: expand `get_symbol` language support beyond
      python/javascript/typescript/tsx/go (e.g. rust, java, ruby, c/c++),
      and consider returning all matches instead of just the first.
- [x] tn3270-bridge: structured `read_screen` mode (field positions,
      protected/unprotected, cursor location) alongside the plain-text dump,
      for when an agent needs to know where to type, not just what's shown.
- [ ] Front the MCP servers with an [`mcpo`](https://github.com/open-webui/mcpo)
      (MCP-to-OpenAPI) proxy so tool-calling harnesses built on Ollama or
      llama.cpp — which don't speak MCP natively — can call these tools over
      plain HTTP.
