"""port-tower-bridge: a host-wide view of which ports are taken and by what,
across every Docker Compose project running on this machine, not just one
repo - built after packetriver and widgetorium collided over :8080 while
running side by side during the Packet River expansion-pack work.

Three tools:
- scan_ports() - live inventory of every listening TCP/UDP port on the
  host. Docker ports are attributed to their compose project, working
  directory, and container (via `docker inspect`); everything else is
  attributed to whatever process holds it (via `lsof`).
- check_ports(ports) - check a specific list of port numbers against the
  live scan.
- check_compose_ports(compose_dir, files) - read the host ports a project's
  own compose file(s) would publish (`docker compose config --format
  json`) and check those against the live scan, so a collision surfaces
  before `docker compose up` fails partway through bringing up a whole
  stack.

Everything here shells out to `docker` and `lsof`, both already on the
machine - no new dependencies.
"""
from __future__ import annotations

import json
import subprocess

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("port-tower-bridge")

TIMEOUT = 20


def _run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd)


def _docker_port_map() -> dict[tuple[str, str, str], dict]:
    """(host_ip, host_port, proto) -> {project, working_dir, container, container_port}"""
    ids = _run(["docker", "ps", "-q"]).stdout.split()
    if not ids:
        return {}
    insp = _run(["docker", "inspect", *ids])
    try:
        containers = json.loads(insp.stdout)
    except json.JSONDecodeError:
        return {}
    out: dict[tuple[str, str, str], dict] = {}
    for c in containers:
        name = c.get("Name", "").lstrip("/")
        labels = (c.get("Config", {}) or {}).get("Labels", {}) or {}
        project = labels.get("com.docker.compose.project", "(no compose project)")
        working_dir = labels.get("com.docker.compose.project.working_dir", "")
        ports = (c.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
        for cport_proto, bindings in ports.items():
            if not bindings:
                continue
            proto = cport_proto.rpartition("/")[2] or "tcp"
            for b in bindings:
                host_ip = b.get("HostIp") or "0.0.0.0"
                host_port = b.get("HostPort")
                if not host_port:
                    continue
                out[(host_ip, host_port, proto)] = {
                    "project": project, "working_dir": working_dir,
                    "container": name, "container_port": cport_proto,
                }
    return out


def _lsof_listeners() -> list[dict]:
    """Every listening TCP/UDP socket on the host, via lsof."""
    rows = []
    for proto, args in (("tcp", ["-iTCP", "-sTCP:LISTEN"]), ("udp", ["-iUDP"])):
        r = _run(["lsof", "-nP", *args])
        for line in r.stdout.splitlines()[1:]:  # skip header
            parts = line.split()
            if len(parts) < 3:
                continue
            command, pid = parts[0], parts[1]
            # NAME is the last field, or second-to-last when a "(LISTEN)"
            # state marker trails it as its own whitespace-separated token.
            if parts[-1].startswith("(") and parts[-1].endswith(")"):
                addr_port = parts[-2]
            else:
                addr_port = parts[-1]
            if "->" in addr_port:
                continue  # a connected socket, not a listener
            addr, _, port = addr_port.rpartition(":")
            if not port.isdigit():
                continue
            host_ip = "0.0.0.0" if addr in ("*", "") else addr
            rows.append({"command": command, "pid": pid, "host_ip": host_ip,
                         "port": port, "proto": proto})
    return rows


def _scan() -> list[dict]:
    docker_map = _docker_port_map()
    rows = []
    seen = set()
    for l in _lsof_listeners():
        key = (l["host_ip"], l["port"], l["proto"])
        if key in seen:
            continue
        seen.add(key)
        match = None
        for candidate in (key, ("0.0.0.0", l["port"], l["proto"]), ("127.0.0.1", l["port"], l["proto"])):
            if candidate in docker_map:
                match = docker_map[candidate]
                break
        if match:
            where = match["working_dir"] or match["project"]
            owner = f"docker: {match['project']} ({where}) / {match['container']}"
        else:
            owner = f"process: {l['command']} (pid {l['pid']})"
        rows.append({"port": l["port"], "proto": l["proto"], "host_ip": l["host_ip"], "owner": owner})
    rows.sort(key=lambda r: (int(r["port"]), r["proto"]))
    return rows


@mcp.tool()
def scan_ports() -> str:
    """Host-wide inventory of every listening TCP/UDP port right now,
    across every running Docker Compose project (not just one repo) plus
    anything non-Docker holding a port. Docker ports are attributed to
    their compose project, working directory, and container via `docker
    inspect`; anything else is attributed to whatever process holds it via
    `lsof`. Use this before starting a new docker-compose project to see
    what is already taken and by which other project.
    """
    rows = _scan()
    if not rows:
        return "No listening TCP/UDP ports found (or lsof/docker not available)."
    lines = [f"{'PORT':<7} {'PROTO':<5} {'ADDR':<15} OWNER"]
    for r in rows:
        lines.append(f"{r['port']:<7} {r['proto']:<5} {r['host_ip']:<15} {r['owner']}")
    return "\n".join(lines)


@mcp.tool()
def check_ports(ports: list[int]) -> str:
    """Check a specific list of host ports against the live scan and report
    which are free and which are already taken, and by what (docker
    project + container, or a plain process). Use this to sanity-check a
    project's intended port list (e.g. copied from its .env.example) before
    starting it.
    """
    live = {int(r["port"]): r for r in _scan()}
    lines = []
    taken = []
    for p in ports:
        if p in live:
            lines.append(f"{p:<7} TAKEN  by {live[p]['owner']}")
            taken.append(p)
        else:
            lines.append(f"{p:<7} free")
    header = (f"{len(taken)}/{len(ports)} requested port(s) already taken.\n"
              if taken else "All requested ports are free.\n")
    return header + "\n".join(lines)


@mcp.tool()
def check_compose_ports(compose_dir: str, files: list[str] | None = None) -> str:
    """Before starting a docker-compose project, read the host ports its
    compose file(s) would actually publish (`docker compose ... config
    --format json`, each service's `ports[].published`) and check them
    against the live host-wide scan - so a collision (like packetriver vs
    widgetorium both wanting :8080) surfaces up front instead of partway
    through `docker compose up` bringing up a whole stack.

    compose_dir: absolute path to the project (used as the cwd for the
    compose call). files: compose file names relative to compose_dir, in
    order, e.g. ["docker-compose.yml", "docker-compose.segmented.yml"].
    Defaults to ["docker-compose.yml"].
    """
    files = files or ["docker-compose.yml"]
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", f]
    cmd += ["config", "--format", "json"]
    r = _run(cmd, cwd=compose_dir)
    if r.returncode != 0:
        return f"Could not read compose config in {compose_dir}: {r.stderr.strip()}"
    try:
        cfg = json.loads(r.stdout)
    except json.JSONDecodeError:
        return f"Could not parse compose config JSON from {compose_dir}."

    wanted = []
    for svc_name, svc in (cfg.get("services") or {}).items():
        for p in svc.get("ports") or []:
            published = p.get("published")
            if published:
                wanted.append((svc_name, int(published)))

    if not wanted:
        return f"No published ports found in {compose_dir} ({', '.join(files)})."

    ports_only = sorted({p for _, p in wanted})
    report = check_ports(ports_only)
    by_service = "\n".join(f"  {svc}: {port}" for svc, port in sorted(wanted, key=lambda x: x[1]))
    return f"{compose_dir} ({', '.join(files)}) wants to publish:\n{by_service}\n\n{report}"


if __name__ == "__main__":
    mcp.run()
