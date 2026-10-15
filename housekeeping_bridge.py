"""housekeeping-bridge: repeatable GitHub repo housekeeping for one owner.

Audit Dependabot coverage, rulesets and deploy keys; apply protection rulesets;
add dependabot.yml; test open Dependabot PRs in throwaway Docker containers;
merge the ones that passed. Everything that changes GitHub defaults to
dry_run=True. State lives in ~/.housekeeping/ (config, last test results, log).
"""
import base64
import concurrent.futures
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("housekeeping-bridge")

HOME = Path.home() / ".housekeeping"
CONFIG_FILE = HOME / "config.json"
RESULTS_FILE = HOME / "last_results.json"
LOG_FILE = HOME / "housekeeping_log.jsonl"

GH_TIMEOUT = 60
MAX_WORKERS = 6
STEP_TIMEOUT = 900
PROT_NAME = "prot"
PR_RULE_NAME = "require-pr"
BRANCH = "chore/dependabot-config"
SKIP_TREE = re.compile(r"(^|/)(node_modules|vendor|\.venv|venv|site-packages|\.git)/")
TRAILER = "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"

DEFAULT_CONFIG = {
    "owner": "wren-creator",
    # Repos that are deliberately vulnerable or otherwise should not get version-update PRs.
    "skip_dependabot_yml": ["widgetorium"],
    # Never touch these in any mutating tool.
    "skip_all": [],
    "cooldown_days": 7,          # written into generated dependabot.yml
    "min_pr_age_days": 3,        # merge_bumps refuses younger PRs
    "max_result_age_hours": 48,  # merge_bumps refuses stale test results
    "images": {
        "npm": "node:22-bookworm",
        "pip": "python:3.13-bookworm",
        "go": "golang:1.25-bookworm",
    },
}

_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------- plumbing

def _config() -> dict:
    HOME.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except json.JSONDecodeError:
            pass
    else:
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    return cfg


def _log(action: str, **kw) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "action": action, **kw}) + "\n")


