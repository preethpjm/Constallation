"""Library functions that are 'logically just a converter' become nodes too.

A Python function like  csv_to_json(text: str) -> dict  has no CLI, so the normal onboarding
finds nothing to run. This module reads the function's signature (without importing it),
generates a small argparse shim next to a copy of the source, and hands that shim to the
ordinary onboarding pipeline. From then on it is verified, planned and run like any other
tool - the wrapper is just a tool whose author happened to be us.

Rules, deliberately boring:
  - text-like parameters (text, content, data, markdown, csv_text ...)  -> input file, read as text
  - path-like parameters (path, file, src ...)                          -> input file, passed as a path
  - output-like parameters (out, output_path, dest ...)                 -> output file
  - everything else (int/float/str)                                     -> parameter
  - the return value is written to the output file (dict/list -> JSON, str -> text, bytes -> binary)
Functions it cannot map honestly (callbacks, *args, objects) are reported, not guessed.
"""
from __future__ import annotations

import ast
import os
import re
import shutil

from . import sandbox

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP_DIR = os.path.join(ROOT, "wrapped")

TEXT_NAMES = {"text", "content", "contents", "data", "body", "source", "string", "s", "txt", "doc", "document",
              "markdown", "md", "html", "csv_text", "json_text", "raw", "payload", "message", "input_text"}
PATH_NAMES = {"path", "file", "filename", "filepath", "file_path", "input", "input_path", "infile", "in_file",
              "src", "src_path", "fname", "input_file"}
OUT_NAMES = {"out", "output", "output_path", "out_path", "dest", "dst", "destination", "outfile", "out_file",
             "output_file", "target"}
KIND_HINT = {"csv": "csv", "json": "json", "markdown": "md", "md": "md", "html": "html", "xml": "xml"}
EXT = {"csv": ".csv", "json": ".json", "txt": ".txt", "md": ".md", "html": ".html", "xml": ".xml", "bin": ".bin"}
NOISY_IMPORTS = {"requests", "urllib", "socket", "subprocess", "smtplib", "ftplib", "http", "paramiko", "boto3"}


def _ann(node) -> str:
    return ast.unparse(node) if node is not None else ""


def _const(node):
    try:
        return ast.literal_eval(node)
    except Exception:  # noqa
        return None


def discover(path: str) -> list[dict]:
    """Public, top-level functions of a .py file that can be mapped to a node (without importing it)."""
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return []
    imports = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imports.add(n.module.split(".")[0])
    out = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name.startswith("_"):
            continue
        a = fn.args
        problems = []
        if a.vararg or a.kwarg:
            problems.append("takes *args/**kwargs")
        params = []
        pos = a.posonlyargs + a.args
        defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
        for arg, d in list(zip(pos, defaults)) + list(zip(a.kwonlyargs, a.kw_defaults)):
            if arg.arg in ("self", "cls"):
                problems.append("is a method")
            ann = _ann(arg.annotation)
            has_default = d is not None
            params.append({"name": arg.arg, "ann": ann, "has_default": has_default,
                           "default": _const(d) if has_default else None})
        ret = _ann(fn.returns)
        doc = ast.get_docstring(fn) or ""
        # a converter-ish function is pure: no network, no subprocess, has something to return or an out param
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | \
               {a.name.split(".")[0] for n in ast.walk(fn) if isinstance(n, ast.Import) for a in n.names} | \
               {n.module.split(".")[0] for n in ast.walk(fn) if isinstance(n, ast.ImportFrom) and n.module}
        io = sorted((imports | used) & NOISY_IMPORTS & used)
        body_src = ast.unparse(fn)
        has_return = any(isinstance(n, ast.Return) and n.value is not None for n in ast.walk(fn))
        has_out = any(p["name"] in OUT_NAMES for p in params)
        if not (has_return or has_out):
            problems.append("returns nothing and writes no output parameter")
        for p in params:
            base = p["ann"].replace("Optional[", "").replace("Path", "str").strip("[] ")
            if p["ann"] and base.split("|")[0].strip() not in ("str", "int", "float", "bool", "bytes"):
                problems.append(f"parameter '{p['name']}' has type {p['ann']} (only str/int/float/bool/Path map to a port)")
        score = 0
        score += 3 if doc else 0
        score += 2 if ret else 0
        score += 2 if all(p["ann"] for p in params) and params else 0
        score += 2 if any(p["name"] in TEXT_NAMES | PATH_NAMES for p in params) else 0
        score -= 4 * len(io)
        score -= 5 if re.search(r"\b(open|os\.remove|shutil\.rmtree|os\.system)\(", body_src) and not has_out else 0
        out.append({"function": fn.name, "file": path, "doc": doc, "params": params, "returns": ret,
                    "problems": sorted(set(problems)), "score": score, "side_effects": io})
    return sorted(out, key=lambda f: (bool(f["problems"]), -f["score"]))


