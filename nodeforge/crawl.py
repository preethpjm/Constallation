"""Folder / git crawler: point it at a place where tools live and it works out what is in there.

  1. Get the code (local folder, or git clone into an empty directory; nothing is executed by git).
  2. Read it WITHOUT running it: find tool directories, loose scripts, services and library functions.
  3. Triage every finding: onboard (it has a CLI or a service), wrap (a function that is a converter),
     or skip (with the reason: tests, GUI, needs a compiler this machine lacks, touches the network...).
  4. Only if asked, onboard and wrap the findings (that is when code finally runs, in the sandbox),
     then generate their skills.

Crawled repositories are untrusted data. Reading is static; running happens only through the normal
onboarding sandbox and only when the caller passes onboard=True.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from . import llm, probe, sandbox, wrap
from .probe import IGNORE_DIRS, SOURCE_EXT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRAWL_DIR = os.path.join(ROOT, "crawled")
CONTAINER_DIRS = {"tools", "scripts", "apps", "services", "packages", "cmd", "utils", "bin", "src", "projects"}
MAX_BYTES = 300 * 1024 * 1024


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "repo"


def fetch(source: str, log=print) -> tuple[str, str]:
    """-> (local path, name). A folder is used in place; a git URL is cloned shallowly into crawled/<name>/repo."""
    if os.path.isdir(source):
        return os.path.abspath(source), slug(os.path.basename(os.path.abspath(source).rstrip("/\\")))
    if source.startswith("-") or not re.match(r"^(https://|ssh://|git@|file://)", source):
        raise ValueError("source must be an existing folder or a git URL (https://, ssh://, git@, file://)")
    name = slug(re.sub(r"\.git$", "", source.rstrip("/")).split("/")[-1].split(":")[-1])
    dest = os.path.join(CRAWL_DIR, name, "repo")
    if os.path.exists(dest):
        sandbox.rmtree(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    log(f"cloning {source} (shallow) ...")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never",
                        "clone", "--depth", "1", "--single-branch", "--no-tags", "--no-recurse-submodules",
                        "--", source, dest], capture_output=True, text=True, timeout=300, env=env)
    if r.returncode != 0:
        raise RuntimeError("git clone failed: " + (r.stderr or r.stdout).strip()[-400:])
    size = sum(os.path.getsize(os.path.join(c, f)) for c, _, fs in os.walk(dest) for f in fs
               if ".git" not in c.split(os.sep))
    if size > MAX_BYTES:
        sandbox.rmtree(dest)
        raise RuntimeError(f"repository is {size // 1_000_000} MB of files; the limit is {MAX_BYTES // 1_000_000} MB")
    return dest, name


def _readme_line(ev) -> str:
    for rel, txt in sorted(ev["docs"].items(), key=lambda kv: len(kv[0])):
        for line in txt.splitlines():
            line = line.strip(" #*>-_`")
            if len(line) > 25 and not line.lower().startswith(("http", "[", "!", "license", "table of")):
                return line[:220]
    return ""


def _classify(path: str, ev: dict, repo_root: str) -> dict | None:
    cands = [c for c in ev["entry_candidates"]]
    best = cands[0] if cands else None
    rel = os.path.relpath(path, repo_root)
    base = {"path": rel, "language": next(iter(sorted(ev["languages"], key=lambda k: -ev["languages"][k])), "unknown"),
            "about": _readme_line(ev)}
    if ev["services"]:
        s = ev["services"][0]
        return {**base, "kind": "service", "entry": s["path"], "framework": s["framework"], "action": "onboard",
                "why": f"{s['framework']} service: becomes an http node"}
    if best and best["score"] >= 3:
        if best.get("runtime_problem"):
            return {**base, "kind": "tool", "entry": best["source"], "action": "skip",
                    "why": "this machine can't run it: " + best["runtime_problem"][:140]}
        has_spec = best["source"] in ev["argparse"]
        return {**base, "kind": "tool", "entry": best["source"], "action": "onboard",
                "why": ("argparse CLI read from source" if has_spec else "has an entry point; its --help is probed")
                + (f", {len(ev['samples'])} sample file(s)" if ev["samples"] else "")}
    return None


def _units(root: str) -> list[dict]:
    units, claimed = [], []

    def claim(p):
        claimed.append(os.path.abspath(p))

    def is_claimed(p):
        p = os.path.abspath(p)
        return any(p == c or p.startswith(c + os.sep) for c in claimed)

    def child_dirs(d):
        try:
            return sorted(x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)) and x not in IGNORE_DIRS
                          and not x.startswith("."))
        except OSError:
            return []
    cand_dirs = []
    for d in child_dirs(root):
        cand_dirs.append(os.path.join(root, d))
        if d in CONTAINER_DIRS:
            cand_dirs += [os.path.join(root, d, x) for x in child_dirs(os.path.join(root, d))]
    for d in cand_dirs:
        if os.path.basename(d) in CONTAINER_DIRS:
            continue  # a folder of tools is not a tool: its children and loose scripts are the units
        if re.search(r"(^|[/\\])(tests?|docs?|examples?|samples?|fixtures?|node_modules|\.github)([/\\]|$)", os.path.relpath(d, root)):
            continue
        u = _classify(d, probe.scan(d), root)
        if u:
            units.append(u)
            claim(d)
    # loose scripts: one script = one tool
    for d in [root] + [os.path.join(root, c) for c in CONTAINER_DIRS if os.path.isdir(os.path.join(root, c))]:
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if os.path.isfile(p) and os.path.splitext(f)[1].lower() in SOURCE_EXT and not is_claimed(p) \
                    and not f.startswith(("test_", "conftest", "setup")):
                u = _classify(p, probe.scan(p), root)
                if u and u["kind"] == "tool":
                    u["kind"] = "script"
                    units.append(u)
                    claim(p)
    if not units:  # the repo itself is the tool
        u = _classify(root, probe.scan(root), root)
        if u:
            u["path"] = "."
            units.append(u)
            claim(root)
    return units


def _libraries(root: str, claimed_units: list[dict], limit: int) -> list[dict]:
    """Public functions that look like converters, in Python files that are not already tools."""
    tool_files = {os.path.normpath(os.path.join(root, u["path"], u["entry"])) if u["path"] != "." else
                  os.path.normpath(os.path.join(root, u["entry"])) for u in claimed_units}
    found = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".") and
                   d not in ("tests", "test", "docs", "examples")]
        for f in files:
            if not f.endswith(".py") or f.startswith(("test_", "conftest", "setup", "__")):
                continue
            p = os.path.join(cur, f)
            if os.path.normpath(p) in tool_files:
                continue
            for fn in wrap.discover(p):
                if not fn["problems"] and not fn["side_effects"] and fn["score"] >= 7:
                    found.append({"kind": "library", "path": os.path.relpath(p, root), "function": fn["function"],
                                  "language": "python", "about": (fn["doc"].splitlines() or [""])[0][:200],
                                  "action": "wrap", "score": fn["score"],
                                  "why": "pure function with typed parameters: wrapped as a converter"})
    return sorted(found, key=lambda u: -u["score"])[:limit]


def crawl(source: str, onboard_units: bool = False, make_skills: bool = True, max_units: int = 12,
          use_llm: bool = True, log=print, skip_existing: bool = False) -> dict:
    path, name = fetch(source, log)
    log(f"reading {path} (nothing is executed in this step)")
    ev_root = probe.scan(path)
    units = _units(path)[:max_units]
    libs = _libraries(path, units, max(0, max_units - len(units)))
    summary = _readme_line(ev_root)
    if use_llm and llm.available() and ev_root["docs"]:
        try:
            summary = llm.complete("Summarise in two sentences what this repository is for and what kinds of tools it holds. "
                                   "Plain text.", probe.evidence_text(ev_root, 5000), 200).strip()
        except Exception:  # noqa
            pass
    report = {"source": source, "name": name, "path": path, "summary": summary,
              "languages": ev_root["languages"], "units": units + libs, "results": []}
    log(f"found {len(units)} tool(s)/service(s) and {len(libs)} wrappable function(s) in {len(ev_root['files'])} files")
    for u in report["units"]:
        log(f"  {u['action']:8} {u['kind']:8} {u['path']}" + (f":{u['function']}" if u.get("function") else "") + f"  - {u['why']}")
    if onboard_units:
        from .onboard import onboard
        from .manifest import load_all
        have = {k: m for k, m in load_all().items() if m.verification.status == "verified"}
        for u in report["units"]:
            if u["action"] == "onboard":
                target = os.path.join(path, u["path"]) if u["path"] != "." else path
                if skip_existing and any(os.path.normpath(m.tool_dir).startswith(os.path.normpath(target)) for m in have.values()):
                    log(f"  already registered, skipping {u['path']}")
                    continue
                if u["kind"] == "script":
                    target = os.path.join(path, u["path"])
                ms = onboard(target, f"{name}-{slug(os.path.splitext(os.path.basename(u['path']))[0] or name)}",
                             use_llm=use_llm, log=log)
                for m in ms:
                    m.origin = m.origin or ""
                    report["results"].append({"id": m.id, "status": m.verification.status})
            elif u["action"] == "wrap":
                if skip_existing and f"{name}-{slug(u['function'])}" in have:
                    log(f"  already registered, skipping {u['function']}")
                    continue
                try:
                    m = wrap.wrap(os.path.join(path, u["path"]), u["function"],
                                  f"{name}-{slug(u['function'])}", use_llm=use_llm, log=log)
                    report["results"].append({"id": m.id, "status": m.verification.status})
                except Exception as e:  # noqa
                    report["results"].append({"id": u["function"], "status": "failed", "error": str(e)})
        if make_skills:
            from . import skills
            skills.generate_all(polish=use_llm, log=log)
    out = os.path.join(CRAWL_DIR, name)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write(report_md(report))
    return report


def report_md(r: dict) -> str:
    rows = "\n".join(f"| {u['action']} | {u['kind']} | `{u['path']}{':' + u['function'] if u.get('function') else ''}` | "
                     f"{u['language']} | {u['why']} |" for u in r["units"]) or "| - | | | | |"
    res = "\n".join(f"- `{x['id']}`: {x['status']}" + (f" ({x['error']})" if x.get("error") else "") for x in r["results"])
    return f"""# Crawl: {r['name']}

{r['summary'] or '_No README summary._'}

Source: `{r['source']}` · Languages: {', '.join(f'{k} ({v})' for k, v in r['languages'].items()) or '-'}

| action | kind | where | language | why |
|---|---|---|---|---|
{rows}

## Onboarding results
{res or '_Not run yet. Findings above come from reading the code only._'}
"""
