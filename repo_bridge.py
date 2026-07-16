import os
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from tree_sitter import Language, Node, Parser

mcp = FastMCP("repo-bridge")

MAX_FILE_CHARS = 60_000
MAX_SEARCH_RESULTS = 50
MAX_TREE_ENTRIES = 500
GIT_TIMEOUT = 15

IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", ".next"}

EXTENSION_LANGUAGE = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
}

# Node types that represent a named function/class/method/type definition in
# each grammar. Most of these node types expose their identifier via the
# "name" field, which is what lets get_symbol match by name generically
# instead of needing per-language field lookups. The exception is C/C++
# `function_definition`, whose identifier is nested inside a `declarator`
# chain rather than exposed as a "name" field directly - see
# _definition_name below.
DEFINITION_TYPES = {
    "python": {"function_definition", "class_definition"},
    "javascript": {"function_declaration", "generator_function_declaration", "method_definition", "class_declaration"},
    "typescript": {"function_declaration", "method_definition", "class_declaration", "interface_declaration", "type_alias_declaration", "enum_declaration"},
    "tsx": {"function_declaration", "method_definition", "class_declaration", "interface_declaration", "type_alias_declaration", "enum_declaration"},
    "go": {"function_declaration", "method_declaration", "type_spec"},
    "rust": {"function_item", "struct_item", "enum_item", "trait_item"},
    "java": {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration", "method_declaration", "constructor_declaration"},
    "ruby": {"method", "singleton_method", "class", "module"},
    "c": {"function_definition", "struct_specifier", "enum_specifier", "union_specifier"},
    "cpp": {"function_definition", "class_specifier", "struct_specifier", "enum_specifier", "union_specifier"},
}

_PARSER_CACHE: dict[str, Parser] = {}


def _get_parser(language: str) -> Parser:
    if language not in _PARSER_CACHE:
        if language == "python":
            import tree_sitter_python as ts_lang
            lang = Language(ts_lang.language())
        elif language == "javascript":
            import tree_sitter_javascript as ts_lang
            lang = Language(ts_lang.language())
        elif language == "typescript":
            import tree_sitter_typescript as ts_lang
            lang = Language(ts_lang.language_typescript())
        elif language == "tsx":
            import tree_sitter_typescript as ts_lang
            lang = Language(ts_lang.language_tsx())
        elif language == "go":
            import tree_sitter_go as ts_lang
            lang = Language(ts_lang.language())
        elif language == "rust":
            import tree_sitter_rust as ts_lang
            lang = Language(ts_lang.language())
        elif language == "java":
            import tree_sitter_java as ts_lang
            lang = Language(ts_lang.language())
        elif language == "ruby":
            import tree_sitter_ruby as ts_lang
            lang = Language(ts_lang.language())
        elif language == "c":
            import tree_sitter_c as ts_lang
            lang = Language(ts_lang.language())
        elif language == "cpp":
            import tree_sitter_cpp as ts_lang
            lang = Language(ts_lang.language())
        else:
            raise ValueError(f"unsupported language: {language}")
        _PARSER_CACHE[language] = Parser(lang)
    return _PARSER_CACHE[language]


def _truncate(text: str, limit: int = MAX_FILE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated {len(text) - limit} chars ...]"


def _resolve_in_repo(repo_path: str, path: str) -> Path | None:
    root = Path(repo_path).resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        return None
    return target


def _is_git_repo(repo_path: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_path, capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _walk_files(repo_path: str) -> list[str]:
    root = Path(repo_path)
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
        for name in filenames:
            rel = str(Path(dirpath, name).relative_to(root))
            paths.append(rel)
    return paths


def _grep_matches(repo_path: str, query: str, ignore_case: bool, max_results: int) -> list[str]:
    if _is_git_repo(repo_path):
        cmd = ["git", "grep", "-n", "-I"]
        if ignore_case:
            cmd.append("-i")
        cmd += ["-e", query]
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.strip())
        lines = result.stdout.splitlines()
    else:
        cmd = ["grep", "-rn", "-I"]
        if ignore_case:
            cmd.append("-i")
        cmd += ["--exclude-dir=" + d for d in IGNORED_DIRS]
        cmd += ["-e", query, "."]
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.strip())
        lines = [line[2:] if line.startswith("./") else line for line in result.stdout.splitlines()]

    return lines[:max_results]


@mcp.tool()
def search_codebase(repo_path: str, query: str, max_results: int = MAX_SEARCH_RESULTS, ignore_case: bool = False) -> str:
    """Search repo_path for lines matching query (a grep-style regex).
    Uses `git grep` when repo_path is a git repo (respects .gitignore),
    otherwise plain `grep -r`. Pass the absolute path of the repo - this
    server does not share Claude Code's working directory.
    Returns "path:line:content" per match, one per line.
    """
    try:
        matches = _grep_matches(repo_path, query, ignore_case, max_results)
    except Exception as e:
        return f"Error searching '{repo_path}': {e}"

    if not matches:
        return f"No matches for '{query}' in {repo_path}"
    return "\n".join(matches)