def _kind_of(name: str, ann: str = "") -> str:
    for w in re.split(r"[^a-z0-9]+", name.lower()):
        if w in KIND_HINT:
            return KIND_HINT[w]
    return "txt"


def _ret_kind(fn: dict) -> str:
    r = fn["returns"].replace(" ", "").lower()
    if "_to_" in fn["function"]:  # csv_to_json -> the target of the name wins
        tgt = fn["function"].split("_to_")[-1].split("_")[0]
        if tgt in KIND_HINT:
            return KIND_HINT[tgt]
    name_kind = _kind_of(fn["function"])
    if r.startswith(("dict", "list", "tuple", "set")) or r in ("any", ""):
        return "json"
    if r.startswith("bytes"):
        return "bin"
    if r in ("str", "int", "float", "bool"):
        return name_kind if name_kind != "txt" else "txt"
    return "json"


def shim_source(fn: dict, module: str) -> tuple[str, dict]:
    """-> (python source for the shim, port plan). Uses explicit add_argument lines so the probe can read it."""
    lines, plan = [], {"inputs": [], "params": [], "output": None}
    call = []
    taken = set()
    out_param = None
    for p in fn["params"]:
        n, ann = p["name"], p["ann"].replace(" ", "")
        is_str = ann in ("", "str", "Path", "Optional[str]", "str|None", "Optional[Path]")
        if n in OUT_NAMES and is_str:
            out_param = n
            continue
    ret_kind = _ret_kind(fn)
    for p in fn["params"]:
        n, ann = p["name"], p["ann"].replace(" ", "")
        is_str = ann in ("", "str", "Path", "Optional[str]", "str|None", "Optional[Path]")
        if n == out_param:
            lines.append(f'    ap.add_argument("--out_{ret_kind}", required=True, help="output {ret_kind} file")')
            call.append(f'{n}=a.out_{ret_kind}')
            plan["output"] = f"out_{ret_kind}"
            continue
        if is_str and n in TEXT_NAMES:
            k = _kind_of(n) if _kind_of(n) != "txt" else _kind_of(fn["function"].split("_to_")[0])
            flag = f"in_{k}" if f"in_{k}" not in taken else f"in_{k}_{n}"
            taken.add(flag)
            lines.append(f'    ap.add_argument("--{flag}", required={not p["has_default"]}, help="input {k} file (read as text)")')
            call.append(f'{n}=read_text(a.{flag})')
            plan["inputs"].append(flag)
        elif is_str and n in PATH_NAMES:
            k = _kind_of(n) if _kind_of(n) != "txt" else "txt"
            flag = f"in_{k}" if f"in_{k}" not in taken else f"in_{k}_{n}"
            taken.add(flag)
            lines.append(f'    ap.add_argument("--{flag}", required={not p["has_default"]}, help="input {k} file")')
            call.append(f'{n}=a.{flag}')
            plan["inputs"].append(flag)
        else:
            base = ann.replace("Optional[", "").replace("]", "").split("|")[0]
            typ = {"int": "int", "float": "float", "bool": "str2bool"}.get(base, "str")
            if p["has_default"]:
                dflt = p["default"]
            else:  # a value is needed to verify the node; the help says so
                dflt = {"int": 1, "float": 1.0, "str2bool": False}.get(typ, "test")
            note = "" if p["has_default"] else " (the function has no default; this placeholder is used unless set)"
            lines.append(f'    ap.add_argument("--{n}", type={typ}, default={dflt!r}, help="{n}{note}")')
            call.append(f"{n}=a.{n}")
            plan["params"].append(n)
    if not out_param:
        lines.append(f'    ap.add_argument("--out_{ret_kind}", required=True, help="output {ret_kind} file")')
        plan["output"] = f"out_{ret_kind}"
    doc1 = (fn["doc"].strip().splitlines() or [f"{fn['function']} from {module}"])[0]
    src = f'''"""Generated by Constellation: wraps {module}.{fn["function"]}. Do not edit; regenerate instead."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import {module} as _lib  # noqa: E402


def read_text(p):
    return open(p, encoding="utf-8", errors="replace").read() if p else ""


def str2bool(v):
    return str(v).lower() in ("1", "true", "yes", "y")


def main():
    ap = argparse.ArgumentParser(description={doc1!r})
{chr(10).join(lines)}
    a = ap.parse_args()
    result = _lib.{fn["function"]}({", ".join(call)})
    out = a.{plan["output"]}
    {"pass  # the function wrote the output file itself" if out_param else "write(out, result)"}
    if not os.path.exists(out):
        sys.exit("the wrapped function produced no output file")


def write(path, result):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if isinstance(result, (bytes, bytearray)):
        open(path, "wb").write(result)
    elif isinstance(result, str) and not path.endswith(".json"):
        open(path, "w", encoding="utf-8").write(result)
    else:
        open(path, "w", encoding="utf-8").write(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
'''
    return src, plan


