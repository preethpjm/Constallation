"""Stage 1 of onboarding: gather EVIDENCE about an unknown tool.

Language-agnostic by design. We don't need to understand the code; we need
to find (a) how to start it, (b) what it says about itself when started,
(c) sample data it ships with. Static reading is only a hint; the --help
output and real runs are the ground truth.
"""
import ast
import json
import os
import re
import stat

from . import sandbox
import sys

from . import interp
from .types import EXT_MIME

IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
               ".idea", ".vscode", "target", "bin", "obj", ".mypy_cache"}
SOURCE_EXT = {".py": "python", ".js": "node", ".mjs": "node", ".cjs": "node", ".ts": "node",
              ".sh": "shell", ".bash": "shell", ".bat": "batch", ".ps1": "powershell",
              ".c": "c", ".cpp": "cpp", ".cc": "cpp", ".go": "go", ".rs": "rust",
              ".java": "java", ".cs": "csharp", ".rb": "ruby", ".php": "php", ".r": "r"}
BUILD_FILES = {"package.json", "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
               "Pipfile", "environment.yml", "Cargo.toml", "go.mod", "CMakeLists.txt", "Makefile",
               "makefile", "Dockerfile", "docker-compose.yml", "pom.xml", "build.gradle"}
SAMPLE_DIR_HINTS = ("example", "sample", "test", "fixture", "demo", "data", "input")


def _is_binary_exec(path):
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError:
        return None
    if magic == b"\x7fELF":
        return "elf"
    if magic[:2] == b"MZ":
        return "pe"
    if magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
        return "macho"
    return None


def scan(tool_dir: str) -> dict:
    ev = {"tool_dir": tool_dir, "files": [], "languages": {}, "docs": {}, "build": {},
          "sources": {}, "samples": [], "binaries": [], "entry_candidates": [], "services": [],
          "argparse": {}, "help": {}}
    single = os.path.isfile(tool_dir)
    root = os.path.dirname(tool_dir) if single else tool_dir
    walker = [(root, [], [os.path.basename(tool_dir)])] if single else os.walk(root)
    for cur, dirs, files in walker:
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for fn in sorted(files):
            p = os.path.join(cur, fn)
            rel = os.path.relpath(p, root)
            ext = os.path.splitext(fn)[1].lower()
            ev["files"].append(rel)
            if len(ev["files"]) > 3000:
                break
            low = fn.lower()
            if low.startswith("readme") or (ext in (".md", ".rst", ".txt") and "doc" in rel.lower()) \
                    or low in ("usage.md", "usage.txt", "help.txt"):
                ev["docs"][rel] = _read(p, 6000)
            if fn in BUILD_FILES:
                ev["build"][rel] = _read(p, 4000)
            if ext in SOURCE_EXT:
                lang = SOURCE_EXT[ext]
                ev["languages"][lang] = ev["languages"].get(lang, 0) + 1
                ev["sources"][rel] = _read(p, 20000)
            elif ext == ".jar":
                ev["binaries"].append({"path": rel, "kind": "jar"})
            else:
                bk = _is_binary_exec(p)
                if bk:
                    ev["binaries"].append({"path": rel, "kind": bk})
                elif ext in EXT_MIME and ext not in (".md",) and not fn in BUILD_FILES \
                        and not low.startswith(("readme", "license", "requirements", "package")):
                    in_sample_dir = any(h in rel.lower() for h in SAMPLE_DIR_HINTS)
                    ev["samples"].append({"path": rel, "ext": ext, "mime": EXT_MIME[ext],
                                          "sample_dir": in_sample_dir, "size": os.path.getsize(p)})
    _find_entries(ev, root)
    return ev


def _read(p, n):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(n)
    except OSError:
        return ""


RUNTIME_OF = {"python": "python", "node": "node", "shell": "bash", "powershell": "powershell",
              "batch": "cmd", "go": "go", "rust": "cargo", "jar": "java",
              "elf": "wsl" if os.name == "nt" else None, "macho": None,
              "pe": None if os.name == "nt" else "wine"}


