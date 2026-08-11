"""Standalone chat loop that lets a local Ollama model call the tools mcpo
exposes over HTTP (gemini-bridge, tn3270-bridge, repo-bridge per
mcpo_config.json). Ollama doesn't speak MCP, but tool-capable models (e.g.
qwen2.5-coder) support OpenAI-style function calling over its own /api/chat
endpoint - this script is the missing piece that turns mcpo's OpenAPI spec
into Ollama tool schemas and dispatches the resulting tool_calls back to
mcpo as plain HTTP requests.

Requires mcpo already running against mcpo_config.json:
    .venv/bin/mcpo --port 8000 --api-key "your-key" --config mcpo_config.json

Usage:
    .venv/bin/python ollama_agent.py                  # interactive REPL
    .venv/bin/python ollama_agent.py "list_structure on ~/git/claude-tools"
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_MODEL = "qwen2.5-coder:7b"  # same default as ollama_bridge.py - :14b
# is more capable but noticeably slower on CPU-only Ollama; override
# --model for :14b when latency isn't a concern
DEFAULT_MCPO_URL = "http://localhost:8000"
DEFAULT_NUM_CTX = 8192  # Ollama defaults to 2048 and would silently
# truncate the tool schemas + system prompt before the conversation even
# starts - same gotcha ollama_bridge.py's _call_ollama works around.
MCPO_ENV_PATH = Path.home() / ".mcpo" / ".env"
LOG_PATH = Path(__file__).parent / "ollama_agent_log.jsonl"
OLLAMA_TIMEOUT = 120  # local 14b tool-calling turns are slower than a
# plain triage prompt
MCPO_TIMEOUT = 60
MAX_TOOL_ITERATIONS = 8

SYSTEM_PROMPT = (
    "You are a local coding assistant with tool access to a codebase "
    "(search/read files, find symbol definitions), a second-opinion "
    "coding model (Gemini), and TN3270 mainframe sessions. Use a tool "
    "when it would get a better answer than guessing - e.g. read the "
    "actual file instead of assuming its contents. Always call tools "
    "using their exact full name as given (e.g. 'repo-bridge__get_file', "
    "never a shortened form like 'get_file'), and only with real paths "
    "given in the conversation - never a placeholder like '/path/to/repo'. "
    "Give a direct final answer in plain text once you have what you "
    "need, don't call more tools than necessary."
)


def _load_mcpo_env() -> dict:
    env = {}
    if MCPO_ENV_PATH.exists():
        for line in MCPO_ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get_json(url: str, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _resolve_refs(node, spec: dict):
    """Recursively inline OpenAPI $ref pointers - mcpo's generated schemas
    can nest components/schemas refs for any tool with non-scalar args."""
    if isinstance(node, dict):
        if "$ref" in node:
            target = spec
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target[part]
            return _resolve_refs(target, spec)
        return {k: _resolve_refs(v, spec) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(v, spec) for v in node]
    return node


def fetch_mcpo_tools(mcpo_url: str, config_path: Path) -> tuple[list[dict], dict[str, str]]:
    """Returns (tools in Ollama's function-calling format, name -> mcpo
    path map). mcpo mounts each MCP server as its own sub-app with its own
    openapi.json (the combined /openapi.json is just an index page with no
    paths of its own) - server names come from mcpo_config.json rather
    than being discovered, since that's the same config mcpo itself was
    started with. Per-server openapi.json is intentionally public in
    mcpo's default setup - only the actual tool calls require the API key.
    """
    config = json.loads(config_path.read_text())
    servers = list(config.get("mcpServers", {}).keys())

    tools = []
    endpoint_map = {}
    for server in servers:
        spec = _get_json(f"{mcpo_url}/{server}/openapi.json", headers={}, timeout=MCPO_TIMEOUT)
        for path, methods in spec.get("paths", {}).items():
            post = methods.get("post")
            if not post:
                continue
            tool_name = path.strip("/")
            func_name = f"{server}__{tool_name}"

            schema = {"type": "object", "properties": {}}
            try:
                raw_schema = post["requestBody"]["content"]["application/json"]["schema"]
                schema = _resolve_refs(raw_schema, spec)
            except KeyError:
                pass  # tool takes no arguments

            description = post.get("description") or post.get("summary") or f"Call {tool_name} on {server}"
            tools.append({
                "type": "function",
                "function": {
                    "name": func_name,
                    "description": description[:1000],
                    "parameters": schema,
                },
            })
            endpoint_map[func_name] = f"/{server}{path}"
    return tools, endpoint_map


def call_mcpo_tool(mcpo_url: str, api_key: str, path: str, arguments: dict) -> str:
    try:
        result = _post_json(
            f"{mcpo_url}{path}",
            arguments,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=MCPO_TIMEOUT,
        )
        return json.dumps(result) if not isinstance(result, str) else result
    except urllib.error.HTTPError as e:
        return f"Error calling {path}: HTTP {e.code} - {e.read().decode(errors='replace')}"
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            return f"Error calling {path}: timed out after {MCPO_TIMEOUT}s"
        return f"Error calling {path}: mcpo not reachable at {mcpo_url} - is it running?"


def call_ollama_chat(model: str, messages: list, tools: list, num_ctx: int) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": False,
        "options": {"num_ctx": num_ctx},
    }
    try:
        resp = _post_json(f"{OLLAMA_HOST}/api/chat", payload, headers={}, timeout=OLLAMA_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Error calling Ollama: HTTP {e.code} - {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise SystemExit(f"Error calling Ollama: timed out after {OLLAMA_TIMEOUT}s")
        raise SystemExit(f"Error calling Ollama: not reachable at {OLLAMA_HOST} - is `ollama serve` running?")
    if "error" in resp:
        raise SystemExit(f"Error calling Ollama: {resp['error']}")
    return resp["message"]


def _fallback_tool_calls(content: str, endpoint_map: dict) -> list[dict] | None:
    """qwen2.5-coder over Ollama's default chat template often answers a
    tool call as one or more bare JSON objects in `message.content` -
    {"name": ..., "arguments": {...}} - instead of populating the
    structured `tool_calls` field the way llama3.1-style templates do, and
    sometimes emits several such objects back to back as plain text rather
    than a single valid JSON value (so a single json.loads() on the whole
    content fails). Confirmed live against qwen2.5-coder:7b/14b on
    2026-08-11. Scan for every `{` and try to decode a JSON value starting
    there with raw_decode, which stops at the first balanced close-brace
    and ignores anything before/after (markdown fences, commentary, a
    second call appended right after the first) - robust to all of the
    above without needing to special-case each one."""
    # Also matches a bare tool name missing its "<server>__" prefix (e.g.
    # "get_file" instead of "repo-bridge__get_file") - qwen2.5-coder drops
    # it inconsistently even when told the exact full name to use. Only
    # resolved when exactly one server exposes that bare name, to avoid
    # silently guessing between two same-named tools on different servers.
    bare_to_full: dict[str, list[str]] = {}
    for func_name in endpoint_map:
        bare_to_full.setdefault(func_name.split("__", 1)[1], []).append(func_name)

    decoder = json.JSONDecoder()
    calls = []
    idx = 0
    while idx < len(content):
        brace = content.find("{", idx)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(content, brace)
        except json.JSONDecodeError:
            idx = brace + 1
            continue
        if isinstance(obj, dict):
            name = obj.get("name")
            if name not in endpoint_map and len(bare_to_full.get(name, [])) == 1:
                name = bare_to_full[name][0]
            if name in endpoint_map:
                calls.append({"function": {"name": name, "arguments": obj.get("arguments", {})}})
        idx = end
    return calls or None


def run_turn(model: str, messages: list, tools: list, endpoint_map: dict, mcpo_url: str, api_key: str, num_ctx: int) -> str:
    for _ in range(MAX_TOOL_ITERATIONS):
        message = call_ollama_chat(model, messages, tools, num_ctx)
        messages.append(message)
        tool_calls = message.get("tool_calls") or _fallback_tool_calls(message.get("content", ""), endpoint_map) or []
        if not tool_calls:
            return message.get("content", "")

        for call in tool_calls:
            func_name = call["function"]["name"]
            raw_args = call["function"].get("arguments", {})
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            path = endpoint_map.get(func_name)
            if path is None:
                result = f"Error: unknown tool '{func_name}'"
            else:
                print(f"  [tool] {func_name}({json.dumps(arguments)})", file=sys.stderr)
                result = call_mcpo_tool(mcpo_url, api_key, path, arguments)
            _log({"tool": func_name, "arguments": arguments, "result": result})
            messages.append({"role": "tool", "content": result})

    return "(stopped after too many tool calls in a row - try a narrower question)"


def format_tools(tools: list[dict]) -> str:
    lines = [f"{len(tools)} tools loaded:"]
    for t in tools:
        fn = t["function"]
        first_line = (fn.get("description") or "").strip().split("\n", 1)[0]
        lines.append(f"  {fn['name']} - {first_line}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", nargs="*", help="one-shot prompt; omit for an interactive REPL")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--mcpo-url", default=None, help=f"default: $MCPO_URL, ~/.mcpo/.env, or {DEFAULT_MCPO_URL}")
    parser.add_argument("--api-key", default=None, help="default: $MCPO_API_KEY or ~/.mcpo/.env")
    parser.add_argument("--mcpo-config", default=str(Path(__file__).parent / "mcpo_config.json"),
                         help="path to the mcpo_config.json mcpo itself was started with (for server names)")
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    parser.add_argument("--list-tools", action="store_true", help="print the loaded tools and exit, without calling Ollama")
    args = parser.parse_args()

    env = _load_mcpo_env()
    mcpo_url = (args.mcpo_url or os.environ.get("MCPO_URL") or env.get("MCPO_URL") or DEFAULT_MCPO_URL).rstrip("/")
    api_key = args.api_key or os.environ.get("MCPO_API_KEY") or env.get("MCPO_API_KEY")
    if not api_key:
        sys.exit(
            "No mcpo API key found. Pass --api-key, set $MCPO_API_KEY, or write "
            f"MCPO_API_KEY=... to {MCPO_ENV_PATH} (matching the --api-key mcpo was started with)."
        )

    try:
        tools, endpoint_map = fetch_mcpo_tools(mcpo_url, Path(args.mcpo_config))
    except (urllib.error.URLError, urllib.error.HTTPError):
        sys.exit(f"Could not reach mcpo at {mcpo_url} - is `mcpo --config mcpo_config.json` running?")
    print(f"Loaded {len(tools)} tools from mcpo at {mcpo_url}: {', '.join(endpoint_map)}", file=sys.stderr)

    if args.list_tools:
        print(format_tools(tools))
        return

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if args.prompt:
        if args.prompt == ["/tools"]:
            print(format_tools(tools))
            return
        messages.append({"role": "user", "content": " ".join(args.prompt)})
        print(run_turn(args.model, messages, tools, endpoint_map, mcpo_url, api_key, args.num_ctx))
        return

    print(f"Interactive mode with {args.model} - Ctrl-D to exit, /tools to list available tools.", file=sys.stderr)
    while True:
        try:
            user_input = input("> ")
        except EOFError:
            print()
            break
        if not user_input.strip():
            continue
        if user_input.strip() == "/tools":
            print(format_tools(tools))
            continue
        messages.append({"role": "user", "content": user_input})
        print(run_turn(args.model, messages, tools, endpoint_map, mcpo_url, api_key, args.num_ctx))


if __name__ == "__main__":
    main()