@mcp.tool()
def get_file(repo_path: str, path: str) -> str:
    """Return the full contents of path, relative to repo_path (truncated
    past 60k chars). Pass the absolute path of the repo as repo_path.
    """
    target = _resolve_in_repo(repo_path, path)
    if target is None:
        return f"Error: '{path}' escapes repo_path"
    if not target.exists():
        return f"Error: '{path}' does not exist in {repo_path}"
    if target.is_dir():
        return f"Error: '{path}' is a directory, not a file"

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Error reading '{path}': {e}"
    return _truncate(text)


@mcp.tool()
def list_structure(repo_path: str, max_entries: int = MAX_TREE_ENTRIES) -> str:
    """Return a directory tree for repo_path as indented text. Uses tracked
    files (`git ls-files`) when repo_path is a git repo, otherwise walks the
    filesystem skipping common junk dirs (node_modules, .venv, etc.).
    """
    try:
        if _is_git_repo(repo_path):
            result = subprocess.run(
                ["git", "ls-files"], cwd=repo_path, capture_output=True, text=True, timeout=GIT_TIMEOUT,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip())
            paths = result.stdout.splitlines()
        else:
            paths = _walk_files(repo_path)
    except Exception as e:
        return f"Error listing structure of '{repo_path}': {e}"

    if not paths:
        return f"{repo_path} has no files (or is empty)"

    tree: dict = {}
    for p in paths:
        parts = p.split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault(parts[-1], None)

    lines: list[str] = []

    def render(node: dict, prefix: str) -> None:
        entries = sorted(node.items(), key=lambda kv: (kv[1] is None, kv[0]))
        for i, (name, child) in enumerate(entries):
            if len(lines) >= max_entries:
                lines.append(prefix + "... (truncated)")
                return
            last = i == len(entries) - 1
            lines.append(prefix + ("└── " if last else "├── ") + name + ("/" if isinstance(child, dict) else ""))
            if isinstance(child, dict):
                render(child, prefix + ("    " if last else "│   "))

    render(tree, "")
    return "\n".join(lines)


@mcp.tool()
def get_symbol(repo_path: str, name: str, language: str = "") -> str:
    """Find a function/class/method/type definition named `name` in
    repo_path and return its source text. Supports python, javascript,
    typescript, tsx, go, rust, java, ruby, c, and cpp. Optionally restrict
    to one of those via `language`. Returns the first match found;
    searches files that reference `name` first (via grep) rather than
    parsing the whole repo.
    """
    try:
        candidates = _grep_matches(repo_path, rf"\b{name}\b", ignore_case=False, max_results=200)
    except Exception as e:
        return f"Error searching for '{name}': {e}"

    files_seen = []
    for line in candidates:
        file_path = line.split(":", 1)[0]
        if file_path not in files_seen:
            files_seen.append(file_path)

    for file_path in files_seen:
        ext = Path(file_path).suffix
        lang = EXTENSION_LANGUAGE.get(ext)
        if lang is None or (language and lang != language):
            continue

        target = _resolve_in_repo(repo_path, file_path)
        if target is None or not target.exists():
            continue
        source = target.read_bytes()

        try:
            tree = _get_parser(lang).parse(source)
        except Exception:
            continue

        match = _find_definition(tree.root_node, name, DEFINITION_TYPES[lang])
        if match is not None:
            start_line = match.start_point[0] + 1
            end_line = match.end_point[0] + 1
            text = match.text.decode("utf-8", errors="replace")
            return f"{file_path}:{start_line}-{end_line}\n\n{text}"

    return f"No definition found for '{name}' in {repo_path} (searched {len(files_seen)} candidate file(s))"


def _c_family_function_name(node: Node) -> Node | None:
    # C/C++ function_definition nodes don't expose a "name" field - the
    # identifier is nested inside a chain of declarators (pointer_declarator
    # for pointer return types, etc.) ending in a function_declarator whose
    # own "declarator" field is the actual identifier.
    declarator = node.child_by_field_name("declarator")
    while declarator is not None and declarator.type != "function_declarator":
        declarator = declarator.child_by_field_name("declarator")
    if declarator is None:
        return None
    return declarator.child_by_field_name("declarator")


def _definition_name(node: Node) -> Node | None:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return name_node
    if node.type == "function_definition":
        return _c_family_function_name(node)
    return None


def _find_definition(node: Node, name: str, definition_types: set[str]) -> Node | None:
    if node.type in definition_types:
        name_node = _definition_name(node)
        if name_node is not None and name_node.text.decode("utf-8", errors="replace") == name:
            return node
    for child in node.children:
        found = _find_definition(child, name, definition_types)
        if found is not None:
            return found
    return None


if __name__ == "__main__":
    mcp.run()
