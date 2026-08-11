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

## ollama-bridge

Exposes three tools backed by a locally running [Ollama](https://ollama.com)
instance:

- `prefilter_diff(repo_path, model="qwen2.5-coder:7b")` — runs `git diff` in
  `repo_path` and sends it to a local Ollama model for a cheap first-pass
  triage before spending a `review_diff` (Gemini) call on it. The response
  starts with `CLEAN:` or `FLAGGED:` — only escalate to `review_diff` when
  it's `FLAGGED`, or when this tool errors (e.g. Ollama isn't running); never
  skip review outright just because the local pass errored.
- `triage_log(repo_path, log_path, model="qwen2.5-coder:7b")` — reads a
  build/test log already written to disk (redirect a failing command's
  output first, e.g. `cmd > out.log 2>&1`) and sends it to a local Ollama
  model to pull out just the root failure (`FILE:`/`ERROR:`/`CONTEXT:`),
  instead of reading the whole raw log directly. Returns `NO FAILURE
  FOUND.` for a clean run. `log_path` must resolve inside `repo_path`
  (absolute or relative, same containment rule as `repo-bridge`'s
  `get_file`) — rejected otherwise.
- `triage_transcript(repo_path, transcript_path, model="qwen2.5-coder:7b")`
  — reads a transcript JSON file already written by `youtube-bridge`'s
  `transcribe_video` (a list of `{start, end, text}` segments) and sends it
  to a local Ollama model to flag likely restarts, filler-heavy stretches,
  and dead air, instead of reading the whole transcript closely to spot
  them. Returns `[MM:SS-MM:SS]` timestamps with a one-line reason each, or
  `CLEAN: ...` if nothing stands out. Same `repo_path`/`transcript_path`
  containment rule as `triage_log`. Doesn't decide cuts — points at where to
  look before building `keep_segments` for `cut_video`.

Every call is logged to `ollama_log.jsonl` (gitignored) as an audit trail.

### Setup

1. Install [Ollama](https://ollama.com) and pull a coding-capable model:
   ```
   ollama pull qwen2.5-coder:7b
   ```
2. This tool has no new dependency beyond what's already in
   `requirements.txt` — it talks to Ollama's local REST API with the
   standard library's `urllib`, consistent with the rest of this repo's
   bridges.
3. Register the server with Claude Code (user scope):
   ```
   claude mcp add ollama-bridge --scope user -- \
     ~/git/claude-tools/.venv/bin/python ~/git/claude-tools/ollama_bridge.py
   ```
4. Restart Claude Code / reload the window. `prefilter_diff` should show up
   as a callable tool.

### Notes

- This exists to save Gemini API calls on routine commits, not to replace
  `review_diff`. A quantized 7B local model catches the obvious stuff (dead
  code, naming, syntax slips) but reliably misses subtler bugs and security
  issues a frontier model catches — treat a `CLEAN` verdict as "nothing
  obvious jumped out," not "safe to skip a real review," for anything
  consequential.
- Reuses `review_diff`'s own diff-selection order (vs HEAD, then staged
  `--cached`, then plain unstaged) so both tools always look at the same
  diff.
- `_call_ollama` sets an explicit `num_ctx: 8192` on every request. Ollama
  defaults to 2048 regardless of the model's real context length, which
  would silently left-truncate any diff near `MAX_CONTEXT_CHARS` and drop
  `PREFILTER_INSTRUCTIONS` entirely — caught by `review_diff` on this
  tool's own first commit before it ever shipped. `temperature: 0.0` is set
  alongside it for a more consistent CLEAN/FLAGGED verdict.
- Verified end-to-end against this repo's own pending `README.md` diff on
  2026-07-24: `prefilter_diff` picked up the real diff and returned a
  `CLEAN` verdict from `qwen2.5-coder:7b` running locally — call and
  response both landed in `ollama_log.jsonl` as expected.
- `qwen2.5-coder:7b` was picked as the default because it's the only
  locally installed model (of qwen2.5-coder, dolphin-mistral, llama3,
  deepseek-coder, plus several custom personas) that reports tool-use/code
  capability rather than plain chat completion — override via `model` for
  a different local model.
- `triage_log` reads an existing log file rather than executing a command
  itself — deliberately, to keep this bridge's subprocess surface limited
  to `git diff` (a fixed, safe command) rather than adding an
  arbitrary-command-execution tool. Redirect a failing command's output to
  a file first (`cmd > out.log 2>&1`), then triage that.
- `triage_log` truncates from the **start** of the log via
  `_truncate_keep_tail`, the opposite of `prefilter_diff`'s
  `_truncate` — a build/test failure is almost always near the end of a
  log (the start is setup noise: dependency resolution, banners), unlike a
  diff where head-truncation is fine. Uses a larger `num_ctx: 24576`
  (`LOG_NUM_CTX`) than `prefilter_diff`'s `8192` since `MAX_LOG_CHARS`
  (40k chars) needs more headroom than a same-ratio scale-up would give —
  `review_diff` flagged that 40k chars could run denser than ~2 chars/token
  on symbol-heavy logs, so `LOG_NUM_CTX` was set with margin rather than
  scaled proportionally to `prefilter_diff`'s ratio.
- `triage_log` requires `repo_path` and resolves `log_path` inside it via
  the same `_resolve_in_repo` containment check `repo-bridge` uses for
  `get_file` — added after `review_diff` flagged the first draft (a bare
  `log_path` with no scoping) as an arbitrary-file-read risk: an
  unscoped path could be pointed at `~/.ssh`, `.env` files, etc.