def _with_runtime(lang, target):
    """-> (invoke, problem). The runtime is smoke-tested before use."""
    kind = RUNTIME_OF.get(lang)
    if kind is None:  # native binary for this OS
        return interp.exe_path(target), None
    pre, problem = interp.resolve(kind)
    if problem:
        return None, problem
    if lang in ("go", "rust"):
        return pre, None
    if lang in ("elf",):
        return f"{pre} ./{target.replace(os.sep, '/')}", None
    return f"{pre} {_q(target)}", None


def _find_entries(ev, root):
    cands = []
    has_makefile = any(os.path.basename(b).lower() == "makefile" for b in ev["build"])
    for rel, src in ev["sources"].items():
        ext = os.path.splitext(rel)[1].lower()
        lang = SOURCE_EXT[ext]
        score = 0
        base = os.path.basename(rel).lower()
        parts = set(rel.replace("\\", "/").lower().split("/")[:-1])
        if base in ("conftest.py", "setup.py", "__init__.py") or base.startswith("test_") or \
                re.search(r"(_test|\.test|\.spec)\.\w+$", base) or parts & {"tests", "test", "__tests__", "spec"}:
            continue  # tests/config/packaging files are not the tool's entry point
        if lang == "python":
            if "__main__" in src: score += 3
            if re.search(r"\b(argparse|click|typer|fire|sys\.argv)\b", src): score += 3
            if re.search(r"\b(FastAPI|Flask)\s*\(", src):
                ev["services"].append({"path": rel, "framework": "fastapi" if "FastAPI(" in src else "flask"})
            ap = _argparse_ast(src)
            if ap:
                ev["argparse"][rel] = ap
                score += 2
        elif lang == "node":
            if "process.argv" in src or "commander" in src or "yargs" in src: score += 4
            if src.startswith("#!"): score += 1
            if re.search(r"express\(\)", src):
                ev["services"].append({"path": rel, "framework": "express"})
        elif lang == "shell":
            score += 3
        elif lang == "powershell":
            score += 2
        elif lang == "batch":
            score += 1
        elif lang == "go" and "func main()" in src:
            score += 3
        elif lang == "rust" and "fn main()" in src:
            score += 3
        elif lang in ("c", "cpp") and re.search(r"\bint\s+main\s*\(", src):
            # needs a build; the binary is discovered after building
            make, _ = interp.resolve("make")
            cc, cc_problem = interp.resolve("cc")
            build, problem = None, None
            if has_makefile and make and cc:
                build = make
            elif cc:
                build = f"{cc} -O2 -o tool_bin {_q(rel)}" + (" -lstdc++" if lang == "cpp" else "")
            else:
                problem = cc_problem
            cands.append({"invoke": None, "source": rel, "lang": lang, "score": 2,
                          "needs_build": build, "runtime_problem": problem})
            continue
        if score > 0:
            inv, problem = _with_runtime(lang, rel)
            docs = " ".join(ev["docs"].values()) + " ".join(ev["build"].values())
            if os.path.basename(rel) in docs: score += 3
            cands.append({"invoke": inv, "source": rel, "lang": lang, "score": score, "runtime_problem": problem})
    pkg = ev["build"].get("package.json")
    if pkg:
        try:
            pj = json.loads(pkg)
            b = pj.get("bin")
            mains = list(b.values()) if isinstance(b, dict) else ([b] if b else [])
            for m in mains + ([pj["main"]] if pj.get("main") else []):
                for c in cands:
                    if os.path.normpath(c["source"]) == os.path.normpath(m):
                        c["score"] += 5
        except Exception:
            pass
    for b in ev["binaries"]:
        inv, problem = _with_runtime(b["kind"], b["path"])
        cands.append({"invoke": inv, "source": b["path"], "lang": b["kind"], "score": 6, "runtime_problem": problem})
    # runnable candidates first, then by score
    cands.sort(key=lambda c: (c.get("runtime_problem") is not None, -c["score"]))
    ev["entry_candidates"] = cands[:6]


def _q(p):
    return f'"{p}"' if " " in p else p