def _run(cmd: list[str], timeout: int = GH_TIMEOUT, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def _gh(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return _run(["gh"] + args, input=stdin)


def _gh_json(path: str):
    r = _gh(["api", path])
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _repos(owner: str, only: str = "") -> list[dict]:
    r = _gh(["repo", "list", owner, "--limit", "300", "--json",
             "name,isArchived,isFork,visibility,defaultBranchRef"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    wanted = {x.strip() for x in only.split(",") if x.strip()}
    out = []
    for x in json.loads(r.stdout):
        if x["isArchived"]:
            continue
        if wanted and x["name"] not in wanted:
            continue
        x["branch"] = (x.get("defaultBranchRef") or {}).get("name", "main")
        x["public"] = x["visibility"] == "PUBLIC"
        out.append(x)
    return out


def _tree(owner: str, name: str, branch: str) -> list[str]:
    t = _gh_json(f"repos/{owner}/{name}/git/trees/{branch}?recursive=1")
    if not t:
        return []
    return [e["path"] for e in t.get("tree", []) if e.get("type") == "blob"]


def _dirs_for(paths: list[str], pattern: str) -> list[str]:
    rx = re.compile(pattern)
    dirs = set()
    for p in paths:
        if SKIP_TREE.search(p):
            continue
        base = p.rsplit("/", 1)[-1]
        if rx.search(base):
            d = p.rsplit("/", 1)[0] if "/" in p else ""
            dirs.add("/" + d)
    return sorted(dirs)


def _ecosystems(paths: list[str]) -> dict[str, list[str]]:
    eco = {
        "npm": _dirs_for(paths, r"^package\.json$"),
        "pip": _dirs_for(paths, r"^(requirements.*\.txt|pyproject\.toml)$"),
        "docker": _dirs_for(paths, r"^Dockerfile"),
        "gomod": _dirs_for(paths, r"^go\.mod$"),
    }
    if any(re.match(r"^\.github/workflows/.+\.ya?ml$", p) for p in paths):
        eco["github-actions"] = ["/"]
    return {k: v for k, v in eco.items() if v}


def _build_yml(eco: dict[str, list[str]], cooldown_days: int) -> str:
    lines = [
        "# Dependabot version updates. Weekly; minor and patch bumps grouped into one PR,",
        "# major bumps arrive separately so they can be tested on purpose.",
        f"# New releases wait {cooldown_days} days before a PR opens (supply-chain cooldown).",
        "version: 2",
        "updates:",
    ]
    for name, dirs in eco.items():
        lines.append(f'  - package-ecosystem: "{name}"')
        if len(dirs) == 1:
            lines.append(f'    directory: "{dirs[0]}"')
        else:
            lines.append("    directories:")
            lines += [f'      - "{d}"' for d in dirs]
        lines += [
            "    schedule:",
            '      interval: "weekly"',
            "    open-pull-requests-limit: 5",
            "    cooldown:",
            f"      default-days: {cooldown_days}",
            "    groups:",
            "      minor-and-patch:",
            '        update-types: ["minor", "patch"]',
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- audit

def _coverage(owner: str, name: str):
    """(ruleset names, blocks delete+force-push, requires PR) judged by what the
    active rulesets actually do, not by what they are called."""
    rs = _gh_json(f"repos/{owner}/{name}/rulesets")
    if not isinstance(rs, list):
        return [], False, False
    names, types = [], set()
    for x in rs:
        names.append(x["name"])
        if x.get("enforcement") != "active":
            continue
        d = _gh_json(f"repos/{owner}/{name}/rulesets/{x['id']}") or {}
        types |= {rule["type"] for rule in d.get("rules", [])}
    return names, {"deletion", "non_fast_forward"} <= types, "pull_request" in types


def _audit_repo(owner: str, r: dict) -> dict:
    name = r["name"]
    alerts = _gh(["api", f"repos/{owner}/{name}/vulnerability-alerts"]).returncode == 0
    sf = _gh_json(f"repos/{owner}/{name}/automated-security-fixes")
    yml = _gh(["api", f"repos/{owner}/{name}/contents/.github/dependabot.yml"]).returncode == 0
    eco = _ecosystems(_tree(owner, name, r["branch"]))
    rulesets: list[str] | None = None
    has_prot = has_pr = False
    if r["public"]:
        rulesets, has_prot, has_pr = _coverage(owner, name)
    keys = _gh_json(f"repos/{owner}/{name}/keys")
    prs = _gh(["pr", "list", "-R", f"{owner}/{name}", "--author", "app/dependabot",
               "--json", "number", "-q", "length"])
    return {
        "name": name, "public": r["public"], "fork": r["isFork"], "alerts": alerts,
        "secfix": bool(sf and sf.get("enabled")), "yml": yml, "ecosystems": sorted(eco),
        "rulesets": rulesets, "has_prot": has_prot, "has_pr": has_pr, "deploy_keys": len(keys) if isinstance(keys, list) else 0,
        "dependabot_prs": int(prs.stdout.strip() or 0) if prs.returncode == 0 else 0,
    }


@mcp.tool()
def housekeeping_audit(owner: str = "") -> str:
    """Read-only audit of every non-archived repo for `owner` (default from
    ~/.housekeeping/config.json): Dependabot alerts and security fixes, whether
    a dependabot.yml exists where package manifests do, the `prot` and
    `require-pr` rulesets on public repos (private repos cannot have rulesets
    on a free plan), deploy keys, and open Dependabot PRs. Returns a summary of
    what needs attention plus a per-repo table. Changes nothing.
    """
    cfg = _config()
    owner = owner or cfg["owner"]
    try:
        repos = _repos(owner)
    except Exception as e:
        return f"Error listing repos for {owner}: {e}"
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        rows = sorted(ex.map(lambda r: _audit_repo(owner, r), repos), key=lambda x: x["name"].lower())

    skip_yml = set(cfg["skip_dependabot_yml"]) | set(cfg["skip_all"])
    no_alerts = [r["name"] for r in rows if not (r["alerts"] and r["secfix"])]
    need_yml = [r["name"] for r in rows if r["ecosystems"] and not r["yml"] and r["name"] not in skip_yml]
    need_prot = [r["name"] for r in rows if r["public"] and not r["has_prot"]]
    need_pr = [r["name"] for r in rows if r["public"] and not r["has_pr"]]
    keys = [f"{r['name']} ({r['deploy_keys']})" for r in rows if r["deploy_keys"]]
    prs = sum(r["dependabot_prs"] for r in rows)

    pub = sum(1 for r in rows if r["public"])
    out = [
        f"Housekeeping audit for {owner}: {len(rows)} repos ({pub} public, {len(rows) - pub} private)",
        f"  Dependabot alerts+security fixes off : {len(no_alerts)}  {', '.join(no_alerts) or '-'}",
        f"  Manifests but no dependabot.yml      : {len(need_yml)}  {', '.join(need_yml) or '-'}",
        f"  Public, delete/force-push not blocked: {len(need_prot)}  {', '.join(need_prot) or '-'}",
        f"  Public, PRs not required on default : {len(need_pr)}  {', '.join(need_pr) or '-'}",
        f"  Repos with deploy keys               : {', '.join(keys) or 'none'}",
        f"  Open Dependabot PRs                  : {prs}",
        "",
        f"{'repo':<28} {'vis':<5} {'alert':<5} {'yml':<4} {'rulesets':<20} {'keys':<4} {'prs':<4} ecosystems",
    ]
    for r in rows:
        rs = "n/a (private)" if r["rulesets"] is None else ",".join(r["rulesets"]) or "none"
        out.append(f"{r['name']:<28} {'pub' if r['public'] else 'priv':<5} "
                   f"{'on' if r['alerts'] and r['secfix'] else 'OFF':<5} {'yes' if r['yml'] else 'no':<4} "
                   f"{rs:<20} {r['deploy_keys']:<4} {r['dependabot_prs']:<4} {','.join(r['ecosystems'])}")
    return "\n".join(out)


# ---------------------------------------------------------------- protection

def _ruleset(name: str, include: list[str], rules: list[dict]) -> dict:
    return {"name": name, "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": include, "exclude": []}},
            "rules": rules, "bypass_actors": []}


PROT = _ruleset(PROT_NAME, ["~ALL"], [{"type": "deletion"}, {"type": "non_fast_forward"}])
REQUIRE_PR = _ruleset(PR_RULE_NAME, ["~DEFAULT_BRANCH"], [{"type": "pull_request", "parameters": {
    "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": False,
    "require_code_owner_review": False, "require_last_push_approval": False,
    "required_review_thread_resolution": False}}])


@mcp.tool()
def apply_protection(repos: str = "", require_pr: bool = True, dry_run: bool = True) -> str:
    """Add the `prot` ruleset (blocks branch deletion and force-push on all
    branches) and, if `require_pr`, the `require-pr` ruleset (pull request
    required on the default branch, zero approvals) to every public repo that
    lacks them. Idempotent: judged by what existing active rulesets do (a ruleset
    named something else that blocks deletion and force-push counts), not by name. Private repos are skipped (rulesets need GitHub Pro). `repos` is an
    optional comma-separated list. dry_run=True (default) only reports.

    Note: require_pr means direct pushes to the default branch stop working;
    changes go through a branch and pull request.
    """
    cfg = _config()
    owner = cfg["owner"]
    todo = []
    for r in _repos(owner, repos):
        if not r["public"] or r["name"] in cfg["skip_all"]:
            continue
        _, has_prot, has_pr = _coverage(owner, r["name"])
        missing = ([] if has_prot else [PROT]) + ([] if has_pr or not require_pr else [REQUIRE_PR])
        todo.append((r["name"], missing))
    lines = []
    for name, missing in todo:
        if not missing:
            lines.append(f"{name}: already protected")
            continue
        names = ",".join(m["name"] for m in missing)
        if dry_run:
            lines.append(f"{name}: WOULD add {names}")
            continue
        for m in missing:
            res = _gh(["api", "-X", "POST", f"repos/{owner}/{name}/rulesets", "--input", "-", "-q", ".id"],
                      stdin=json.dumps(m))
            ok = res.returncode == 0
            lines.append(f"{name}: {m['name']} {'created ' + res.stdout.strip() if ok else 'FAILED ' + res.stderr.strip()[:100]}")
            _log("apply_protection", repo=name, ruleset=m["name"], ok=ok)
        time.sleep(0.5)
    head = "DRY RUN (no changes). " if dry_run else ""
    return head + "\n".join(lines)


# ---------------------------------------------------------------- dependabot.yml

@mcp.tool()
def add_dependabot_config(repos: str = "", dry_run: bool = True) -> str:
    """For repos that contain package manifests (npm, pip, Docker, Go, Actions)
    but no .github/dependabot.yml, generate one covering every manifest
    directory (weekly, minor+patch grouped, with a release cooldown). Public
    repos get a branch + pull request (because of the require-pr ruleset);
    private repos get a direct commit. Repos in config `skip_dependabot_yml`
    (e.g. deliberately vulnerable labs) are skipped. Idempotent. dry_run=True
    (default) prints each config and changes nothing.
    """
    cfg = _config()
    owner = cfg["owner"]
    skip = set(cfg["skip_dependabot_yml"]) | set(cfg["skip_all"])
    lines = []
    for r in _repos(owner, repos):
        name, b = r["name"], r["branch"]
        if name in skip:
            lines.append(f"{name}: skipped (config skip list)")
            continue
        if _gh(["api", f"repos/{owner}/{name}/contents/.github/dependabot.yml"]).returncode == 0:
            continue
        eco = _ecosystems(_tree(owner, name, b))
        if not eco:
            continue
        yml = _build_yml(eco, int(cfg["cooldown_days"]))
        lines.append(f"=== {name} [{'public -> PR' if r['public'] else 'private -> direct commit'}]")
        if dry_run:
            lines.append(yml)
            continue
        msg = ("chore: add Dependabot version-update config\n\n"
               "Security alerts were already on; this adds weekly scheduled updates with\n"
               f"minor and patch bumps grouped and a {cfg['cooldown_days']}-day release cooldown.\n\n{TRAILER}")
        content = base64.b64encode(yml.encode()).decode()
        target = BRANCH if r["public"] else b
        if r["public"]:
            sha = _gh_json(f"repos/{owner}/{name}/git/ref/heads/{b}")
            if not sha:
                lines.append("  FAILED: cannot read default branch")
                continue
            mk = _gh(["api", "-X", "POST", f"repos/{owner}/{name}/git/refs",
                      "-f", f"ref=refs/heads/{BRANCH}", "-f", f"sha={sha['object']['sha']}"])
            if mk.returncode != 0:
                lines.append(f"  FAILED branch: {mk.stderr.strip()[:100]}")
                continue
        put = _gh(["api", "-X", "PUT", f"repos/{owner}/{name}/contents/.github/dependabot.yml",
                   "-f", f"message={msg}", "-f", f"content={content}", "-f", f"branch={target}"])
        if put.returncode != 0:
            lines.append(f"  FAILED commit: {put.stderr.strip()[:100]}")
            continue
        if r["public"]:
            pr = _gh(["pr", "create", "-R", f"{owner}/{name}", "--base", b, "--head", BRANCH,
                      "--title", "chore: add Dependabot version-update config",
                      "--body", "Adds a weekly Dependabot version-update config. Minor and patch bumps are grouped; "
                                "majors arrive separately.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"])
            lines.append(f"  PR: {pr.stdout.strip() or pr.stderr.strip()[:100]}")
        else:
            lines.append("  committed")
        _log("add_dependabot_config", repo=name, public=r["public"])
    if not lines:
        return "Nothing to do: every repo with manifests already has a dependabot.yml (or is on the skip list)."
    return ("DRY RUN (no changes).\n" if dry_run else "") + "\n".join(lines)


# ---------------------------------------------------------------- bump testing

def _kind(title: str) -> str:
    m = re.search(r"from [^0-9]*(\d+)\.(\d+)\.?(\d*)\S* to [^0-9]*(\d+)\.(\d+)\.?(\d*)", title)
    if not m:
        return "group"
    a, b = m.groups()[:3], m.groups()[3:]
    if a[0] != b[0]:
        return "major"
    return "minor" if a[1] != b[1] else "patch"


def _from_version(title: str) -> str:
    m = re.search(r"from [^0-9]*(\d+\.\d+\S*)", title)
    return m.group(1) if m else ""


NPM_SH = r'''set -e; cd "$D"
if [ -f package-lock.json ]; then npm ci --no-audit --no-fund || npm install --no-audit --no-fund; else npm install --no-audit --no-fund; fi
did=0
if node -e 'process.exit((require("./package.json").scripts||{}).build?0:1)'; then npm run build; did=1; fi
if node -e 'const t=((require("./package.json").scripts||{}).test||"");process.exit(t&&!/no test specified/.test(t)?0:1)'; then npm test; did=1; fi
[ $did = 1 ] && exit 0 || exit 10'''
PIP_SH = r'''set -e; cd "$D"; python -m venv /tmp/v; . /tmp/v/bin/activate
if [ -f requirements.txt ]; then pip install -q -r requirements.txt; elif [ -f pyproject.toml ]; then pip install -q .; else exit 10; fi
if ls test_*.py tests >/dev/null 2>&1; then pip install -q pytest; pytest -q; exit 0; fi
python -m compileall -q . ; exit 10'''
GO_SH = r'''set -e; cd "$D"; go build ./... && go test ./...'''
SCRIPTS = {"npm": NPM_SH, "pip": PIP_SH, "go": GO_SH}


def _test_dir(cfg: dict, root: Path, d: str, eco: str, log: Path) -> str:
    """Run install/build/test inside a throwaway container. Only the cloned repo is mounted."""
    if eco not in SCRIPTS:
        return "MANUAL"
    cmd = ["docker", "run", "--rm", "--cpus", "2", "--memory", "3g", "-v", f"{root}:/work",
           "-e", f"D=/work/{d}", cfg["images"][eco], "bash", "-c", SCRIPTS[eco]]
    try:
        p = _run(cmd, timeout=STEP_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.write_text("timeout")
        return "FAIL"
    log.write_text((p.stdout or "")[-4000:] + (p.stderr or "")[-4000:])
    return {0: "PASS", 10: "NOTEST"}.get(p.returncode, "FAIL")


def _test_repo(cfg: dict, owner: str, name: str, work: Path, job: dict) -> list[dict]:
    root = work / name
    c = _run(["gh", "repo", "clone", f"{owner}/{name}", str(root), "--", "-q"], timeout=300)
    if c.returncode != 0:
        return []
    base = _run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    prs = _gh(["pr", "list", "-R", f"{owner}/{name}", "--author", "app/dependabot", "--limit", "100",
               "--json", "number,title,createdAt"])
    rows, baselines = [], {}
    for pr in sorted(json.loads(prs.stdout or "[]"), key=lambda x: x["number"]):
        n, title = pr["number"], pr["title"]
        files = _gh(["pr", "diff", str(n), "-R", f"{owner}/{name}", "--name-only"]).stdout.split()
        mf = next((f for f in files if re.search(r"(^|/)(package\.json|requirements[^/]*\.txt|pyproject\.toml|go\.mod|Dockerfile[^/]*)$", f)), "")
        if not mf:
            lf = next((f for f in files if f.endswith("package-lock.json")), "")
            mf = lf.replace("package-lock.json", "package.json") if lf else ""
        d = mf.rsplit("/", 1)[0] if "/" in mf else "."
        b = mf.rsplit("/", 1)[-1]
        eco = ("npm" if b == "package.json" else "pip" if b.startswith("requirements") or b == "pyproject.toml"
               else "go" if b == "go.mod" else "docker" if b.startswith("Dockerfile") else "none")
        logdir = HOME / "logs"
        logdir.mkdir(exist_ok=True)

        def checkout(ref: str) -> None:
            _run(["git", "-C", str(root), "checkout", "-q", "-f", ref])
            _run(["git", "-C", str(root), "clean", "-fdxq"])
        if (d, eco) not in baselines:
            checkout(base)
            baselines[(d, eco)] = _test_dir(cfg, root, d, eco, logdir / f"{name}-baseline.log")
        _run(["git", "-C", str(root), "fetch", "-q", "origin", f"pull/{n}/head:pr-{n}"])
        checkout(f"pr-{n}")
        res = _test_dir(cfg, root, d, eco, logdir / f"{name}-{n}.log")
        rows.append({"repo": name, "pr": n, "kind": _kind(title), "from": _from_version(title),
                     "dir": d, "eco": eco, "baseline": baselines[(d, eco)], "result": res,
                     "created": pr["createdAt"], "title": title})
        job["done"] += 1
    return rows


def _run_job(job_id: str, repos: str) -> None:
    job = _jobs[job_id]
    cfg = _config()
    owner = cfg["owner"]
    work = Path(tempfile.mkdtemp(prefix="housekeeping-"))
    try:
        names = [r["name"] for r in _repos(owner, repos) if r["name"] not in cfg["skip_all"]]
        job["total"] = sum(
            int(_gh(["pr", "list", "-R", f"{owner}/{n}", "--author", "app/dependabot", "--json", "number", "-q", "length"]).stdout.strip() or 0)
            for n in names)
        all_rows: list[dict] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            for rows in ex.map(lambda n: _test_repo(cfg, owner, n, work, job), names):
                all_rows += rows
        RESULTS_FILE.write_text(json.dumps({"finished": datetime.now(timezone.utc).isoformat(),
                                            "owner": owner, "rows": all_rows}, indent=1))
        job["state"] = "finished"
        job["rows"] = len(all_rows)
    except Exception as e:  # noqa: BLE001
        job["state"], job["error"] = "failed", str(e)
    finally:
        shutil.rmtree(work, ignore_errors=True)  # fresh workspace per run, never left behind


@mcp.tool()
def start_bump_tests(repos: str = "") -> str:
    """Start (in the background) a test of every open Dependabot PR. Each repo
    is cloned to a temp dir; install, build and tests run inside a throwaway
    Docker container (node/python/go image) that only sees the cloned repo, so
    third-party install scripts never run on the host. Every PR is compared to
    a baseline run of the default branch. Docker-base-image bumps are marked
    MANUAL. `repos` is an optional comma-separated list. Takes ~20 minutes for
    a full account; poll bump_test_status. Changes nothing on GitHub.
    """
    if _run(["docker", "info"], timeout=20).returncode != 0:
        return "Docker is not running. Start Docker Desktop and try again."
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"state": "running", "done": 0, "total": 0, "started": datetime.now(timezone.utc).isoformat()}
    threading.Thread(target=_run_job, args=(job_id, repos), daemon=True).start()
    return f"Started bump test job {job_id}. Check progress with bump_test_status('{job_id}')."


def _tier(row: dict, cfg: dict) -> str:
    if row["result"] == "FAIL" and row["baseline"] != "FAIL":
        return "broken-by-bump"
    if row["result"] == "MANUAL":
        return "manual"
    if row["kind"] in ("major", "group"):
        return "hold-major-or-group"
    if row["kind"] == "minor" and row["from"].startswith("0."):
        return "hold-pre-1.0"
    if row["result"] == "PASS":
        return "tier1"
    if row["result"] == "NOTEST" and row["baseline"] != "FAIL":
        return "tier2"
    return "hold"


@mcp.tool()
def bump_test_status(job_id: str = "") -> str:
    """Progress of a start_bump_tests job, or (with no job_id / once finished)
    the last saved results grouped into tiers: tier1 (patch/minor, build+tests
    passed), tier2 (patch/minor, installs and compiles but has no tests),
    broken-by-bump, hold-major-or-group, hold-pre-1.0, manual (Docker bumps).
    """
    cfg = _config()
    if job_id and job_id in _jobs and _jobs[job_id]["state"] != "finished":
        j = _jobs[job_id]
        return f"Job {job_id}: {j['state']}, {j['done']}/{j['total'] or '?'} PRs tested. {j.get('error', '')}"
    if not RESULTS_FILE.exists():
        return "No saved results yet. Run start_bump_tests first."
    data = json.loads(RESULTS_FILE.read_text())
    tiers: dict[str, list[str]] = {}
    for row in data["rows"]:
        tiers.setdefault(_tier(row, cfg), []).append(
            f"{row['repo']}#{row['pr']} [{row['kind']}, {row['eco']}, base={row['baseline']} pr={row['result']}] {row['title'][:70]}")
    out = [f"Results from {data['finished']} ({len(data['rows'])} PRs)"]
    for t in ("tier1", "tier2", "broken-by-bump", "hold-major-or-group", "hold-pre-1.0", "manual", "hold"):
        if t in tiers:
            out += ["", f"{t} ({len(tiers[t])})"] + [f"  {x}" for x in tiers[t]]
    return "\n".join(out)


# ---------------------------------------------------------------- merge

@mcp.tool()
def merge_bumps(include_untested: bool = False, dry_run: bool = True) -> str:
    """Merge Dependabot PRs from the last bump test run: tier1 only, or tier1
    plus tier2 when `include_untested`. Safety checks before each merge: results
    must be fresh (config max_result_age_hours), the PR must still be open and
    at least min_pr_age_days old (supply-chain cooldown), and majors, grouped
    PRs and pre-1.0 minors are never merged. If a merge fails (usually a
    lockfile conflict after an earlier merge), it asks Dependabot to rebase;
    run it again later. dry_run=True (default) only lists what would merge.
    """
    cfg = _config()
    if not RESULTS_FILE.exists():
        return "No saved results. Run start_bump_tests first."
    data = json.loads(RESULTS_FILE.read_text())
    age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(data["finished"])).total_seconds() / 3600
    if age_h > cfg["max_result_age_hours"]:
        return f"Results are {age_h:.0f}h old (limit {cfg['max_result_age_hours']}h). Re-run start_bump_tests."
    owner = data["owner"]
    allowed = {"tier1"} | ({"tier2"} if include_untested else set())
    lines = []
    for row in data["rows"]:
        if _tier(row, cfg) not in allowed or row["repo"] in cfg["skip_all"]:
            continue
        ref = f"{row['repo']}#{row['pr']}"
        info = _gh_json(f"repos/{owner}/{row['repo']}/pulls/{row['pr']}")
        if not info or info["state"] != "open":
            lines.append(f"{ref}: skip ({info['state'] if info else 'unreadable'})")
            continue
        age_d = (datetime.now(timezone.utc) - datetime.fromisoformat(info["created_at"].replace("Z", "+00:00"))).days
        if age_d < cfg["min_pr_age_days"]:
            lines.append(f"{ref}: skip (only {age_d}d old, cooldown {cfg['min_pr_age_days']}d)")
            continue
        if dry_run:
            lines.append(f"{ref}: WOULD merge ({row['kind']}) {row['title'][:60]}")
            continue
        m = _gh(["pr", "merge", str(row["pr"]), "-R", f"{owner}/{row['repo']}", "--squash", "--delete-branch"])
        if m.returncode == 0:
            lines.append(f"{ref}: merged")
            _log("merge_bump", pr=ref)
        else:
            _gh(["pr", "comment", str(row["pr"]), "-R", f"{owner}/{row['repo']}", "--body", "@dependabot rebase"])
            lines.append(f"{ref}: not merged, asked Dependabot to rebase ({m.stderr.strip()[:70]})")
        time.sleep(3)
    if not lines:
        return "Nothing eligible to merge."
    return ("DRY RUN (no changes).\n" if dry_run else "") + "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