- `_call_ollama`'s `URLError` handler checks `isinstance(e.reason,
  TimeoutError)` before the generic `OSError` check — `review_diff` caught
  that a real (slow-but-reachable-Ollama) timeout is itself an `OSError`
  subclass, so without checking `TimeoutError` first it was misreported as
  "not reachable - is `ollama serve` running?" instead of a timeout.
- `triage_log` always appends a pointer back to the full `log_path` and
  line/char count alongside the model's summary — a 7B model can
  misidentify the root cause in a complex multi-error log, so the raw log
  stays one `Read` away rather than being fully replaced by the summary.
- Verified against a synthetic pytest log (42 tests, 1 real failure buried
  in passing-test noise): correctly extracted the failing file/line, exact
  `AssertionError`, and relevant traceback lines, ignoring the noise. A
  second synthetic clean-run log correctly returned `NO FAILURE FOUND.`
- `triage_transcript` (added 2026-08-06) reuses `_resolve_in_repo` from
  `triage_log` and `_truncate` from `prefilter_diff` rather than adding new
  helpers — a transcript is closer to a diff than a log for truncation
  purposes (an issue can be anywhere in it, not clustered near the end), so
  head-truncation is the right default, not `triage_log`'s tail-keeping
  one. Verified end-to-end against a synthetic 7-segment transcript
  containing one obvious mid-sentence restart and one filler-heavy stretch
  ("um, so, yeah, so basically, um") — `qwen2.5-coder:7b` flagged both
  correctly with their timestamps. Does not decide `keep_segments` itself,
  by design — `cut_video`'s docstring is explicit that cut decisions belong
  to the agent reading the actual transcript, not a local model summary.

## tn3270-bridge

Exposes six tools backed by [py3270](https://pypi.org/project/py3270/) (which
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
- `read_panel_state(session_id)` — returns a compact JSON summary instead of
  a full screen dump: `{"messages": [{"id", "severity", "text"}, ...],
  "actionable_inputs": [{"label", "row", "col", "hidden"}, ...], "cursor":
  {"row", "col"}}`. `messages` is every line starting with a standard IBM
  message ID (`ICH408I`, `IKJ56650I`, etc.) — RACF/TSO/JES errors and status
  lines all follow that shape, so this generalizes across products instead
  of needing a per-panel lookup table. `actionable_inputs` is every
  unprotected field with a best-guess label pulled from the protected field
  immediately before it (e.g. `"Userid ===>"` next to the input). Use this
  instead of `read_screen` when the agent just needs to know what the
  screen says and where to type, not the full layout.
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
- The first five tools have been exercised against a live TN3270 host (a
  local test mainframe on `localhost:3270`): `connect` + `read_screen`
  pulled back the logon banner, `send_keys("TSO\n")` advanced from the
  logon-type prompt to the TSO/E LOGON screen, `send_function_key("PF3")`
  logged off back to the banner, and structured `read_screen` correctly
  identified the one unprotected field on the banner screen and the hidden
  password fields on the TSO/E LOGON screen.
- `read_panel_state` (added 2026-08-06) has **not** been verified against a
  live host in this environment — no test mainframe was reachable, only unit
  tested against synthetic screen data (a fabricated RACF-style logon
  denial: `ICH70001I`/`ICH408I` message lines plus `Userid`/`Password`
  fields), which it parsed correctly. The message-ID regex is based on IBM's
  documented, standardized message format, and the label heuristic matches
  every real panel layout seen in this repo's earlier live testing, but both
  should be treated as unverified against a real host until run against
  one. `actionable_inputs` labels are a best guess, not authoritative — fall
  back to structured `read_screen` if a label looks wrong.

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
- `get_symbol(repo_path, name, language="", max_results=10)` — finds
  function/class/method/type definitions named `name` and returns their
  source text via [tree-sitter](https://tree-sitter.github.io/tree-sitter/).
  Supports `python`, `javascript`, `typescript`, `tsx`, `go`, `rust`, `java`,
  `ruby`, `c`, and `cpp`. Greps for files that reference `name` first rather
  than parsing the whole repo, and returns every match found (up to
  `max_results`), not just the first.

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
  works generically across languages without per-language field lookups.
  `_find_definitions()` walks the whole tree (including inside already-matched
  nodes, so e.g. two same-named methods in two different classes in one file
  both surface) and results are capped at `max_results`, defaulting to 10.
- C and C++ `function_definition` nodes are the one exception to the
  generic `name`-field lookup above: their identifier is nested inside a
  `declarator` chain (a `pointer_declarator` for pointer return types, etc.)
  ending in a `function_declarator` whose own `declarator` field is the
  actual identifier. `_c_family_function_name()` in `repo_bridge.py` unwraps
  that chain; struct/enum/union/class specifiers in C/C++ do expose a
  `name` field directly and don't need it.
- Verified against this repo (Python) and small standalone fixtures for
  TypeScript, Go, Rust, Java, Ruby, C, and C++ — `get_symbol` correctly
  pulled a function/struct (or class/method) from each, with correct line
  ranges.

## linkedin-bridge

Exposes three tools for managing LinkedIn posts on behalf of the
authenticated member, all via LinkedIn's [Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api):

- `post_to_linkedin(text, visibility="PUBLIC")` — publishes a text post.
  `visibility` is `"PUBLIC"` or `"CONNECTIONS"`. Returns the live post URL on
  success.
- `update_linkedin_post(post_url_or_urn, text)` — replaces the text of an
  existing post. Takes either the live URL or a bare URN. Only needs the
  `w_member_social` scope this app already has.
- `delete_linkedin_post(post_url_or_urn)` — permanently deletes a post.
  Irreversible.

All three publish, edit, or delete under the user's real identity — always
confirm the exact action/text before calling any of them, never call them
unprompted.

Every call is logged to `linkedin_log.jsonl` (gitignored) as an audit trail.

### Setup

1. Create a LinkedIn Company Page, then a developer app at
   [linkedin.com/developers/apps](https://www.linkedin.com/developers/apps)
   associated with it, with the **Share on LinkedIn** product added (grants
   the `w_member_social` scope). The app needs a real privacy policy URL —
   this repo's own consulting site's [privacy page](https://wren-creator.github.io/privacy.html)
   is an example of a minimal one.
2. Add an **Authorized redirect URL** of `http://localhost:8765/callback` on
   the app's Auth tab, and note the Client ID and Client Secret.
