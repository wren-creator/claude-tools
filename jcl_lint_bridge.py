"""jcl-lint-bridge: a local, deterministic JCL syntax and convention linter,
so JCL gets checked before it is submitted (or before it is pasted into a
conversation for review) without spending model tokens on the mechanical
stuff. No model, no network, no mainframe connection, just parsing.

Two tools:
- lint_jcl(jcl) - lint JCL passed inline.
- lint_jcl_file(path) - lint a JCL file on disk.

What it catches: records over 80 columns, lowercase outside quotes, names
over 8 characters, unknown operations, broken continuations (trailing comma
with no continuation line, continuation text outside columns 4 to 16),
missing JOB, DD before the first EXEC, duplicate step and DD names, EXEC
with no PGM or procedure, unterminated or unbalanced IF/ELSE/ENDIF,
misspelled DD/EXEC/JOB keywords (with a "did you mean"), bad dataset names
and members, bad DISP values, DISP missing or NEW with no SPACE, a NEW
dataset that is deleted at step end, BLKSIZE that doesn't fit LRECL/RECFM,
backward references to a step that doesn't exist, PARM over 100 characters,
and PASSWORD= on a JOB statement.

What it does NOT do: expand procedures or INCLUDE members, resolve symbolic
parameters, check the catalog, or know your site's JES and SMS rules. It is
a first pass that catches typos and structure mistakes. The system's own
TYPRUN=SCAN is still the authority on whether the job will run.
"""
from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("jcl-lint-bridge")

LOG_PATH = Path(__file__).parent / "jcl_lint_log.jsonl"
ALLOWED_EXTENSIONS = {".jcl", ".cntl", ".proc", ".prc", ".job", ".sub", ".txt"}
MAX_BYTES = 200_000
MAX_FINDINGS = 60

OPERATIONS = {
    "JOB", "EXEC", "DD", "PROC", "PEND", "SET", "IF", "THEN", "ELSE", "ENDIF",
    "INCLUDE", "JCLLIB", "OUTPUT", "XMIT", "COMMAND", "CNTL", "ENDCNTL", "EXPORT",
}
JOB_KEYWORDS = {
    "ADDRSPC", "BYTES", "CARDS", "CCSID", "CLASS", "COND", "DSENQSHR", "GROUP",
    "JESLOG", "JOBRC", "LINES", "MEMLIMIT", "MSGCLASS", "MSGLEVEL", "NOTIFY",
    "PAGES", "PASSWORD", "PERFORM", "PRTY", "RD", "REGION", "RESTART", "SECLABEL",
    "SCHENV", "SYSAFF", "SYSTEM", "TIME", "TYPRUN", "USER", "UJOBCORR",
}
EXEC_KEYWORDS = {
    "ACCT", "ADDRSPC", "CCSID", "COND", "DYNAMNBR", "MEMLIMIT", "PARM", "PERFORM",
    "PGM", "PROC", "RD", "REGION", "REGIONX", "RLSTMOUT", "TIME",
}
DD_KEYWORDS = {
    "AMP", "AVGREC", "BLKSIZE", "BLKSZLIM", "BUFNO", "CCSID", "CHARS", "CHKPT",
    "CNTL", "COPIES", "DATACLAS", "DCB", "DDNAME", "DEST", "DISP", "DLM", "DSID",
    "DSKEYLBL", "DSN", "DSNAME", "DSNTYPE", "EATTR", "EXPDT", "FCB", "FILEDATA",
    "FLASH", "FREE", "KEYLABL1", "KEYLABL2", "KEYENCD1", "KEYENCD2", "KEYLEN",
    "KEYOFF", "LABEL", "LGSTREAM", "LIKE", "LRECL", "MAXGENS", "MGMTCLAS", "MODIFY",
    "OUTLIM", "OUTPUT", "PATH", "PATHDISP", "PATHMODE", "PATHOPTS", "PROTECT",
    "RECFM", "RECORG", "REFDD", "RETPD", "RLS", "ROACCESS", "SECMODEL", "SEGMENT",
    "SPACE", "SPIN", "STORCLAS", "SUBSYS", "SYSOUT", "TERM", "UCS", "UNIT", "VOL",
    "VOLUME",
}
DISP_STATUS = {"NEW", "OLD", "SHR", "MOD"}
DISP_END = {"DELETE", "KEEP", "PASS", "CATLG", "UNCATLG"}