def wrap(source_file: str, function: str, nid: str | None = None, use_llm: bool = False, log=print):
    """Generate the shim, onboard it, mark the manifest as a wrapper. -> Manifest"""
    from .manifest import load_all, save, load_md
    from .onboard import onboard, slug
    source_file = os.path.abspath(source_file)
    fns = {f["function"]: f for f in discover(source_file)}
    if function not in fns:
        raise ValueError(f"no public function '{function}' in {source_file}; found: {', '.join(fns) or 'none'}")
    fn = fns[function]
    if fn["problems"]:
        raise ValueError(f"cannot wrap {function}: " + "; ".join(fn["problems"]))
    module = os.path.splitext(os.path.basename(source_file))[0]
    nid = nid or slug(f"{module}-{function}")
    d = os.path.join(WRAP_DIR, nid)
    if os.path.exists(d):
        sandbox.rmtree(d)
    os.makedirs(d)
    # copy sibling sources so relative imports inside the library keep working
    srcdir = os.path.dirname(source_file)
    for name in os.listdir(srcdir):
        p = os.path.join(srcdir, name)
        if os.path.isfile(p) and name.endswith(".py") and not name.startswith("test_"):
            shutil.copy2(p, os.path.join(d, name))
        elif os.path.isdir(p) and os.path.exists(os.path.join(p, "__init__.py")):
            shutil.copytree(p, os.path.join(d, name), ignore=sandbox.IGNORE, dirs_exist_ok=True)
    src, plan = shim_source(fn, module)
    with open(os.path.join(d, "constellation_shim.py"), "w", encoding="utf-8") as f:
        f.write(src)
    log(f"[{nid}] wrapping {module}.{function}  inputs={plan['inputs']} params={plan['params']} output={plan['output']}")
    ms = onboard(d, nid, use_llm=use_llm, log=log)
    m = ms[0]
    m.origin, m.wrapper_of = "wrapper", f"{os.path.basename(source_file)}:{function}"
    if fn["doc"]:
        m.description = fn["doc"].strip().splitlines()[0]
    if "converter" not in m.tags:
        m.tags = sorted(set(m.tags + ["converter", "wrapper"]))
    md = load_md(nid) + (f"\n## Origin\nGenerated wrapper around `{module}.{function}` "
                         f"({'verified by a real run' if m.verification.status == 'verified' else 'NOT verified'}). "
                         f"The source file is unchanged; the shim lives in `wrapped/{nid}/`.\n")
    save(m, md)
    return m