3. Put the credentials in `~/.linkedin/.env` (create the file yourself in a
   text editor — don't paste secrets through an agent if avoidable):
   ```
   LINKEDIN_CLIENT_ID=...
   LINKEDIN_CLIENT_SECRET=...
   ```
   then `chmod 600 ~/.linkedin/.env`.
4. Run the one-time OAuth flow:
   ```
   .venv/bin/python linkedin_oauth_setup.py
   ```
   This opens a browser for LinkedIn's consent screen, exchanges the resulting
   code for an access token, fetches the member's person URN, and writes both
   back into `~/.linkedin/.env`.
5. Register the server with Claude Code:
   ```
   claude mcp add linkedin-bridge --scope user -- \
     ~/git/claude-tools/.venv/bin/python ~/git/claude-tools/linkedin_bridge.py
   ```

### Notes

- Standard LinkedIn apps don't get a refresh token without extra approval —
  access tokens last ~60 days. Re-run `linkedin_oauth_setup.py` once one
  expires; `post_to_linkedin` will surface LinkedIn's own error message if a
  call is attempted with an expired token.
- Uses `urllib` from the standard library rather than adding an HTTP client
  dependency, consistent with the rest of this repo's bridges.
- **Solved: the earlier "posts sometimes render truncated" issue was
  LinkedIn's ["little" text format](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/little-text-format).**
  The `commentary` field isn't plain text — it's a small markup language for
  mentions/hashtags, and characters reserved for that markup
  (`` ( ) [ ] { } @ # < > \ * _ ~ ``) must be backslash-escaped to appear as
  literal text. Every post logged during the original investigation was
  checked against this: every single one containing an unescaped `(` or `)`
  rendered truncated in the feed, every one without either character
  rendered in full — 11/11 with no exceptions. `_escape_little_format()`
  now escapes all reserved characters automatically before every
  `post_to_linkedin`/`update_linkedin_post` call, except `#word` sequences
  (left alone so intentional hashtags still render as hashtags). Confirmed
  fixed with a live test post containing parentheses.
- `update_linkedin_post`/`delete_linkedin_post` don't need `r_member_social`
  (LinkedIn's read-back permission, currently closed for new access
  requests) - only reading a post back to verify its content needs that, so
  there's still no way to check a post's live content without a human
  looking at it.
- Verified end-to-end: OAuth flow completed, a real post published
  successfully via `post_to_linkedin`, then updated via `update_linkedin_post`
  (tested with the full URL form) and deleted via `delete_linkedin_post`
  (tested with the bare URN form) - both input styles work.
- The `Linkedin-Version` header is pinned to the previous month
  (`_linkedin_api_version()`), not the current one. LinkedIn versions its
  REST API by month, but a brand-new monthly version isn't always live in
  production right at the start of that month, requests using the current
  month's version have come back with a 426 NONEXISTENT_VERSION error, while
  the previous month's version is always already rolled out.

## youtube-bridge

Exposes five tools for a solo-creator video pipeline: transcribe a raw
recording, mechanically tighten it, cut it down semantically, then upload it
to YouTube on a schedule.

- `transcribe_video(video_path)` — local [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  transcription with timestamps. Writes `<video_path>.transcript.json` and
  returns `[MM:SS - MM:SS] text` lines for Claude to read and reason about
  what to cut.
- `tighten_video(video_path, output_path="")` — runs [auto-editor](https://github.com/WyattBlue/auto-editor)
  for a mechanical first pass (cuts silence/dead air). Doesn't understand
  meaning — pair with `cut_video` for semantic cuts (flubbed takes, restarts,
  rambling).
- `cut_video(video_path, keep_segments, output_path="")` — given a list of
  `[start, end]` second ranges to keep (picked by Claude from the
  transcript), re-encodes and concatenates via ffmpeg for frame-accurate
  cuts. Claude decides *what* to cut by reading the transcript; this tool
  only executes the mechanical trim.
- `queue_video_for_upload(video_path, title, description, tags, category_id="28", publish_at="")` —
  uploads as `privacyStatus: "private"` with `status.publishAt` set, so
  **YouTube itself** flips the video public at that timestamp — no daemon or
  cron needed on this end. If `publish_at` is omitted, computes the next open
  slot from `YOUTUBE_POST_TIMES` (comma-separated `HH:MM`, local time — one
  entry = 1/day, two = 2/day), reading/advancing state in
  `youtube_schedule_state.json` so repeated calls in one batch-recording
  session spread out across days without double-booking. On success, moves
  the source file into a `posted/` subfolder (date-prefixed) so it drops out
  of the pending queue.
- `list_pending_videos(folder="")` — lists video files in `folder` (default
  `YOUTUBE_VIDEO_DIR`) not yet moved to `posted/`, i.e. what's left in a
  batch day's queue.

`queue_video_for_upload` schedules a video to go **publicly live with no
further confirmation** at `publish_at` — always confirm title/description/
tags/timing with the user before calling it, never call it unprompted, same
rule as `linkedin-bridge`.

Every call is logged to `youtube_log.jsonl` (gitignored).

### Setup

1. Install `ffmpeg` (`brew install ffmpeg`) and this project's Python
   dependencies (shared `.venv`, `faster-whisper` and `auto-editor` are in
   `requirements.txt`):
   ```
   cd ~/git/claude-tools
   .venv/bin/pip install -r requirements.txt
   ```
2. Create a Google Cloud project, enable the **YouTube Data API v3**, and
   create an OAuth client of type **Desktop app**. Add
   `http://localhost:8766/callback` as an authorized redirect URI.
3. Put the client credentials in `~/.youtube/.env`:
   ```
   YOUTUBE_CLIENT_ID=...
   YOUTUBE_CLIENT_SECRET=...
   YOUTUBE_VIDEO_DIR=/path/to/your/raw-recordings-folder
   YOUTUBE_POST_TIMES=10:00,17:00
   ```
   then `chmod 600 ~/.youtube/.env`.
4. Run the one-time OAuth flow (forces `access_type=offline&prompt=consent`
   so Google actually returns a refresh token):
   ```
   .venv/bin/python youtube_oauth_setup.py
   ```
   This writes `YOUTUBE_REFRESH_TOKEN` back into `~/.youtube/.env` and prints
   the authorized channel name to confirm you authorized the right account.
5. Register the server with Claude Code:
   ```
   claude mcp add youtube-bridge --scope user -- \
     ~/git/claude-tools/.venv/bin/python ~/git/claude-tools/youtube_bridge.py
   ```
6. Restart Claude Code / reload the window.

### Notes

- Unlike LinkedIn's ~60-day access token, Google's refresh token doesn't
  expire from normal use — `youtube_bridge.py` exchanges it for a fresh
  access token on every call rather than caching one, so
  `youtube_oauth_setup.py` should only need to run once.
- `publishAt` requires `privacyStatus: "private"` at upload time (YouTube
  rejects a scheduled `public`/`unlisted` upload) — the tool always sends
  `"private"`, which is what triggers YouTube's own scheduling behavior.
- Uses `urllib` from the standard library for OAuth and the resumable-upload
  protocol, consistent with the rest of this repo's bridges — no
  `google-api-python-client` dependency. The upload streams the video file
  from disk (a file object passed as `data`, not `read_bytes()`) so large
  recordings don't get fully buffered into memory; it's still a single PUT
  rather than chunked with per-chunk retry, which would matter more for
  very large files or flaky connections.
- `queue_video_for_upload` rejects `publish_at` values less than 5 minutes
  out, and `cut_video` validates `keep_segments` (sorted, non-overlapping,
  `end > start`) before touching ffmpeg — both fail fast with a clear error
  instead of wasting an upload/encode on bad input.
- `_run()` catches `FileNotFoundError` for missing binaries (`ffmpeg`,
  `ffprobe`, `auto-editor`) and returns a clean "not found — is it installed
  and on PATH?" error through the normal returncode-check path, instead of
  crashing the tool call with a raw traceback.
- `cut_video` re-encodes at every cut (`trim`+`concat` filter graph) rather
  than stream-copying, trading some encode time for frame-accurate
  boundaries — stream-copy cuts only land on keyframes, which would make
  Claude's semantic cut points imprecise.
- Default `category_id` is `"28"` (Science & Technology) — override per call
  if a video fits better under `"27"` (Education) or `"26"` (Howto & Style).
- Scheduling logic (`_next_publish_slot`) verified standalone: queuing 5
  videos in a row against `YOUTUBE_POST_TIMES=10:00,17:00` produced
  2026-07-18 10:00, 2026-07-18 17:00, 2026-07-19 10:00, 2026-07-19 17:00,
  2026-07-20 10:00 — correct 2/day spread with no double-booking.
- Full pipeline verified end-to-end 2026-07-18/19 (transcribe → tighten →
  queue → real OAuth upload), including three fixes found along the way:
  - `tighten_video` called bare `auto-editor` via `subprocess.run`, which
    only exists in this project's `.venv/bin`, not on the MCP server's
    inherited `PATH`. `AUTO_EDITOR_BIN` now resolves it explicitly
    (`shutil.which` first, falling back to the venv's own `bin/` next to
    `sys.executable`).
  - macOS Screenshot/Screen Recording filenames insert a narrow no-break
    space (`U+202F`) before AM/PM, visually identical to a normal space but
    byte-different — any retyped (vs. copy-pasted) path silently failed
    `Path.exists()`. `_resolve_video_path()` now falls back to a
    whitespace-normalized filename match within the same directory across
    all four file-taking tools.
  - The first real upload flickered and had audio skips. Two compounding
    causes, both in `tighten_video`'s default `auto-editor` invocation:
    (1) macOS screen recordings are variable-frame-rate, and left to its
    default auto-editor timed the output off the source's *average* fps,
    landing on an arbitrary non-standard rate (52.41fps); (2) auto-editor's
    default bitrate (~1.4Mbps observed) was far too low for a high-res
    (3024x1898) screen recording with sharp text, producing visible
    compression-artifact flicker. Fixing the frame rate alone did not
    resolve it — the low bitrate was the dominant cause. `tighten_video`
    now passes `--frame-rate 60` (`TIGHTEN_OUTPUT_FPS`) and `--video-bitrate
    10M` (`TIGHTEN_VIDEO_BITRATE`, comfortably above the source's own
    ~4Mbps). Caught only after publishing — a private/scheduled upload with
    a bad encode isn't something `youtube-bridge` can fix or delete itself
    (no update/delete tool exists yet, unlike `linkedin-bridge`); both bad
    uploads had to be deleted by hand in YouTube Studio before their
    scheduled publish time.
- **`tighten_video` disabled 2026-07-19**, the fps/bitrate fix above did not
  actually resolve the flicker. Two more videos processed through the fixed
  `tighten_video` still showed the artifact and were unusable. Isolated the
  cause with an A/B test: the same recording, uploaded completely raw and
  unedited with no auto-editor pass at all, had no flicker. So the artifact
  comes from auto-editor's re-encode step itself, not the source capture and
  not YouTube's transcode, but the exact cause within auto-editor isn't
  identified yet. `tighten_video` now returns an explanatory error instead
  of running, rather than silently producing unusable output.
  `transcribe_video`, `cut_video`, and `queue_video_for_upload` are
  unaffected. Roadmapped below to revisit once the real cause is found.
- Deliberately excluded from `mcpo_config.json`, same rationale as
  `linkedin-bridge` (see mcpo Notes below) — publishing tools stay MCP-only
  so the "confirm before calling" rule can't be bypassed by an HTTP client
  holding the proxy's API key.

## slack-digest-bridge

Exposes one tool that fetches a Slack thread server-side and digests it with
a local Ollama model, so the raw thread never has to land in the agent's own
context the way it does through the hosted Slack connector:

- `digest_thread(thread_url, model="qwen2.5-coder:7b")` — pass a Slack
  permalink to any message in the thread (a message's "Copy link" action) —
  works for both the parent message's link and a reply's link. Fetches the
  thread via `conversations.replies` and returns `DECISIONS`/`ACTION_ITEMS`/
  `BLOCKERS` sections instead of the raw messages.

Every call is logged to `slack_digest_log.jsonl` (gitignored) as an audit
trail.

### Setup

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) →
   **Create New App** → **From a manifest**, pick the target workspace, and
   paste:
   ```yaml
   display_information:
     name: claude-tools-digest
     description: Read-only bot for Slack thread digests via claude-tools
   features:
     bot_user:
       display_name: claude-tools-digest
       always_online: false
   oauth_config:
     scopes:
       bot:
         - channels:history
         - channels:read
         - groups:history
         - groups:read
   settings:
     org_deploy_enabled: false
   ```
2. On the app's **OAuth & Permissions** page, click **Install to Workspace**
   and approve. Copy the **Bot User OAuth Token** (`xoxb-...`) shown after —
   ignore the separate **App Credentials** page (Client ID/Secret/Signing
   Secret); those are for building an external OAuth authorize flow, not
   needed for an app that only installs into your own workspace.
3. Invite the bot to every channel you want digested — it can't read a
   channel's history until it's a member, even with the scopes granted:
   `/invite @claude-tools-digest` in each one.
4. Put the token in `~/.slack/.env` (create it yourself):
   ```
   SLACK_BOT_TOKEN=xoxb-...
   ```
   then `chmod 600 ~/.slack/.env`.
5. Register the server with Claude Code:
   ```
   claude mcp add slack-digest-bridge --scope user -- \
     ~/git/claude-tools/.venv/bin/python ~/git/claude-tools/slack_digest_bridge.py
   ```
6. Restart Claude Code / reload the window.

### Notes

- This exists because the hosted `claude_ai` Slack connector already returns
  raw thread content into the agent's context by the time the agent sees
  it — it can't be the fetch path for a tool meant to keep raw messages out
  of context, so this bridge holds its own bot token and fetches
  server-side instead.
- Scopes are read-only on purpose (`*:history`/`*:read`, no `chat:write`) —
  this bot never posts, it only ever reads threads it's been invited to.
- Message text can contain raw Slack IDs (`<@U12345>` for a user,
  `<#C12345|name>` for a channel) since the bot doesn't have `users:read` —
  the digest instructions tell the model to carry those through as-is
  rather than guess a display name.
- `conversations.replies` is called with `limit=200` and no pagination — if
  a thread has more than 200 replies, only the first batch is digested and
  the response says so. Not built out further since no thread in this
  workspace is anywhere near that size yet.
- Verified end-to-end against a real message in `#claude-tools`
  (`conversations.replies` fetch, auth, and Ollama round trip all
  confirmed working), and separately against a synthetic 6-message thread
  (decision, action item with a raw `<@U123>` mention, a `channel_join`
  system message, and a bare `:+1:` reaction) to check the filtering and
  extraction logic: the join event and the reaction were correctly dropped,
  the decision and action item were both extracted correctly with the
  mention preserved verbatim. One real miss in that same test — an open
  question in the synthetic thread ("not sure if we need a separate channel
  for the mobile team") wasn't picked up as a `BLOCKER`. Same caveat as
  `ollama-bridge`'s other triage tools: a 7B model's first pass, not a
  guarantee, re-read the thread yourself (`slack_read_thread`) for anything
  where a missed open question would actually matter.

## image-bridge

Exposes one tool backed by Gemini's native image model
(`gemini-2.5-flash-image`), reusing the same `GEMINI_API_KEY` as
`gemini-bridge`:

- `generate_image(prompt, output_path, reference_image_paths=[])` —
  generates an image and saves it to `output_path` (absolute path, `.png` or
  `.jpg`). Pass `reference_image_paths` (absolute paths to existing images)
  to guide style, character likeness, and composition against those
  references, e.g. an existing book cover, so a series of illustrations
  actually looks like one book instead of restarting the style from scratch
  on every call. Returns the saved path on success.

Every call is logged to `log.jsonl` (shared with `gemini-bridge`, gitignored)
as an audit trail.

### Setup

1. Same `GEMINI_API_KEY` as `gemini-bridge` (see its Setup section) — no
   separate credential needed.
2. Install this project's dependencies (shared `.venv`, `google-genai` is in
   `requirements.txt`):
   ```
   cd ~/git/claude-tools
   .venv/bin/pip install -r requirements.txt
   ```
3. Register the server with Claude Code:
   ```
   claude mcp add image-bridge --scope user -- \
     ~/git/claude-tools/.venv/bin/python ~/git/claude-tools/image_bridge.py
   ```
4. Restart Claude Code / reload the window.

### Notes

- Uses the `google-genai` SDK directly (not the `gemini` CLI `gemini-bridge`
  shells out to) — the CLI has no image-generation flag, so this calls
  `models.generate_content` against `gemini-2.5-flash-image` itself,
  multimodal in: text prompt plus zero or more reference images as inline
  bytes, image out: the first `inline_data` part found in the response.
- **Blocked on billing as of 2026-08-06.** The free tier of the API key in
  `~/.gemini/.env` has a hard `0` request/token quota specifically for
  `gemini-2.5-flash-image` (confirmed via a live test call: clean HTTP round
  trip, clear `429 RESOURCE_EXHAUSTED` response naming that exact quota) —
  every other quota on that key allows normal usage, this one model's free
  allowance is zero, not low. The code path itself is verified working end
  to end (request sent, error correctly parsed and returned, no image
  written), it just cannot succeed until billing is enabled on the Google
  AI Studio / Cloud project tied to that key.
- No image was actually generated or shipped anywhere as of this note —
  everything above the Blocked line is verified-working plumbing, not a
  verified-working image.

## discord-bridge

Exposes four tools for reading, posting to, and managing Discord text
channels under a bot's own identity, via Discord's REST API:

- `list_channels(guild_id)` — lists the text/announcement channels in a
  server, so you can find a channel's numeric ID from its name before calling
  the other tools.
- `read_channel(channel_id, limit=20)` — returns the most recent messages
  (newest first), including attachment URLs. `limit` is capped at 100
  (Discord's own per-request max).
- `post_message(channel_id, content)` — posts a message under the bot's
  identity. Posts live and immediately, visible to everyone in the channel —
  always confirm the exact channel and text with the user before calling
  this, never call it unprompted, same rule as `linkedin-bridge` and
  `youtube-bridge`.
- `create_channel(guild_id, name, topic="")` — creates a new text channel in
  a server. Requires the bot to have Manage Channels permission. Creates a
  real, visible channel immediately — always confirm the exact server and
  channel name with the user first, never call it unprompted, same rule as
  `post_message`.

Every call is logged to `discord_log.jsonl` (gitignored) as an audit trail.

### Setup

1. Create an app at [discord.com/developers/applications](https://discord.com/developers/applications)
   → **New Application**. On the **Bot** tab, click **Reset Token** to reveal
   a bot token, and turn on the **Message Content Intent** toggle under
   Privileged Gateway Intents — without it, `content` comes back empty on
   messages the bot didn't author, even with the right channel permissions.
2. On the **OAuth2 → URL Generator** page, check the `bot` scope, then under
   Bot Permissions check **View Channels**, **Send Messages**, **Read
   Message History**, and **Manage Channels** (needed for `create_channel`).
   Open the generated URL and invite the bot to your server. If the bot is
   already invited without Manage Channels, grant it the role permission
   directly in Server Settings → Roles instead of re-inviting.
3. Put the token in `~/.discord/.env` (create it yourself):
   ```
   DISCORD_BOT_TOKEN=...
   ```
   then `chmod 600 ~/.discord/.env`.
4. Register the server with Claude Code:
   ```
   claude mcp add discord-bridge --scope user -- \
     ~/git/claude-tools/.venv/bin/python ~/git/claude-tools/discord_bridge.py
   ```
5. Restart Claude Code / reload the window.

### Notes

- Built after searching for an existing Discord MCP server came up empty —
  the ones found were either read-only (webhook posting, no channel
  reading), abandoned, or required more setup than just standing up a
  minimal bridge in this repo's existing style. Same pattern as
  `slack-digest-bridge`: own bot token in a dotfile, stdlib `urllib` only, no
  new dependency in `requirements.txt`.
- Uses Discord API version `v10` (`https://discord.com/api/v10`), the
  current stable version as of this writing.
- `_format_discord_error` parses Discord's JSON error body (`message` +
  numeric `code`) and adds a plain-English hint for the common failure
  modes: `401` (bad token), `403` (bot present but missing a permission,
  or Message Content Intent off), `404` (bot not actually in that
  server/channel, or a wrong ID).
- Deliberately excluded from `mcpo_config.json`, same rationale as
  `linkedin-bridge`/`youtube-bridge` — `post_message` publishes visibly
  under a real identity, so it stays MCP-only where the "confirm before
  posting" rule is enforced by Claude Code's own tool-call flow, not
  bypassable by an HTTP client holding the proxy's API key.
- `list_channels` has been verified against a live server. `create_channel`
  is new and not yet verified against a live server/bot with Manage
  Channels permission — code path mirrors the other tools' verified
  request/error-handling pattern, but treat it as unverified until run
  once for real.

## github-audit-bridge

Exposes three tools for auditing and fixing Dependabot coverage across every
repo on a GitHub account, backed by the `gh` CLI:

- `audit_dependabot_coverage(owner="wren-creator")` — checks every
  non-archived repo under `owner` for vulnerability alerts, automated
  security-fix PRs, and a `.github/dependabot.yml` (scheduled version-update
  config), in parallel. Returns one compact table plus a "needs attention"
  line listing repos with alerts off.
- `list_open_work(owner="wren-creator")` — lists open issues and open pull
  requests (fetched separately, not double-counted) for every repo that has
  any, skipping repos with nothing open.
- `enable_dependabot(owner, repo)` — turns on vulnerability alerts and
  automated security-fix PRs for one repo. Idempotent, safe to call on a repo
  that already has it on. Does not create a `dependabot.yml` — that's a
  separate, per-repo config this tool doesn't write.

All three run entirely against the GitHub REST API through `gh api` — no LLM
calls, no repo cloning. Built to replace looping raw `gh api` calls by hand
for this kind of account-wide check, which burns a lot of turns doing the
same handful of lookups per repo.

### Setup

1. `gh auth login` with a token scoped for repo security-events access (the
   account's existing `gh` auth, if already logged in, is reused as-is — no
   separate credential file).
2. Register the server with Claude Code:
   ```
   claude mcp add github-audit-bridge --scope user -- \
     ~/git/claude-tools/.venv/bin/python ~/git/claude-tools/github_audit_bridge.py
   ```
3. Restart Claude Code / reload the window.

### Notes

- Built 2026-08-10 after a manual 41-repo Dependabot audit for
  `wren-creator` burned a lot of turns looping `gh api` calls one repo at a
  time. Verified against that same account: `audit_dependabot_coverage`
  reproduced the manual audit's numbers exactly (36 repos on, 5 off), and
  `enable_dependabot` was used for real to turn on the 5 that were off.
- `vulnerability-alerts` returns `204`/exit 0 when on and `404`/exit 1 when
  off — `_check_repo_security` reads that off `gh`'s own returncode rather
  than parsing a body, since the endpoint has no JSON body either way.
  `automated-security-fixes` does return a body (`{"enabled": true/false}`),
  parsed instead of trusting the HTTP status — an earlier version of the
  by-hand audit that led to this tool mistakenly treated any 2xx as
  "enabled" and got it wrong for repos with fixes actually off.
- `.github/dependabot.yml` presence is checked via the exit code of `gh api
  repos/.../contents/...`, not by inspecting stdout — `gh api`'s 404 error
  body (`{"message":"Not Found",...}`) prints to stdout, not stderr, so an
  earlier draft that checked "is stdout non-empty" got a false "present" on
  every single repo, including ones with no config file at all. Caught by
  hand before this tool existed; the fix carried forward into
  `_check_repo_security` directly.
- Excluded from `mcpo_config.json` on purpose, same rationale as
  `linkedin-bridge`/`youtube-bridge`/`discord-bridge` — `enable_dependabot`
  changes real settings on a live GitHub repo. Keeping it MCP-only means that
  change only ever happens through Claude Code's own guarded tool-call flow,
  not an HTTP client holding the proxy's API key.

## playwright-bridge

Exposes eight tools backed by [Playwright](https://playwright.dev/python/) for
driving a real browser mid-session, e.g. to visually verify a UI/CSS fix or
confirm a network dependency is (or isn't) actually being hit:

- `launch(browser="chromium", headless=True, viewport_width=0, viewport_height=0)`
  — starts a browser, returns a `session_id`. `browser` is `"chromium"`,
  `"firefox"`, or `"webkit"`. Every request the page makes from this point
  until `close()` is recorded — see `get_requests`.
- `goto(session_id, url, wait_until="load")` — navigates. `wait_until` is
  `"load"`, `"domcontentloaded"`, `"networkidle"`, or `"commit"`. Returns
  JSON `{"url", "title", "status"}`.
- `evaluate(session_id, script)` — runs JavaScript in the page and returns
  the JSON-encoded result. `script` is an expression or function body, same
  as Playwright's own `page.evaluate()` — a script returning a Promise is
  automatically awaited. The general-purpose tool for reading DOM/computed
  style state or calling into a page's own JS (e.g. a dynamic
  `import('/js/whatever.js')`) that the other tools don't have a shape for.
- `screenshot(session_id, output_path, full_page=False, selector="")` —
  saves a PNG to `output_path` (absolute path); read it back with Claude
  Code's own `Read` tool to view it. With `selector` set, screenshots just
  that element.
- `get_requests(session_id, url_contains="")` — every network request made
  since `launch()`, as JSON `[{"url", "method", "resource_type", "status"},
  ...]`. With `url_contains` set, filters to matching URLs — e.g. confirm a
  CDN dependency was actually removed by checking zero requests contain
  `"fonts.googleapis.com"`, without needing devtools open.
- `click(session_id, selector, timeout_ms=5000)` / `fill(session_id,
  selector, text, timeout_ms=5000)` — basic interaction, CSS or Playwright
  `text=`/`role=` selector syntax.
- `close(session_id)` — closes the browser and frees its resources.

Sessions live in memory for the lifetime of the server process (one per
Claude Code session) — `close` any session you're done with rather than
letting it leak. Every call is logged to `playwright_log.jsonl` (gitignored).

### Setup

1. Install this project's dependencies (shared `.venv`, `playwright` is in
   `requirements.txt`):
   ```
   cd ~/git/claude-tools
   .venv/bin/pip install -r requirements.txt
   ```
2. Install the browser binaries **once** — this is the actual fix for
   "waiting on a temporary install every session" (see Notes below). Start
   with Chromium alone; it covers most verification needs and, as of this
   writing, was already cached from earlier unrelated `npx playwright`
   usage, so it costs no download at all. Add Firefox/WebKit later
   (`.venv/bin/playwright install firefox webkit`) when on a connection
   that isn't metered/cellular — each is 100MB+ and this repo's own setup
   session stalled indefinitely trying to fetch Firefox over a cellular
   hotspot (see Notes):
   ```
   .venv/bin/playwright install chromium
   ```
3. Register the server with Claude Code:
   ```
   claude mcp add playwright-bridge --scope user -- \
     ~/git/claude-tools/.venv/bin/python ~/git/claude-tools/playwright_bridge.py
   ```
4. Restart Claude Code / reload the window.

### Notes

- Built 2026-08-11 after verifying a font-loading fix for web3270 required
  ad-hoc `npx playwright install chromium firefox` mid-session — Firefox's
  binary took over 20 minutes to fetch through that ephemeral path and never
  finished, while Chromium happened to already be cached from an earlier
  unrelated session. `npx` re-resolves and re-fetches `playwright` itself
  into a fresh `~/.npm/_npx/<hash>/` directory essentially every time it's
  invoked from a new working directory, so nothing about that install
  persists or speeds up the next one. A real dependency in this repo's
  shared `.venv`, installed once, does.
- Browser binaries cache at `~/Library/Caches/ms-playwright/`, keyed by
  browser version, **not** per-project — this is the same cache directory
  Node's `playwright`/`@playwright/test` packages use on the same machine,
  so a Chromium version already fetched by either language's tooling is
  reused rather than re-fetched. Installing here once covers this bridge
  permanently; it does not need to be redone per project or per session.
- Uses Playwright's **sync** API (`playwright.sync_api`), not `async_api` —
  every tool function here is a plain synchronous function, consistent with
  the rest of this repo's bridges (subprocess-based, no asyncio event loop
  already running that would require the async variant instead).
- `get_requests` exists specifically for the "did a network dependency
  actually get removed" class of check — the alternative (asking a human to
  open devtools, or scraping proxy/server logs) is slower and less precise
  than recording every request Playwright already sees pass through the
  page.
- `evaluate` is deliberately the most general tool rather than adding
  narrower ones (e.g. a dedicated "check computed font-family" tool) — it
  covers arbitrary DOM/JS inspection including calling directly into a
  page's own ES module exports via a dynamic `import()`, which is what
  verifying the web3270 pipe-rendering fix actually needed (feeding a
  synthetic screen through the real client-side `renderLiveScreen()`
  function, not a reimplementation of it).
- Not yet added to `mcpo_config.json` — `click`/`fill` can act on real,
  arbitrary web pages if pointed at one, closer to `tn3270-bridge`'s
  `send_keys` (interacts with a live target) than a pure read tool, and
  there's no immediate need for HTTP-proxied access from a non-MCP harness.
  Revisit if that need comes up.
- Verified end-to-end against a live `web3270` Docker stack, calling this
  bridge's own functions directly (not just the ad-hoc Node/Playwright
  script that motivated building it): `launch("chromium")` started headless
  Chromium from the already-cached binary (no download), `goto` loaded the
  app and returned `{"url", "title", "status": 200}`, `get_requests(...,
  url_contains="fonts.googleapis.com")` correctly returned `[]`, `evaluate`
  read back `document.title`, `screenshot` produced a real, correctly
  rendered PNG of the app UI, and `close` freed the session cleanly.
- Getting a *working* `.venv` for this bridge was its own saga, worth
  recording since it's the exact pain this bridge exists to eliminate going
  forward: over a cellular hotspot connection, `pip install playwright`
  first failed outright after ~15 minutes with `ConnectionResetError`
  (silently — the driving command was piped through `tail`, so pip's own
  non-zero exit code was masked by `tail`'s exit 0; don't trust a piped
  command's reported exit status for a slow install, check
  `pip show <package>` for a real `Version:` line instead), then a retry
  with `--retries 10 --timeout 60` looked stuck for another ~15 minutes
  (same TCP connection, zero new data) before turning out to just be
  crawling at ~69 kB/s through a 42.5MB wheel (`playwright`'s Python
  package bundles its own driver) — it finished on its own, unstuck, right
  as a second kill/retry was about to be triggered. Lesson: a slow-but-
  `ESTABLISHED` connection on a metered/cellular link can look identical to
  a dead one; check for forward progress (changing socket ports on retry,
  growing output) before killing and restarting something that's actually
  fine.
- **Firefox and WebKit are deliberately not installed yet** — each browser
  binary is 100MB+, and the connection issues above make that an expensive
  thing to force on a cellular connection. `playwright install firefox
  webkit` (see Setup) is a same-day, low-risk follow-up once on a real
  connection; `launch(browser="firefox"|"webkit")` will fail with a clear
  Playwright "executable doesn't exist" error until then, not a silent
  wrong result.

## mcpo proxy

Fronts `gemini-bridge`, `tn3270-bridge`, and `repo-bridge` with
[`mcpo`](https://github.com/open-webui/mcpo), so tool-calling harnesses that
don't speak MCP natively (e.g. Ollama or llama.cpp-based agents) can call
these tools over plain HTTP/OpenAPI instead. `linkedin-bridge`,
`youtube-bridge`, `discord-bridge`, and `github-audit-bridge` are
deliberately excluded - see each one's Notes.

### Setup

1. Install this project's dependencies (shared `.venv`, `mcpo` is in
   `requirements.txt`):
   ```
   cd ~/git/claude-tools
   .venv/bin/pip install -r requirements.txt
   ```
2. Run it, pointing at `mcpo_config.json` and picking a real API key (not
   the placeholder below):
   ```
   .venv/bin/mcpo --port 8000 --api-key "your-own-key-here" --config mcpo_config.json
   ```
3. Each server's tools are now live at `http://localhost:8000/<server-name>/<tool-name>`
   (e.g. `http://localhost:8000/repo-bridge/list_structure`), with
   interactive OpenAPI docs per-server at `http://localhost:8000/<server-name>/docs`
   and a combined spec at `http://localhost:8000/openapi.json`. Every actual
   tool call requires `Authorization: Bearer <api-key>`; the docs/spec
   endpoints themselves are intentionally public (mcpo's default behavior).

### Notes

- `linkedin-bridge` is excluded from `mcpo_config.json` on purpose.
  `post_to_linkedin`'s "always confirm the exact text with the user first"
  rule is something Claude Code follows as an instruction, not something the
  proxy enforces - any HTTP client holding the `--api-key` could otherwise
  trigger a real, public LinkedIn post with no confirmation step. Keeping it
  MCP-only means posting only ever happens through Claude Code's own
  guarded tool-call flow.
- Verified end-to-end: started mcpo with all three servers, confirmed a real
  `list_structure` call succeeds with the API key and returns 401 without
  it, and confirmed the (intentionally public) `openapi.json`/`/docs`
  endpoints are reachable either way.

## ollama-agent

A standalone chat loop (`ollama_agent.py`, not an MCP server itself) that lets
a local Ollama model actually call the tools `mcpo` fronts -
`gemini-bridge`, `tn3270-bridge`, `repo-bridge` - over plain HTTP. The DIY
alternative to routing through Open WebUI: no extra service, just a script
that turns `mcpo`'s OpenAPI spec into Ollama's own function-calling `tools`
format and dispatches whatever the model calls.

- Fetches each server's own `openapi.json` from `mcpo` (there's no single
  combined spec with real paths - see Notes) and converts it into Ollama's
  `tools` format.
- Runs a normal `/api/chat` loop: sends the conversation plus `tools`,
  executes any tool call against `mcpo`, feeds the result back, repeats
  until the model gives a plain-text final answer (capped at 8 tool-call
  rounds to avoid a runaway loop).
- Falls back to parsing a JSON-shaped call out of `message.content` when the
  model answers with one instead of populating Ollama's structured
  `tool_calls` field - see Notes, this is qwen2.5-coder's actual default
  behavior locally, not a rare edge case.

### Setup

1. Have `mcpo` already running against `mcpo_config.json` (see the mcpo
   proxy section above):
   ```
   .venv/bin/mcpo --port 8000 --api-key "your-key" --config mcpo_config.json
   ```
2. Put the same key mcpo was started with in `~/.mcpo/.env` (create it
   yourself):
   ```
   MCPO_API_KEY=your-key
   MCPO_URL=http://localhost:8000
   ```
   then `chmod 600 ~/.mcpo/.env`.
3. Run it:
   ```
   .venv/bin/python ollama_agent.py                       # interactive REPL
   .venv/bin/python ollama_agent.py "your one-shot prompt"
   ```
   `--model` (default `qwen2.5-coder:7b`), `--mcpo-url`, `--api-key`, and
   `--num-ctx` (default 8192) override the defaults/dotfile. `--list-tools`
   prints the loaded tools and exits without calling Ollama; the same list
   is available mid-conversation via `/tools`, in either interactive mode
   or as the one-shot prompt. `--offline` excludes `gemini-bridge` (or any
   other server listed in `NETWORK_DEPENDENT_SERVERS`) from the loaded
   tools entirely, rather than just telling the model not to use it - see
   Notes, a soft instruction alone wasn't enough.

Every tool call (name, arguments, result) is logged to
`ollama_agent_log.jsonl` (gitignored).

### Notes

- `mcpo`'s combined `/openapi.json` is just an index page linking to each
  server's own docs - its `paths` object is empty. The real per-tool
  schemas live at `/<server-name>/openapi.json`, one FastAPI sub-app per MCP
  server. `fetch_mcpo_tools` reads `mcpo_config.json` directly for the
  server name list (the same config `mcpo` itself was started with) instead
  of trying to discover servers from the index page, then fetches each
  server's own spec and resolves its `$ref` schemas.
- Confirmed live 2026-08-11 against both `qwen2.5-coder:7b` and `:14b`:
  neither actually populates Ollama's structured `message.tool_calls`
  field, despite both reporting `tools` in their `ollama show`
  capabilities - they answer with a bare `{"name": ..., "arguments":
  {...}}` JSON object in `message.content` instead, sometimes with more
  than one such object printed back to back as plain text rather than one
  valid JSON value. `_fallback_tool_calls` scans `content` for every `{`
  and tries `json.JSONDecoder().raw_decode()` from there, which stops at
  the first balanced close-brace and ignores anything before/after -
  handles markdown fences, multiple sequential calls, and trailing
  commentary without special-casing any of them.
- Also seen live: the model dropping a tool's `<server>__` prefix (e.g.
  calling `get_file` instead of the registered `repo-bridge__get_file`).
  `_fallback_tool_calls` resolves a bare name back to its full one when
  exactly one server exposes it, and stays silent (falls through as
  unrecognized) rather than guessing when a bare name is ambiguous across
  servers.
- The system prompt explicitly warns against placeholder paths like
  `/path/to/repo` - without it, a prompt that didn't spell out the repo
  path verbatim got a hallucinated placeholder path passed straight to a
  real tool call. Always state the real absolute path in the prompt; the
  warning reduces but doesn't eliminate this.
- `num_ctx` is set explicitly to 8192, same reasoning as
  `ollama_bridge.py`'s `_call_ollama` - Ollama defaults to 2048 regardless
  of a model's real context length, which would silently truncate the tool
  schemas and system prompt before the conversation even starts.
- `:14b` ran noticeably slower than `:7b` on this (CPU-only, no local GPU)
  hardware - `:7b` is the default for interactive use; pass `--model
  qwen2.5-coder:14b` when latency isn't a concern.
- Verified end-to-end 2026-08-11: `repo-bridge__list_structure` and
  `repo-bridge__get_file`, both via `qwen2.5-coder:7b` against a live
  `mcpo` instance fronting this repo's own three servers - real tool calls
  dispatched, real results fed back, and a correct final answer synthesized
  from the actual file contents (`DEFAULT_MODEL = "qwen2.5-coder:7b"` read
  back out of `ollama_bridge.py` itself).
- `--offline` exists because a soft instruction wasn't enough. Without it,
  a prompt that didn't push the model toward a specific local tool got it
  calling `gemini-bridge__ask_gemini` eight times in a row (the full
  `MAX_TOOL_ITERATIONS` budget), rephrasing the same question each time
  instead of reaching for `repo-bridge__get_file` - caught live 2026-08-11
  while testing the VS Code integration below, where this failure mode
  matters most since `gemini-bridge` is exactly the tool that can't work
  when there's actually no internet. `--offline` removes
  `NETWORK_DEPENDENT_SERVERS` from the tool list before it's ever sent to
  Ollama, so the model can't reach for it regardless of what it decides -
  a hard exclusion instead of a hopeful one, consistent with not trusting
  this model to reliably follow soft instructions elsewhere in this file.
  Re-ran the identical prompt that triggered the original loop with
  `--offline` added: one `repo-bridge__get_file` call, correct answer.
- Same exclusions as the `mcpo proxy` section above -
  `linkedin-bridge`/`youtube-bridge`/`discord-bridge`/`github-audit-bridge`
  aren't in `mcpo_config.json`, so this script can't call them either.

### VS Code integration (offline fallback)

VS Code **User Tasks** (`~/Library/Application Support/Code/User/tasks.json`
on macOS, not committed here - personal-machine config, same tier as
`keybindings.json`) wire `ollama_agent.py` into the editor for a "Claude
Code, but works with no internet" fallback - built after weighing a real
VS Code extension against this (see Roadmap below) and finding the
extension wasn't worth the new code/maintenance for something only used
during outages. Three tasks, run via **Command Palette → Tasks: Run
Task**:

- **Ollama Offline: Start mcpo** - starts `mcpo` sourcing `~/.mcpo/.env`
  for the key, same command as the mcpo proxy section above. Run this
  first - the other two tasks will surface `ollama_agent.py`'s own clear
  error ("is `mcpo` running?") without it.
- **Ollama Offline: Ask about selection** - runs `ollama_agent.py --offline`
  with a one-shot prompt built from the active file's absolute path and
  containing directory, plus the current selection (VS Code's
  `${file}`/`${fileDirname}`/`${selectedText}` variables), so the model
  gets real context up front instead of the placeholder-path hallucination
  risk noted above. Uses `"type": "process"` rather than `"shell"` so the
  selected code is passed as one real argv element, not concatenated into
  a shell command line - arbitrary quotes/backticks/`$` in a selection
  can't break or inject into the command. Bound to `cmd+alt+o` (Option key
  on a Mac keyboard) in this machine's personal VS Code `keybindings.json`;
  remap it there if it collides with something.