NAME_RE = re.compile(r"[A-Z@#$][A-Z0-9@#$]{0,7}")
QUAL_RE = re.compile(r"[A-Z@#$][A-Z0-9@#$-]{0,7}")
GDG_RE = re.compile(r"[+-]?\d{1,4}")
REFBACK_RE = re.compile(r"\*\.([A-Z@#$][A-Z0-9@#$]*)\.")

Finding = tuple[int, str, str]


def _log(entry: dict) -> None:
    entry["timestamp"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _split_params_field(s: str) -> tuple[str, bool]:
    """The parameter field ends at the first blank outside apostrophes; what
    follows is a comment. Returns (field, still_inside_quote)."""
    in_quote = False
    for i, ch in enumerate(s):
        if ch == "'":
            in_quote = not in_quote
        elif ch == " " and not in_quote:
            return s[:i], False
    return s, in_quote


def _split_top(s: str) -> list[str]:
    """Split on commas at paren depth 0, outside apostrophes."""
    items, depth, in_quote, cur = [], 0, False, []
    for ch in s:
        if ch == "'":
            in_quote = not in_quote
        elif not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                items.append("".join(cur))
                cur = []
                continue
        cur.append(ch)
    items.append("".join(cur))
    return [i for i in items if i]


def _outside_quotes(s: str) -> str:
    out, in_quote = [], False
    for ch in s:
        if ch == "'":
            in_quote = not in_quote
            continue
        if not in_quote:
            out.append(ch)
    return "".join(out)


def _kv(item: str) -> tuple[str, str] | None:
    """('KEY', 'value') for a top-level KEY=value item, else None."""
    m = re.match(r"([A-Z0-9@#$.]+)=(.*)", item, re.S)
    return (m.group(1), m.group(2)) if m else None


def _unknown_keyword(key: str, known: set[str]) -> str:
    near = difflib.get_close_matches(key, known, n=1, cutoff=0.7)
    return f"unknown keyword {key}=" + (f", did you mean {near[0]}=?" if near else "")


class _Stmt:
    def __init__(self, line: int, name: str, op: str, params: str, nlines: int):
        self.line, self.name, self.op, self.params, self.nlines = line, name, op, params, nlines


def _parse_statements(lines: list[str], out: list[Finding]) -> list[tuple[_Stmt, str | None]]:
    """Walk the records, join continuations, skip in-stream data. Returns
    (statement, data_delimiter) pairs; data_delimiter is set when the DD
    starts in-stream data, and is the string that ends it."""
    stmts: list[tuple[_Stmt, str | None]] = []
    i, n = 0, len(lines)

    def skip_data(start: int, delim: str, is_data: bool) -> int:
        j = start
        while j < n:
            ln = lines[j]
            if ln.startswith(delim):
                return j + 1  # delimiter statement consumed
            if not is_data and delim == "/*" and ln.startswith("//"):
                return j  # DD * also ends at the next JCL statement
            j += 1
        out.append((start, "WARN", "in-stream data not terminated by a delimiter statement before end of input"))
        return n

    while i < n:
        raw = lines[i]
        ln = i + 1
        if len(raw) > 80:
            out.append((ln, "ERROR", f"record is {len(raw)} columns, JCL records are 80"))
        if "\t" in raw:
            out.append((ln, "WARN", "tab character, use blanks"))
        if raw.startswith("//*") or not raw.strip():
            i += 1
            continue
        if raw.startswith("/*"):
            i += 1
            continue
        if not raw.startswith("//"):
            out.append((ln, "ERROR", "record does not start with // or /* (lost continuation, or data outside a DD * ?)"))
            i += 1
            continue
        body = raw[2:72]
        if not body.strip():
            i += 1  # null statement, ends the job
            continue

        name = ""
        if body[0] != " ":
            name = body.split()[0]
            rest = body[len(name):].lstrip()
        else:
            rest = body.lstrip()
        parts = rest.split(None, 1)
        if not parts:
            out.append((ln, "ERROR", "statement has a name but no operation"))
            i += 1
            continue
        op = parts[0]
        after = parts[1] if len(parts) > 1 else ""
        if op == "IF":
            params, quote_open = after.strip(), False
        else:
            params, quote_open = _split_params_field(after)
        nlines = 1
        j = i + 1
        while params.endswith(",") or quote_open:
            # skip comment records between continuation lines
            while j < n and lines[j].startswith("//*"):
                j += 1
            if j >= n or not lines[j].startswith("//") or lines[j].startswith("//*"):
                out.append((ln, "ERROR", "line ends with a comma but the next record is not a continuation"))
                break
            cont = lines[j]
            cbody = cont[2:72]
            if cbody[:1] != " " and cbody.strip():
                out.append((j + 1, "ERROR", f"previous line ends with a comma but this starts a new statement ({cbody.split()[0]}); missing continuation or stray comma"))
                break
            if quote_open:
                chunk = cont[15:72] if len(cont) > 15 else ""
                params += chunk.rstrip()
                quote_open = chunk.count("'") % 2 == 1
            else:
                text = cbody.lstrip()
                start_col = 3 + (len(cbody) - len(text))
                if text and (start_col < 4 or start_col > 16):
                    out.append((j + 1, "ERROR", f"continuation text starts in column {start_col}, must be columns 4 to 16"))
                field, quote_open = _split_params_field(text)
                params += field
            nlines += 1
            j += 1
        if op not in OPERATIONS:
            near = difflib.get_close_matches(op, OPERATIONS, n=1, cutoff=0.6)
            out.append((ln, "ERROR", f"unknown JCL operation '{op}'" + (f", did you mean {near[0]}?" if near else "")))
        stmt = _Stmt(ln, name, op, params, nlines)
        i = j
        delim = None
        if op == "DD":
            items = _split_top(params)
            instream = None
            if "*" in items:
                instream = False
            elif "DATA" in items:
                instream = True
            if instream is not None:
                delim = "/*"
                for it in items:
                    kv = _kv(it)
                    if kv and kv[0] == "DLM":
                        delim = kv[1].strip("'")[:2] or "/*"
                i = skip_data(i, delim, instream)
        stmts.append((stmt, delim))
    return stmts


def _check_name(stmt: _Stmt, out: list[Finding]) -> None:
    if not stmt.name:
        return
    for part in stmt.name.split("."):
        if not NAME_RE.fullmatch(part):
            why = "longer than 8 characters" if len(part) > 8 else "must start with a letter, #, @ or $ and use only A-Z, 0-9, #, @, $"
            out.append((stmt.line, "ERROR", f"name '{stmt.name}' is invalid, {why}"))
            return
    if stmt.name.count(".") > 2:
        out.append((stmt.line, "ERROR", f"name '{stmt.name}' has too many qualifiers"))


def _check_dsn(value: str, line: int, step_names: set[str], out: list[Finding]) -> None:
    v = value.strip()
    if v.startswith("*"):
        return  # backward reference, checked separately
    if "&" in v and not v.startswith("&&"):
        return  # symbolic, can't judge without the proc
    member = None
    m = re.fullmatch(r"(.*?)\(([^()]*)\)", v)
    if m:
        v, member = m.group(1), m.group(2)
    if v.startswith("&&"):
        if not NAME_RE.fullmatch(v[2:]):
            out.append((line, "ERROR", f"temporary dataset name '{v}' is invalid"))
        return
    if len(v) > 44:
        out.append((line, "ERROR", f"dataset name '{v}' is {len(v)} characters, max is 44"))
    for q in v.split("."):
        if not q:
            out.append((line, "ERROR", f"dataset name '{v}' has an empty qualifier"))
            break
        if not QUAL_RE.fullmatch(q):
            why = "over 8 characters" if len(q) > 8 else "must start with a letter, #, @ or $"
            out.append((line, "ERROR", f"qualifier '{q}' in '{v}' is invalid, {why}"))
            break
    if member is not None and not (NAME_RE.fullmatch(member) or GDG_RE.fullmatch(member)):
        out.append((line, "ERROR", f"member or generation '({member})' is invalid"))


def _check_disp(items: dict[str, str], line: int, out: list[Finding]) -> None:
    raw = items["DISP"].strip()
    inner = raw[1:-1] if raw.startswith("(") and raw.endswith(")") else raw
    # (NEW,CATLG,DELETE), (,CATLG), (OLD), SHR ...
    sub = [s.strip() for s in inner.split(",")]
    status = sub[0] or "NEW"
    normal = sub[1] if len(sub) > 1 else ""
    abnormal = sub[2] if len(sub) > 2 else ""
    if status not in DISP_STATUS:
        out.append((line, "ERROR", f"DISP status '{status}' is not NEW, OLD, SHR or MOD"))
    for label, val in (("normal", normal), ("abnormal", abnormal)):
        if val and val not in DISP_END:
            out.append((line, "ERROR", f"DISP {label} disposition '{val}' is not one of {', '.join(sorted(DISP_END))}"))
    if len(sub) > 3:
        out.append((line, "ERROR", "DISP has more than three subparameters"))
    dsn = items.get("DSN", items.get("DSNAME", ""))
    if status == "NEW" and "SPACE" not in items and not {"LIKE", "DATACLAS", "REFDD", "PATH", "SUBSYS"} & items.keys():
        out.append((line, "WARN", "DISP NEW with no SPACE (fine only if an SMS data class supplies it)"))
    if status == "NEW" and normal in ("", "DELETE") and dsn and not dsn.startswith("&&") and "&" not in dsn:
        out.append((line, "WARN", "DISP NEW without CATLG or KEEP, the dataset is deleted when the step ends"))


def _check_dcb(joined: str, line: int, out: list[Finding]) -> None:
    lrecl = re.search(r"LRECL=(\d+)", joined)
    blk = re.search(r"BLKSIZE=(\d+)", joined)
    recfm = re.search(r"RECFM=([A-Z]+)", joined)
    if not (lrecl and blk and recfm):
        return
    l, b, r = int(lrecl.group(1)), int(blk.group(1)), recfm.group(1)
    if b == 0:
        return
    if "V" in r:
        if b < l + 4:
            out.append((line, "ERROR", f"BLKSIZE={b} is less than LRECL+4 ({l + 4}) for RECFM={r}"))
    elif "F" in r and b % l != 0:
        out.append((line, "ERROR", f"BLKSIZE={b} is not a multiple of LRECL={l} for RECFM={r}"))


def _lint(jcl: str) -> tuple[list[Finding], dict]:
    out: list[Finding] = []
    lines = jcl.splitlines()
    stmts = _parse_statements(lines, out)

    seen_job = False
    in_proc = False
    proc_file = False
    step_names: set[str] = set()
    dd_names: set[str] = set()
    current_step = False
    exec_count = 0
    total_steps = 0
    if_depth = 0

    def close_job(at: int) -> None:
        if seen_job and not proc_file and exec_count == 0:
            out.append((at, "ERROR", "job has no EXEC statement"))

    first = True
    job_line = 0
    for stmt, _ in stmts:
        op, ln = stmt.op, stmt.line
        if first:
            first = False
            if op == "PROC":
                proc_file = True
            elif op != "JOB":
                out.append((ln, "ERROR", f"first statement must be JOB, found {op}"))
        _check_name(stmt, out)

        masked = _outside_quotes(stmt.params) if op != "IF" else _outside_quotes(stmt.params)
        if re.search(r"[a-z]", masked):
            out.append((ln, "ERROR", "lowercase characters outside apostrophes, JCL must be uppercase"))
        if stmt.name and re.search(r"[a-z]", stmt.name):
            out.append((ln, "ERROR", f"lowercase name '{stmt.name}'"))

        items = _split_top(stmt.params) if op != "IF" else []
        kvs: dict[str, str] = {}
        for it in items:
            kv = _kv(it)
            if kv:
                kvs[kv[0]] = kv[1]

        if op == "JOB":
            close_job(job_line)
            seen_job, job_line = True, ln
            step_names, dd_names, current_step, exec_count = set(), set(), False, 0
            if_depth = 0
            if not stmt.name:
                out.append((ln, "ERROR", "JOB statement needs a name"))
            for k in kvs:
                if k not in JOB_KEYWORDS:
                    out.append((ln, "WARN", _unknown_keyword(k, JOB_KEYWORDS)))
            if "PASSWORD" in kvs:
                out.append((ln, "ERROR", "PASSWORD= on a JOB statement puts a credential in the JCL; remove it and let the submitter's security ID carry"))
        elif op == "EXEC":
            exec_count += 1
            total_steps += 1
            current_step = True
            dd_names = set()
            if stmt.name:
                if stmt.name in step_names:
                    out.append((ln, "ERROR", f"duplicate step name '{stmt.name}'"))
                step_names.add(stmt.name)
            positional = [it for it in items if _kv(it) is None]
            is_proc = "PROC" in kvs or (positional and "PGM" not in kvs)
            if "PGM" not in kvs and "PROC" not in kvs and not positional:
                out.append((ln, "ERROR", "EXEC needs PGM= or a procedure name"))
            if "PGM" in kvs:
                pgm = kvs["PGM"]
                if not pgm.startswith("*") and not NAME_RE.fullmatch(pgm):
                    out.append((ln, "ERROR", f"PGM={pgm} is not a valid program name"))
                for k in kvs:
                    if k not in EXEC_KEYWORDS:
                        out.append((ln, "WARN", _unknown_keyword(k, EXEC_KEYWORDS)))
            if "PARM" in kvs:
                parm = kvs["PARM"]
                if parm.startswith("'") and parm.endswith("'") and len(parm) >= 2:
                    plain = parm[1:-1].replace("''", "'").replace("&&", "&")
                else:
                    plain = parm
                    if not parm.startswith("(") and re.search(r"[/=+*]", parm):
                        out.append((ln, "WARN", "PARM has special characters but is not in apostrophes"))
                if len(plain) > 100:
                    out.append((ln, "ERROR", f"PARM is {len(plain)} characters, max is 100"))
        elif op == "DD":
            if not current_step and not in_proc and stmt.name not in ("JOBLIB", "JOBCAT"):
                out.append((ln, "ERROR", "DD statement before the first EXEC (only JOBLIB is allowed there)"))
            if stmt.name and "." not in stmt.name:
                if stmt.name in dd_names:
                    out.append((ln, "ERROR", f"duplicate DD name '{stmt.name}' in this step"))
                dd_names.add(stmt.name)
            for k in kvs:
                if k not in DD_KEYWORDS:
                    out.append((ln, "WARN", _unknown_keyword(k, DD_KEYWORDS)))
            dsn_key = "DSN" if "DSN" in kvs else "DSNAME" if "DSNAME" in kvs else ""
            if dsn_key:
                _check_dsn(kvs[dsn_key], ln, step_names, out)
            sysoutish = any(k in kvs for k in ("SYSOUT", "PATH", "DDNAME", "SUBSYS", "REFDD", "OUTPUT")) or \
                any(it in ("DUMMY", "DYNAM", "*", "DATA") for it in items)
            if "DISP" in kvs:
                _check_disp(kvs, ln, out)
            elif dsn_key and not sysoutish and "SPACE" not in kvs and not kvs[dsn_key].startswith("*"):
                out.append((ln, "WARN", "DSN with no DISP defaults to NEW,DELETE; for an existing dataset you almost certainly want DISP=SHR or OLD"))
            for m in REFBACK_RE.finditer(masked):
                ref = m.group(1)
                if ref not in step_names:
                    out.append((ln, "ERROR", f"refers back to step '{ref}' which is not defined earlier in this job"))
            _check_dcb(stmt.params, ln, out)
        elif op == "PROC":
            in_proc = True
            current_step = False
            dd_names = set()
        elif op == "PEND":
            if not in_proc:
                out.append((ln, "ERROR", "PEND with no matching PROC"))
            in_proc = False
        elif op == "IF":
            if_depth += 1
            if not re.search(r"\bTHEN\b", stmt.params):
                out.append((ln, "WARN", "IF without THEN on the same record (multi-record IF is not checked)"))
        elif op == "ELSE":
            if if_depth == 0:
                out.append((ln, "ERROR", "ELSE with no open IF"))
        elif op == "ENDIF":
            if if_depth == 0:
                out.append((ln, "ERROR", "ENDIF with no open IF"))
            else:
                if_depth -= 1
        elif op == "JCLLIB" and "ORDER" not in kvs:
            out.append((ln, "ERROR", "JCLLIB needs ORDER="))
        elif op == "INCLUDE" and "MEMBER" not in kvs:
            out.append((ln, "ERROR", "INCLUDE needs MEMBER="))

        # quote balance on the whole joined statement
        if op != "IF" and stmt.params.count("'") % 2 == 1:
            out.append((ln, "ERROR", "unbalanced apostrophe in parameters"))

    close_job(job_line)
    if if_depth > 0:
        out.append((len(lines), "ERROR", f"{if_depth} IF statement(s) never closed with ENDIF"))
    if in_proc and not proc_file:
        out.append((len(lines), "WARN", "PROC with no PEND"))

    out.sort(key=lambda f: (f[0], f[1] != "ERROR"))
    return out, {"statements": len(stmts), "steps": total_steps}


def _format(findings: list[Finding], stats: dict, lines: int) -> str:
    errors = sum(1 for f in findings if f[1] == "ERROR")
    warns = len(findings) - errors
    head = f"{stats['statements']} statements, {stats['steps']} step(s), {lines} records."
    if not findings:
        return f"CLEAN: {head} Syntax and structure look right. This is a lint, not TYPRUN=SCAN."
    rows = [f"{errors} error(s), {warns} warning(s). {head}"]
    for n, sev, msg in findings[:MAX_FINDINGS]:
        rows.append(f"  L{n:<4} {sev:<5} {msg}")
    if len(findings) > MAX_FINDINGS:
        rows.append(f"  ... {len(findings) - MAX_FINDINGS} more not shown, fix the above first.")
    return "\n".join(rows)


@mcp.tool()
def lint_jcl(jcl: str) -> str:
    """Lint z/OS JCL locally, deterministically (no model, no mainframe
    connection). Run it on any JCL before submitting it or asking for a
    review, and fix the flagged lines. Catches column and continuation
    mistakes, bad names, misspelled keywords, DD before EXEC, duplicate step
    or DD names, bad DISP and dataset names, BLKSIZE/LRECL mismatches,
    unbalanced IF/ENDIF, bad backward references, and PASSWORD= on a JOB.

    It does not expand procedures or INCLUDE members, resolve symbols, check
    the catalog, or know site rules. A clean result means no mechanical
    mistakes found, not that the job will run; TYPRUN=SCAN is still the
    authority. Returns 'CLEAN: ...' or line-numbered ERROR/WARN findings.
    """
    findings, stats = _lint(jcl)
    _log({"tool": "lint_jcl", "records": len(jcl.splitlines()),
          "errors": sum(1 for f in findings if f[1] == "ERROR"),
          "warnings": sum(1 for f in findings if f[1] == "WARN")})
    return _format(findings, stats, len(jcl.splitlines()))


@mcp.tool()
def lint_jcl_file(path: str) -> str:
    """Same as lint_jcl for a file on disk (absolute path; .jcl, .cntl,
    .proc, .prc, .job, .sub or .txt; max 200KB), so a long job stream never
    has to be read into the conversation just to be checked."""
    p = Path(path).expanduser()
    if p.suffix.lower() not in ALLOWED_EXTENSIONS:
        return f"Error: only {', '.join(sorted(ALLOWED_EXTENSIONS))} files can be linted."
    try:
        if p.stat().st_size > MAX_BYTES:
            return f"Error: {p.name} is over the {MAX_BYTES} byte limit."
        jcl = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return f"Error reading {path}: {e}"
    findings, stats = _lint(jcl)
    _log({"tool": "lint_jcl_file", "path": str(p), "records": len(jcl.splitlines()),
          "errors": sum(1 for f in findings if f[1] == "ERROR"),
          "warnings": sum(1 for f in findings if f[1] == "WARN")})
    return _format(findings, stats, len(jcl.splitlines()))


if __name__ == "__main__":
    mcp.run()
