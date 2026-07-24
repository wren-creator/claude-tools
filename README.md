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

Exposes two tools backed by a locally running [Ollama](https://ollama.com)
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

## mcpo proxy

Fronts `gemini-bridge`, `tn3270-bridge`, and `repo-bridge` with
[`mcpo`](https://github.com/open-webui/mcpo), so tool-calling harnesses that
don't speak MCP natively (e.g. Ollama or llama.cpp-based agents) can call
these tools over plain HTTP/OpenAPI instead. `linkedin-bridge` and
`youtube-bridge` are deliberately excluded - see Notes.

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