- **Ollama Offline: Open REPL** - launches `ollama_agent.py --offline` with
  no other args, for general interactive offline chat.

No changes to `ollama_agent.py` itself beyond adding `--offline` (see
Notes above) - all three tasks otherwise use it exactly as documented.

**Why User Tasks and not a workspace `.vscode/tasks.json`** (what this
repo shipped first, 2026-08-11): VS Code only loads `.vscode/tasks.json`
from the folder that's actually open as the workspace root, it doesn't
search subfolders. This machine's actual daily habit is one VS Code
window with the parent `~/git` folder open, not `claude-tools` opened on
its own - so a workspace-scoped `tasks.json` living inside `claude-tools`
was invisible to VS Code the moment the keybinding was tested for real,
silently doing nothing (no error, `workbench.action.tasks.runTask`
just couldn't find a task by that label). User Tasks are global regardless
of which folder or repo is focused, which is what this actually needed -
caught by testing the real keybinding, not just simulating the
task's underlying command.

Verified 2026-08-11: simulated the "Ask about selection" task's exact
`"process"`-type invocation (real argv array, no shell) with a selection
containing `$`, backticks, and quotes - passed through safely with no
injection, and produced a real `get_file` tool call against the real
repo path. Separately verified `--offline` fixes the `gemini-bridge`
looping failure documented above, using the same invocation shape this
task actually sends.

## Roadmap

- [x] Add a third gemini-bridge tool for querying Gemini's larger context
      window on full files, not just diffs.
- [x] repo-bridge: expand `get_symbol` language support beyond
      python/javascript/typescript/tsx/go — added rust, java, ruby, c,
      and cpp.
- [x] repo-bridge: `get_symbol` should return all matches instead of just
      the first.
- [x] tn3270-bridge: structured `read_screen` mode (field positions,
      protected/unprotected, cursor location) alongside the plain-text dump,
      for when an agent needs to know where to type, not just what's shown.
- [x] Front the MCP servers with an [`mcpo`](https://github.com/open-webui/mcpo)
      (MCP-to-OpenAPI) proxy so tool-calling harnesses built on Ollama or
      llama.cpp — which don't speak MCP natively — can call these tools over
      plain HTTP. `linkedin-bridge` excluded on purpose (see mcpo Notes).
- [x] linkedin-bridge: dig further into the post-truncation issue — root
      caused to LinkedIn's "little" text format (see Notes above); fixed by
      auto-escaping reserved characters before every post/update.
- [x] linkedin-bridge: add `update_linkedin_post` / `delete_linkedin_post` —
      both work with the `w_member_social` scope this app already has.
- [x] Add `youtube-bridge`: transcribe/tighten/cut a raw recording, then
      queue it for scheduled upload (YouTube's own `publishAt`, no daemon)
      with source files auto-moved to `posted/`. Upload/OAuth path verified
      end-to-end with a real upload (see Notes above).
- [ ] youtube-bridge: add `update_youtube_video` / `delete_youtube_video`
      (mirroring `linkedin-bridge`'s pattern). Surfaced 2026-07-19 when a
      bad first upload (flicker/audio-skip from the fps bug, see Notes) sat
      privately scheduled on YouTube with no way to remove or replace it
      via MCP — had to be fixed by hand in YouTube Studio.
- [ ] youtube-bridge: find the actual cause of `tighten_video`'s flicker/
      scanline artifact and re-enable it. Confirmed 2026-07-19 the fps/
      bitrate fix didn't fix it, and that the artifact is specific to
      auto-editor's re-encode (raw unedited upload of the same recording had
      no artifact), but not yet which part of auto-editor's pipeline is at
      fault. Currently disabled, returns an error instead of running.
- [ ] linkedin-bridge: scheduled posting. LinkedIn's API has no server-side
      scheduled publish for personal profiles (that's a Company Page /
      Campaign Manager feature), so this would need Claude-side scheduling
      (a cron routine calling `post_to_linkedin` at a set time) with
      pre-approved text, mirroring `youtube-bridge`'s `publishAt` pattern
      but without native platform support. Surfaced 2026-07-21.
- [ ] youtube-bridge: chunked resumable upload with retry, for large files
      or flaky connections (current version sends the whole video in one PUT).
- [ ] youtube-bridge: thumbnail upload (`thumbnails.set`) once the core
      transcribe → cut → schedule path is verified end-to-end.
- [ ] linkedin-bridge: add read-back support (a tool that fetches a post's
      live content) once `r_member_social` is available. **Blocked on
      LinkedIn, no ETA** — that permission is currently closed to all new
      access requests, not just under heavy review, so there's nothing to
      do here until that changes.
- [x] Add `ollama-bridge`: a `prefilter_diff` tool backed by a local Ollama
      model (`qwen2.5-coder:7b`), so routine diffs get a free/offline triage
      pass before spending a `review_diff` (Gemini) call — only escalate
      when it comes back `FLAGGED`. Verified end-to-end 2026-07-24.
- [x] ollama-bridge: add `triage_log`, a second tool that extracts just the
      root failure (`FILE:`/`ERROR:`/`CONTEXT:`) from a build/test log file
      via the same local model, instead of reading the whole raw log.
      Verified end-to-end 2026-07-24 against synthetic pytest logs (one
      failing, one clean).
- [x] tn3270-bridge: panel-state abstractor. Added `read_panel_state`, which
      extracts IBM message-ID lines (`ICH408I`, etc.) and unprotected fields
      with best-guess labels instead of a full screen dump. Surfaced
      2026-08-06 brainstorming session on cutting agent token load. Unit
      tested against synthetic screen data only, not yet verified against a
      live host (see Notes above) — no test mainframe was reachable in this
      environment.
- [x] ollama-bridge: add `triage_transcript`, flagging likely restarts/
      filler/dead-air spots in a youtube-bridge transcript instead of the
      agent reading the whole thing closely to find them. Doesn't replace
      transcribe_video's full transcript (cut_video's docstring is explicit
      that cut decisions need the agent reading the real thing), it points
      at where to look first. Surfaced 2026-08-06, ranked #2. Verified
      end-to-end against a synthetic transcript with a real Ollama call.
- [ ] image-bridge: **blocked on billing.** Built `generate_image` against
      Gemini's native image model (`gemini-2.5-flash-image`), code path
      verified end-to-end (clean request, correctly parsed `429
      RESOURCE_EXHAUSTED` error), but the free-tier API key has a hard `0`
      quota for that specific model. Needs billing enabled on the Google AI
      Studio / Cloud project before a real image can be generated. Surfaced
      2026-08-06 while building interior illustrations for a children's
      book manuscript.
- [x] New slack-digest-bridge: `digest_thread`, a local-model digest of a
      Slack thread (decisions/action-items/blockers) fetched server-side via
      its own read-only bot token, so raw thread content never has to land
      in agent context. Surfaced 2026-08-06, ranked #3. Verified end-to-end
      against a real message in #claude-tools and a synthetic multi-message
      thread (see Notes above) — one real miss found: an open question
      wasn't flagged as a BLOCKER, documented as a known first-pass gap.
- [x] New discord-bridge: `list_channels`/`read_channel`/`post_message` for a
      Discord server, since no existing Discord MCP server actually covered
      both reading and posting. Surfaced 2026-08-07. Not yet verified against
      a live server (see Notes above) — no Discord bot token was available to
      test against in this environment.
- [x] Created `#britleys-corner` on the Discord server (guild
      961749187802824724) as a project idea inbox, random thoughts and ideas
      land there before they earn the right to become a real project.
      Introduced 2026-08-07.
- [x] Measured the actual token savings from the `prefilter_diff` escalation
      order, pulled straight from `ollama_log.jsonl`: 149 diffs reviewed by
      the local model, 147 of them (98.7%) came back clean and never
      escalated to `review_diff`, only 2 needed the bigger model. That's
      roughly 90,000+ tokens of review output avoided on diff review alone.
      Surfaced 2026-08-08 while drafting a LinkedIn post on tiering AI work
      by cost, numbers cited there are these.
- [x] New github-audit-bridge: `audit_dependabot_coverage`/`list_open_work`/
      `enable_dependabot` for checking and fixing Dependabot coverage across
      every repo on a GitHub account via `gh api`, no LLM calls. Surfaced
      2026-08-10 after a manual 41-repo audit burned a lot of turns looping
      `gh api` by hand. Verified end-to-end against the real `wren-creator`
      account — reproduced the manual audit's numbers exactly, and
      `enable_dependabot` was used for real to fix the 5 repos found off.
- [x] New playwright-bridge: `launch`/`goto`/`evaluate`/`screenshot`/
      `get_requests`/`click`/`fill`/`close` for driving a real browser
      mid-session — visual verification, computed-style/DOM inspection, and
      confirming a network dependency is or isn't actually being hit.
      Surfaced 2026-08-11 verifying a web3270 font-loading fix, where an
      ad-hoc `npx playwright install chromium firefox` stalled on the
      Firefox binary for 20+ minutes and never finished — nothing about
      that install persists between sessions, so the next session would hit
      the same wall. A real `.venv` dependency, installed once, does
      persist (see Notes for the install itself turning into its own saga
      over a cellular hotspot). Verified end-to-end against a live web3270
      Docker stack: `launch`+`goto` loaded the app, `get_requests` correctly
      confirmed zero `fonts.googleapis.com` requests, `evaluate` and
      `screenshot` both round-tripped real data. Chromium only for now —
      Firefox/WebKit installs deferred pending a non-metered connection.
- [x] New `ollama_agent.py`: a standalone chat loop giving a local Ollama
      model real tool access to the tools `mcpo` fronts (`gemini-bridge`,
      `tn3270-bridge`, `repo-bridge`), the DIY answer to "how can Ollama
      call these bridges" without routing through Open WebUI. Surfaced
      2026-08-11. Verified end-to-end against a live `mcpo` instance and
      `qwen2.5-coder:7b`, including two real model quirks caught and
      handled along the way: neither `:7b` nor `:14b` populate Ollama's
      structured `tool_calls` field, they answer with JSON text in
      `message.content` instead, and the model sometimes drops a tool's
      `<server>__` prefix or invents a placeholder path on an
      underspecified prompt. See the `ollama-agent` section above for the
      fixes for each.
- [x] Wired `ollama_agent.py` into VS Code for an offline "no internet"
      fallback inside the editor. Surfaced 2026-08-11 right after
      `ollama_agent.py` itself, when the user asked about building a
      dedicated VS Code extension for this. Got a second opinion from
      Gemini first: a full extension (or even a thin subprocess-wrapper
      one) was real new code and new failure modes for something only used
      during outages, its call was to skip the extension and wire the
      existing script into VS Code's built-in task system instead - the
      user agreed. Two real bugs caught only by testing the actual
      keybinding for real, not just simulating the underlying command:
      (1) the first version shipped as a workspace `.vscode/tasks.json`,
      invisible the moment the user tested it since their real habit is
      one VS Code window with the parent `~/git` folder open, not
      `claude-tools` on its own - moved to global VS Code User Tasks,
      which don't depend on which folder is focused; (2) with no explicit
      nudge, the model reached for `gemini-bridge__ask_gemini` eight times
      in a row instead of a local tool, exactly the tool that can't work
      with no internet - added an `--offline` flag to `ollama_agent.py`
      that excludes `NETWORK_DEPENDENT_SERVERS` from the tool list
      entirely rather than just telling the model not to use them. See the
      `ollama-agent` section's VS Code integration subsection and Notes
      above for the full writeup, including the `"process"`-type (not
      `"shell"`) task design that keeps an arbitrary code selection from
      being able to inject into the command line.