def _argparse_ast(src):
    """Exact CLI spec for Python argparse tools, read without executing them."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    args = []
    desc = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "ArgumentParser":
                for kw in node.keywords:
                    if kw.arg == "description" and isinstance(kw.value, ast.Constant):
                        desc = kw.value.value
            if node.func.attr == "add_argument":
                names = [a.value for a in node.args if isinstance(a, ast.Constant)]
                if not names:
                    continue
                kws = {}
                for kw in node.keywords:
                    try:
                        kws[kw.arg] = ast.literal_eval(kw.value)
                    except Exception:
                        kws[kw.arg] = ast.unparse(kw.value) if hasattr(ast, "unparse") else "?"
                args.append({"names": names, **{k: kws[k] for k in kws if k in
                             ("help", "default", "type", "required", "nargs", "action", "dest", "choices")}})
    if not args:
        return None
    return {"description": desc, "args": args}


def probe_help(ev: dict, max_cands: int = 3) -> None:
    """Actually start the top entry points and record what they say."""
    work = sandbox.fresh_copy(ev["tool_dir"], "help")
    built = False
    for c in ev["entry_candidates"][:max_cands]:
        if c.get("needs_build") and not built:
            native = "pe" if os.name == "nt" else ("macho" if sys.platform == "darwin" else "elf")
            before = {os.path.join(cur, f) for cur, _, fs in os.walk(work) for f in fs}
            r = sandbox.run(c["needs_build"], work, timeout=300)
            ev["help"][f"BUILD: {c['needs_build']}"] = r["output"][-1500:]
            built = True
            exes = [os.path.relpath(os.path.join(cur, f), work) for cur, _, fs in os.walk(work) for f in fs
                    if os.path.join(cur, f) not in before and _is_binary_exec(os.path.join(cur, f)) == native]
            if exes:
                c["invoke"] = interp.exe_path(exes[0])
                c["build_cmd"] = c["needs_build"]
            else:
                c["runtime_problem"] = (f"build `{c['needs_build']}` produced no {native} executable "
                                        f"(exit {r['code']}): {r['output'][-400:].strip()}")
        if not c.get("invoke") or c.get("runtime_problem"):
            continue
        texts = []
        for flag in ("--help", "-h", ""):
            r = sandbox.run(f"{c['invoke']} {flag}".strip(), work, timeout=15)
            if r["output"].strip():
                texts.append(f"$ {c['invoke']} {flag}\n[exit {r['code']}]\n{r['output'][:3000]}")
                if r["code"] == 0 and len(r["output"]) > 40 and flag:
                    break
        ev["help"][c["invoke"]] = "\n\n".join(texts)


def evidence_text(ev: dict, budget: int = 24000) -> str:
    """Condensed evidence pack for an LLM: never the whole repo."""
    parts = [f"TOOL DIR: {ev['tool_dir']}",
             f"LANGUAGES: {ev['languages']}",
             "FILES:\n" + "\n".join(ev["files"][:150]),
             "ENTRY CANDIDATES:\n" + json.dumps(ev["entry_candidates"], indent=1),
             "SERVICES:\n" + json.dumps(ev["services"]),
             "SAMPLE DATA FILES:\n" + json.dumps(ev["samples"][:30], indent=1),
             "ARGPARSE (static):\n" + json.dumps(ev["argparse"], indent=1)]
    for k, v in ev["help"].items():
        parts.append(f"HELP OUTPUT [{k}]:\n{v[:2500]}")
    for k, v in ev["docs"].items():
        parts.append(f"DOC {k}:\n{v[:3000]}")
    for k, v in ev["build"].items():
        parts.append(f"BUILD FILE {k}:\n{v[:1500]}")
    text = "\n\n".join(parts)
    # entry source files last, trimmed to fit budget
    for c in ev["entry_candidates"][:2]:
        src = ev["sources"].get(c["source"], "")
        room = budget - len(text)
        if room > 1500:
            text += f"\n\nSOURCE {c['source']}:\n{src[:room - 200]}"
    return text[:budget]
