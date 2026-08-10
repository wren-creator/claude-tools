import concurrent.futures
import json
import subprocess

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("github-audit-bridge")

GH_TIMEOUT = 30
MAX_WORKERS = 8


def _gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=GH_TIMEOUT)


def _gh_json(path: str):
    result = _gh(["api", path])
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _list_repos(owner: str) -> list[dict]:
    result = _gh([
        "repo", "list", owner, "--limit", "300",
        "--json", "name,isArchived,isFork",
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return [r for r in json.loads(result.stdout) if not r["isArchived"]]


def _check_repo_security(owner: str, name: str) -> dict:
    # vulnerability-alerts returns 204 (exit 0) when on, 404 (exit 1) when off
    alerts_on = _gh(["api", f"repos/{owner}/{name}/vulnerability-alerts"]).returncode == 0
    secfix = _gh_json(f"repos/{owner}/{name}/automated-security-fixes")
    secfix_on = bool(secfix and secfix.get("enabled"))
    # contents/... returns 404 (exit 1) when the file is absent
    dbyml_present = _gh(["api", f"repos/{owner}/{name}/contents/.github/dependabot.yml"]).returncode == 0
    return {"name": name, "alerts": alerts_on, "secfix": secfix_on, "dependabot_yml": dbyml_present}


@mcp.tool()
def audit_dependabot_coverage(owner: str = "wren-creator") -> str:
    """Check every non-archived repo under `owner` for Dependabot coverage:
    vulnerability alerts, automated security-fix PRs, and a
    .github/dependabot.yml (scheduled version-update config). Runs entirely
    against the GitHub API via `gh` (no LLM calls, no repo cloning) and
    returns one compact table - use this instead of looping `gh api` calls
    by hand each time this needs re-checking.

    Requires `gh auth status` to already be logged in with a token that has
    repo security-events access.
    """
    try:
        repos = _list_repos(owner)
    except Exception as e:
        return f"Error listing repos for {owner}: {e}"

    if not repos:
        return f"{owner} has no non-archived repos."

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_check_repo_security, owner, r["name"]): r for r in repos}
        for fut in concurrent.futures.as_completed(futures):
            meta = futures[fut]
            data = fut.result()
            data["is_fork"] = meta["isFork"]
            results.append(data)

    results.sort(key=lambda r: (r["alerts"], r["name"].lower()))

    off = [r for r in results if not r["alerts"]]
    no_yml = [r for r in results if not r["dependabot_yml"]]

    lines = [
        f"Dependabot audit for {owner} ({len(results)} repos, {sum(1 for r in results if r['is_fork'])} forks)",
        f"alerts+secfix ON: {len(results) - len(off)}   OFF: {len(off)}   dependabot.yml present: {len(results) - len(no_yml)}",
        "",
        f"{'repo':<28} {'fork':<5} {'alerts':<7} {'secfix':<7} {'dbyml'}",
    ]
    for r in results:
        lines.append(
            f"{r['name']:<28} {'yes' if r['is_fork'] else '':<5} "
            f"{'on' if r['alerts'] else 'OFF':<7} {'on' if r['secfix'] else 'OFF':<7} "
            f"{'yes' if r['dependabot_yml'] else 'no'}"
        )

    if off:
        lines.append("")
        lines.append("Needs attention (alerts off): " + ", ".join(r["name"] for r in off))

    return "\n".join(lines)


@mcp.tool()
def enable_dependabot(owner: str, repo: str) -> str:
    """Turn on Dependabot vulnerability alerts and automated security-fix
    PRs for owner/repo (PUT vulnerability-alerts + PUT
    automated-security-fixes). Does not create a dependabot.yml - that's a
    separate, per-repo scheduled-update config this tool doesn't write.
    Idempotent - safe to call on a repo that already has it on.
    """
    alerts = _gh(["api", "-X", "PUT", f"repos/{owner}/{repo}/vulnerability-alerts"])
    secfix = _gh(["api", "-X", "PUT", f"repos/{owner}/{repo}/automated-security-fixes"])
    alerts_ok = alerts.returncode == 0
    secfix_ok = secfix.returncode == 0
    if alerts_ok and secfix_ok:
        return f"{owner}/{repo}: vulnerability alerts and automated security fixes are now ON."
    problems = []
    if not alerts_ok:
        problems.append(f"vulnerability-alerts failed: {alerts.stderr.strip()}")
    if not secfix_ok:
        problems.append(f"automated-security-fixes failed: {secfix.stderr.strip()}")
    return f"{owner}/{repo}: partial failure - " + "; ".join(problems)


@mcp.tool()
def list_open_work(owner: str = "wren-creator") -> str:
    """List open issues and open pull requests for every non-archived repo
    under `owner` that has any, using `gh issue list` / `gh pr list` (issues
    and PRs are separate GitHub objects, this does not double-count).
    Skips repos with nothing open. Runs entirely against the GitHub API, no
    LLM calls.
    """
    try:
        repos = _list_repos(owner)
    except Exception as e:
        return f"Error listing repos for {owner}: {e}"

    def _fetch(name: str) -> tuple[str, list[str], list[str]]:
        issues_r = _gh([
            "issue", "list", "-R", f"{owner}/{name}", "--state", "open",
            "--json", "number,title", "-q", ".[] | \"#\\(.number) \\(.title)\"",
        ])
        prs_r = _gh([
            "pr", "list", "-R", f"{owner}/{name}", "--state", "open",
            "--json", "number,title", "-q", ".[] | \"#\\(.number) \\(.title)\"",
        ])
        issues = issues_r.stdout.strip().splitlines() if issues_r.returncode == 0 else []
        prs = prs_r.stdout.strip().splitlines() if prs_r.returncode == 0 else []
        return name, issues, prs

    lines = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for name, issues, prs in ex.map(lambda r: _fetch(r["name"]), repos):
            if not issues and not prs:
                continue
            lines.append(f"{name}  ({len(issues)} issue(s), {len(prs)} PR(s))")
            for i in issues:
                lines.append(f"  issue {i}")
            for p in prs:
                lines.append(f"  pr    {p}")

    if not lines:
        return f"No open issues or PRs across any non-archived repo under {owner}."
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
