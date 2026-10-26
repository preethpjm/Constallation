"""Onboarding: unknown tool in, verified node out.

  probe (static + live --help)  ->  draft manifest (heuristics)
        -> verify by REALLY running it on sample data
        -> if it fails and an LLM is configured: agent investigates + fixes, re-verify
        -> write manifest.json + node.md into the registry

Nothing is trusted until a real run produced the declared outputs with the
declared types. That is what keeps the canvas from showing broken nodes.
"""
from __future__ import annotations

import json
import os
import re
import shutil

from . import interp, llm, probe, runner, sandbox
from .manifest import Manifest, Port, Runtime, Verification, save
from . import fixtures
from .types import EXT_MIME, WORD_MIME, MIME_EXT, sniff

OUT_WORDS = {"out", "output", "outdir", "dest", "destination", "result", "results", "target", "save"}
DIR_WORDS = {"dir", "directory", "folder", "outdir"}
NUM_WORDS = {"rate", "scale", "margin", "pct", "percent", "count", "n", "num", "factor", "ratio", "size",
             "threshold", "limit", "width", "height", "seed", "port"}
VENV_PY = r".venv\Scripts\python.exe" if os.name == "nt" else ".venv/bin/python"
FIXTURES = {"text/plain": "the quick brown fox jumps over the lazy dog\nsecond line of sample text\n",
            "text/markdown": "# Sample\n\nSome *sample* text.\n",
            "text/csv": "id,name,quantity,price\n1,alpha,2,3.5\n2,beta,1,10\n",
            "image/svg+xml": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="40"/></svg>\n',
            "application/json": '{"id": 1, "name": "alpha", "items": [{"sku": "a", "qty": 2}, {"sku": "b", "qty": 1}]}\n'}
BASE_IMAGES = {"python": "python:3.12-slim", "node": "node:22-slim", "shell": "debian:stable-slim",
               "c": "gcc:14", "cpp": "gcc:14", "go": "golang:1.23", "rust": "rust:1",
               "jar": "eclipse-temurin:21-jre", "elf": "debian:stable-slim", "pe": "scottyhardy/docker-wine"}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "tool"


def tokens(s: str) -> list[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s or "")
    return [t for t in re.split(r"[^a-zA-Z0-9]+", s.lower()) if t]


# ------------------------------------------------------------------ CLI spec

def cli_spec(ev: dict, cand: dict) -> list[dict]:
    """Ordered argument list: {name, flag, positional, optional, help, default}."""
    ap = ev["argparse"].get(cand["source"])
    if ap:
        spec = []
        for a in ap["args"]:
            names = a["names"]
            if any(n in ("-h", "--help") for n in names) or a.get("action") in ("help", "version"):
                continue
            pos = not names[0].startswith("-")
            longf = next((n for n in names if n.startswith("--")), names[0])
            name = a.get("dest") or longf.lstrip("-").replace("-", "_")
            spec.append({"name": name, "flag": None if pos else longf, "positional": pos,
                         "optional": (not pos and not a.get("required")) or a.get("nargs") in ("?", "*"),
                         "help": a.get("help", ""), "default": a.get("default"),
                         "is_flag": a.get("action") in ("store_true", "store_false")})
        return spec
    helptext = ev["help"].get(cand["invoke"], "")
    return parse_usage(helptext)


def parse_usage(text: str) -> list[dict]:
    m = re.search(r"usage:\s*(.+)", text, re.I)
    if not m:
        return []
    line = m.group(1).strip()
    toks = re.findall(r"\[[^\]]*\]|<[^>]+>|\S+", line)
    spec, seen_prog = [], False
    for t in toks:
        optional = t.startswith("[")
        inner = t.strip("[]").strip()
        if not seen_prog:  # skip program / interpreter words
            if re.match(r"^[A-Z<]", inner) or inner.startswith("-"):
                seen_prog = True
            else:
                continue
        if inner.startswith("-"):
            parts = inner.split()
            flag = parts[0].split("|")[0].split(",")[0]
            if flag in ("-h", "--help"):
                continue
            valname = parts[1] if len(parts) > 1 else None
            spec.append({"name": (valname or flag).strip("<>").lower().lstrip("-").replace("-", "_"),
                         "flag": flag, "positional": False, "optional": optional, "help": inner,
                         "default": None, "is_flag": valname is None})
        elif inner and inner not in ("...", "options", "OPTIONS"):
            nm = inner.strip("<>")
            spec.append({"name": re.sub(r"[^a-zA-Z0-9]+", "_", nm).strip("_").lower(), "raw": nm,
                         "flag": None, "positional": True, "optional": optional, "help": nm,
                         "default": None, "is_flag": False})
    return spec


def classify(arg: dict) -> tuple[str, str | None]:
    """-> (role, mime). role in input_file|output_file|output_dir|param|skip"""
    words = set(tokens(arg["name"]) + tokens(arg.get("raw", "")) + tokens(arg.get("help", "")))
    name_words = set(tokens(arg["name"]) + tokens(arg.get("raw", "")))
    if arg.get("is_flag"):
        return "skip", None
    mime = None
    for w in tokens(arg.get("raw", "")) + tokens(arg["name"]) + tokens(arg.get("help", "")):
        if "." + w in EXT_MIME:
            mime = EXT_MIME["." + w]
            break
        if w in WORD_MIME and w not in ("page", "listing", "website", "image", "3d", "mesh", "cad"):
            mime = WORD_MIME[w]
            break
    if mime is None and "file" in words and words & {"text", "txt"}:
        mime = "text/plain"
    is_out = bool(name_words & OUT_WORDS) or arg["name"] in ("o", "out")
    if is_out and (words & DIR_WORDS or (mime is None and "file" not in words)):
        return "output_dir", None
    if is_out:
        return "output_file", mime
    if mime or words & {"file", "path", "input", "in", "infile", "src", "source"}:
        if words & DIR_WORDS and not mime:
            return "input_dir", None
        return "input_file", mime
    return "param", None


def pick_sample(ev, mime, port_name, used):
    def score(s):
        sc = 0
        if mime and s["mime"] == mime: sc += 10
        if s["sample_dir"]: sc += 3
        sc += 4 * len(set(tokens(port_name)) & set(tokens(s["path"])))
        if s["path"] in used: sc -= 8
        return sc
    pool = [s for s in ev["samples"] if not mime or s["mime"] == mime]
    if not pool:
        return None
    return max(pool, key=score)["path"]


def guess_value(arg):
    w = set(tokens(arg["name"]) + tokens(arg.get("help", "")))
    if arg.get("default") not in (None, ""):
        return str(arg["default"])
    if w & NUM_WORDS:
        return "1"
    return "test"


def setup_steps(ev, cand) -> list[str]:
    steps = []
    if cand.get("build_cmd"):
        steps.append(cand["build_cmd"])
    b = ev["build"]
    if "requirements.txt" in b and cand["lang"] == "python":
        steps.append(f"{interp.resolve('python')[0]} -m venv --system-site-packages .venv && "
                     f"{VENV_PY} -m pip install -q -r requirements.txt")
    if "package.json" in b:
        try:
            pj = json.loads(b["package.json"])
            if pj.get("dependencies"):
                steps.append("npm install --omit=dev --no-audit --no-fund")
        except Exception:
            pass
    return steps


def describe(ev, cand):
    ap = ev["argparse"].get(cand.get("source") or "", {})
    if ap and ap.get("description"):
        return ap["description"]
    src = ev["sources"].get(cand.get("source") or "", "")
    m = re.search(r'^\s*(?:"""|\'\'\')(.+?)(?:"""|\'\'\'|\n\n)', src, re.S) or \
        re.search(r"^(?:#!.*\n)?((?:\s*(?://|#|/\*|\*)[^\n]*\n){1,4})", src)
    if m:
        txt = re.sub(r"^\s*(//|#|/\*|\*/?)\s?", "", m.group(1), flags=re.M).strip()
        txt = " ".join(l for l in txt.splitlines() if l and not l.lower().startswith(("usage", "build", "run", "start")))
        if len(txt) > 10:
            return txt[:300]
    for d in ev["docs"].values():
        paras = [p.strip() for p in d.split("\n\n") if p.strip() and not p.strip().startswith("#")]
        if paras:
            return paras[0][:300]
    pj = ev["build"].get("package.json")
    if pj:
        try:
            return json.loads(pj).get("description", "")
        except Exception:
            pass
    return ""


# ------------------------------------------------------------------ drafting

def draft_cli(ev, cand, nid) -> Manifest:
    spec = cli_spec(ev, cand)
    inputs, outputs, parts = [], [], [cand["invoke"]]
    if cand["lang"] == "python" and "requirements.txt" in ev["build"]:
        parts = [cand["invoke"].replace(interp.resolve("python")[0] + " ", VENV_PY + " ", 1)]
    ex_in, ex_params, used = {}, {}, set()
    has_outdir = False
    for a in spec:
        role, mime = classify(a)
        pname = slug(a["name"]).replace("-", "_") or "arg"
        if role == "skip":
            continue
        if role in ("input_file", "input_dir"):
            sample = pick_sample(ev, mime, a["name"], used)
            if sample:
                used.add(sample)
                ex_in[pname] = sample
                mime = mime or EXT_MIME.get(os.path.splitext(sample)[1], "*/*")
            inputs.append(Port(name=pname, kind="file" if role == "input_file" else "dir", mime=mime or "*/*",
                               description=a.get("help", ""), required=not a["optional"]))
            ph = f"{{{pname}}}"
        elif role == "output_dir":
            has_outdir = True
            ph = "{outdir}"
        elif role == "output_file":
            ext = MIME_EXT.get(mime or "", ".out")
            outputs.append(Port(name=pname, mime=mime or "*/*", description=a.get("help", ""),
                                path=f"{{outdir}}/{pname}{ext}"))
            ph = f"{{{pname}}}"
        else:
            inputs.append(Port(name=pname, kind="param", description=a.get("help", ""),
                               required=a["positional"] and not a["optional"],
                               default=None if a.get("default") is None else str(a["default"])))
            if inputs[-1].required or inputs[-1].default is None:
                ex_params[pname] = guess_value(a)
            ph = f"{{{pname}}}"
        seg = ph if a["positional"] else f"{a['flag']} {ph}"
        if a["optional"] and role in ("param", "input_file", "input_dir"):
            seg = f"[[{seg}]]"
        parts.append(seg)
    lang = cand["lang"]
    return Manifest(
        id=nid, name=nid, description=describe(ev, cand), tool_dir=ev["tool_dir"], language=lang,
        inputs=inputs, outputs=outputs,
        runtime=Runtime(kind="cli", command=" ".join(parts), setup=setup_steps(ev, cand),
                        base_image=BASE_IMAGES.get(lang, "debian:stable-slim")),
        tags=sorted({t for t in tokens(nid) + tokens(describe(ev, cand)) if len(t) > 2 and not t.isdigit()}
                    - {"the", "and", "for", "with", "based", "expects", "column"})[:14],
        verification=Verification(example_inputs=ex_in, example_params=ex_params,
                                  log="outdir-style" if has_outdir else ""),
    )


def drafts_http(ev, nid) -> list[Manifest]:
    """Services: start them and read their own API description (OpenAPI)."""
    out = []
    for svc in ev["services"]:
        if svc["framework"] != "fastapi":
            continue
        module = os.path.splitext(svc["path"])[0].replace(os.sep, ".")
        src = ev["sources"][svc["path"]]
        var = (re.search(r"^(\w+)\s*=\s*FastAPI\(", src, re.M) or [None, "app"])[1]
        venv = "requirements.txt" in ev["build"]
        py = VENV_PY if venv else interp.resolve("python")[0]
        start = f"{py} -m uvicorn {module}:{var} --host 127.0.0.1 --port {{port}}"
        tmp = Manifest(id=nid, name=nid, tool_dir=ev["tool_dir"], language="python",
                       runtime=Runtime(kind="http", start=start, route="/openapi.json", method="GET",
                                       setup=setup_steps(ev, {"lang": "python"})),
                       outputs=[Port(name="spec", mime="application/json")])
        work = sandbox.fresh_copy(ev["tool_dir"], "openapi")
        for s in tmp.runtime.setup:
            sandbox.run(s, work, timeout=600)
        d = os.path.join(os.path.dirname(work), "spec")
        r = runner.run_node(tmp, {}, {}, d, cwd=work)
        if not r["ok"]:
            ev["help"][f"openapi:{svc['path']}"] = r["log"]
            continue
        spec = json.load(open(r["outputs"]["spec"]))
        ev["help"][f"openapi:{svc['path']}"] = json.dumps(spec)[:4000]
        schemas = spec.get("components", {}).get("schemas", {})
        for route, ops in spec.get("paths", {}).items():
            for method, op in ops.items():
                if method != "post":
                    continue
                inputs, ex_in, used = [], {}, set()
                body = op.get("requestBody", {}).get("content", {})
                if "multipart/form-data" in body:
                    ref = body["multipart/form-data"]["schema"].get("$ref", "")
                    sch = schemas.get(ref.split("/")[-1], body["multipart/form-data"]["schema"])
                    req = set(sch.get("required", []))
                    for fname, f in sch.get("properties", {}).items():
                        is_file = f.get("format") == "binary" or f.get("contentMediaType") or \
                            (f.get("type") == "string" and f.get("format") in ("binary", "base64"))
                        a = {"name": fname, "help": f.get("description", ""), "is_flag": False}
                        if is_file:
                            _, mime = classify({**a, "name": fname + " file"})
                            sample = pick_sample(ev, mime, fname + " " + a["help"], used)
                            if sample:
                                used.add(sample); ex_in[fname] = sample
                                mime = mime or EXT_MIME.get(os.path.splitext(sample)[1])
                            inputs.append(Port(name=fname, mime=mime or "*/*", description=a["help"],
                                               required=fname in req))
                        else:
                            inputs.append(Port(name=fname, kind="param", description=a["help"],
                                               required=fname in req))
                    bodykind = "multipart"
                elif "application/json" in body:
                    inputs.append(Port(name="body", mime="application/json", description="JSON request body"))
                    bodykind = "json"
                else:
                    bodykind = "multipart"
                rid = slug(f"{nid}-{route}") if len(spec["paths"]) > 1 else nid
                out.append(Manifest(
                    id=rid, name=rid, description=op.get("summary") or spec.get("info", {}).get("description", ""),
                    tool_dir=ev["tool_dir"], language="python", inputs=inputs,
                    outputs=[Port(name="response", mime="application/json", description="endpoint response",
                                  path="{outdir}/response.json")],
                    runtime=Runtime(kind="http", start=start, route=route, method="POST", body=bodykind,
                                    response_output="response", setup=tmp.runtime.setup),
                    tags=sorted(set(tokens(rid) + tokens(op.get("summary", ""))))[:12],
                    verification=Verification(example_inputs=ex_in)))
    return out


# ------------------------------------------------------------------ verification

def verify(m: Manifest, discover: bool = False) -> Manifest:
    """Run the node for real on its example inputs. discover=True also learns
    which files an outdir-style tool produces and adds them as output ports."""
    m.verification.attempts += 1
    root = os.path.join(runner.REGISTRY_DIR, m.id)
    os.makedirs(root, exist_ok=True)
    prep = runner.prepare(m)
    if not prep["ok"]:
        m.verification.status, m.verification.log = "failed", "setup failed:\n" + prep["log"]
        return m
    ad = runner.app_dir(m)
    for p in m.file_inputs():  # no shipped sample? synthesize one for simple text types
        if p.required and p.name not in m.verification.example_inputs and (p.mime in FIXTURES or p.mime in fixtures.BINARY):
            rel = f"_nodeforge_fixtures/{p.name}{MIME_EXT.get(p.mime, '.txt')}"
            os.makedirs(os.path.join(ad, "_nodeforge_fixtures"), exist_ok=True)
            if p.mime in fixtures.BINARY:
                open(os.path.join(ad, rel), "wb").write(fixtures.BINARY[p.mime]())
            else:
                open(os.path.join(ad, rel), "w").write(FIXTURES[p.mime])
            m.verification.example_inputs[p.name] = rel
    missing = [p.name for p in m.file_inputs() if p.required and p.name not in m.verification.example_inputs]
    if missing:
        m.verification.status = "failed"
        m.verification.log = f"no sample data found for inputs {missing}; upload a sample to verify"
        return m
    inputs = {k: os.path.join(ad, v) for k, v in m.verification.example_inputs.items()}
    outdir = os.path.join(root, "verify_out")
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)
    before = sandbox.snapshot(ad)
    res = runner.run_node(m, inputs, m.verification.example_params, outdir)
    if discover:
        norm = lambda x: os.path.normcase(os.path.normpath(x))
        declared = {norm(runner.output_path(o, outdir)) for o in m.outputs}
        taken = {p.name for p in m.inputs + m.outputs}
        for cur, _, files in os.walk(outdir):
            for f in sorted(files):
                p = os.path.join(cur, f)
                if norm(p) in declared:
                    continue
                rel = os.path.relpath(p, outdir).replace(os.sep, "/")
                name = slug(os.path.splitext(rel)[0]).replace("-", "_") or "out"
                in_stems = {slug(os.path.splitext(os.path.basename(v))[0]).replace("-", "_") for v in m.verification.example_inputs.values()}
                if name in in_stems or name in {q.name for q in m.inputs}:   # named after the input, not meaningful
                    name = "out_" + (os.path.splitext(rel)[1].lstrip(".").lower() or "file")
                while name in taken:
                    name += "_2"
                taken.add(name)
                m.outputs.append(Port(name=name, mime=sniff(p), path="{outdir}/" + rel,
                                      description=f"discovered output ({rel})"))
        exit_errs = [e for e in res["errors"] if e.startswith("exit code")]
        res = runner._collect(m, outdir, {"code": 0, "cmd": "", "log": res["log"]})
        res["errors"] = exit_errs + res["errors"]
        res["ok"] = not res["errors"]
        stray = [os.path.relpath(p, ad) for p in sandbox.snapshot(ad) if p not in before]
        if stray:
            res["log"] += f"\nNOTE: tool also wrote into its own folder: {stray[:10]}"
    m.verification.observed_outputs = res.get("observed", {})
    if not m.outputs:
        res["ok"] = False
        res["errors"] = res.get("errors", []) + ["tool ran but produced no files and declares no outputs"]
    m.verification.status = "verified" if res["ok"] else "failed"
    m.verification.log = (res["log"][-3000:] + ("\nERRORS: " + "; ".join(res["errors"]) if res["errors"] else ""))
    # tighten declared mimes to what we actually observed
    pinned = set((_hints(m.tool_dir).get("outputs") or {}))
    for o in m.outputs:
        obs = m.verification.observed_outputs.get(o.name)
        if obs and o.mime in ("*/*", "text/plain") and o.name not in pinned:
            o.mime = obs
    return m


# ------------------------------------------------------------------ LLM repair agent

AGENT_SYSTEM = """You onboard unknown internal tools into a node-based pipeline platform.
You get an evidence pack (file list, detected entry points, live --help output, docs, samples)
and a DRAFT manifest from static heuristics plus the error from really running it.
Investigate with the tools (read files, run commands in a sandbox copy), then call
submit_manifest with a corrected manifest. Each submission is verified by actually running
the tool on example inputs; you get the result back. Keep fixing until ACCEPTED.

Manifest rules:
- runtime.command is a shell command run inside the tool folder. Use {port_name} placeholders for
  every file input, param and output file, and {outdir} for a directory the tool writes into.
  Wrap optional segments in [[ ... ]] (dropped when their placeholder is empty).
- Every output port needs `path`, e.g. "{outdir}/model.glb". If the tool writes fixed filenames,
  point the command at {outdir} and declare those names.
- mime types: model/step, model/gltf-binary, application/json, text/csv, text/html, application/pdf, ...
- example_inputs maps input port -> sample file path RELATIVE to tool folder (use shipped samples;
  if none exist, create one with write_sample). example_params gives values for required params.
- runtime.setup lists one-time install/build steps (pip/npm/make/...).
- runtime.kind "http" for web services: start (use {port}), route, method, body multipart|json,
  response_output = output port name.
Prefer the smallest correct manifest. Never invent outputs you have not observed."""


def _submit_schema():
    sch = Manifest.model_json_schema()
    defs = sch.pop("$defs", {})
    defs["Manifest"] = sch            # $refs must resolve from the root of the tool schema
    return {"type": "object", "properties": {"manifest": {"$ref": "#/$defs/Manifest"}},
            "required": ["manifest"], "$defs": defs}


def llm_repair(m: Manifest, ev: dict, max_submits: int = 4) -> Manifest:
    work = sandbox.fresh_copy(ev["tool_dir"], "agent")
    state = {"m": m, "submits": 0}

    def list_files(subdir: str = "."):
        base = os.path.normpath(os.path.join(work, subdir))
        if not base.startswith(work):
            return "outside tool folder"
        return "\n".join(os.path.relpath(os.path.join(c, f), work) for c, _, fs in os.walk(base)
                         for f in fs if "node_modules" not in c)[:6000]

    def read_file(path: str, max_chars: int = 8000):
        p = os.path.normpath(os.path.join(work, path))
        if not p.startswith(work):
            return "outside tool folder"
        return open(p, encoding="utf-8", errors="replace").read()[:max_chars]

    def run_command(command: str, timeout: int = 60):
        r = sandbox.run(command, work, timeout=min(timeout, 300))
        return f"[exit {r['code']}{' timeout' if r['timeout'] else ''}]\n{r['output'][-5000:]}"

    def write_sample(path: str, content: str):
        p = os.path.normpath(os.path.join(work, path))
        if not p.startswith(work):
            return "outside tool folder"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(content)
        return f"wrote {path}"

    def submit_manifest(manifest: dict):
        state["submits"] += 1
        manifest.setdefault("id", m.id); manifest.setdefault("name", m.id)
        manifest["tool_dir"] = ev["tool_dir"]
        cand = Manifest.model_validate(manifest)
        cand.verification.attempts = m.verification.attempts
        cand.onboarded_by = f"llm:{llm.model_name()}"
        cand = verify_from(cand, work)
        state["m"] = cand
        if cand.verification.status == "verified":
            return "ACCEPTED. Observed outputs: " + json.dumps(cand.verification.observed_outputs)
        if state["submits"] >= max_submits:
            return "ACCEPTED_WITH_FAILURE (out of attempts). Last error:\n" + cand.verification.log
        return "REJECTED. Verification log:\n" + cand.verification.log

    tools = [
        {"name": "list_files", "description": "List files in the tool folder", "schema":
            {"type": "object", "properties": {"subdir": {"type": "string"}}}},
        {"name": "read_file", "description": "Read a file in the tool folder", "schema":
            {"type": "object", "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}},
             "required": ["path"]}},
        {"name": "run_command", "description": "Run a shell command in a sandbox copy of the tool folder",
         "schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
                    "required": ["command"]}},
        {"name": "write_sample", "description": "Create a sample input file in the tool folder (for verification)",
         "schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]}},
        {"name": "submit_manifest", "description": "Submit a manifest; it is verified by a real run",
         "schema": _submit_schema()},
    ]
    user = (f"EVIDENCE:\n{probe.evidence_text(ev)}\n\nDRAFT MANIFEST:\n{m.model_dump_json(indent=1)}\n\n"
            f"DRAFT VERIFICATION RESULT: {m.verification.status}\n{m.verification.log}")
    llm.tool_loop(AGENT_SYSTEM, user, tools,
                  {"list_files": list_files, "read_file": read_file, "run_command": run_command,
                   "write_sample": write_sample, "submit_manifest": submit_manifest},
                  max_turns=30, stop_tool="submit_manifest")
    return state["m"]


def verify_from(m: Manifest, src: str) -> Manifest:
    """Verify using a specific source folder (the agent may have added samples)."""
    orig = m.tool_dir
    m.tool_dir = src
    try:
        discover = not m.outputs
        return verify(m, discover=discover)
    finally:
        m.tool_dir = orig


# ------------------------------------------------------------------ node.md

def node_md(m: Manifest, ev: dict) -> str:
    def rows(ports):
        return "\n".join(f"| `{p.name}` | {p.kind} | {p.mime if p.kind != 'param' else '-'} | "
                         f"{'yes' if p.required else 'no'} | {p.description or ''} |" for p in ports) or "| - | | | | |"
    md = f"""# {m.name}

{m.description or '_No description found in the source; add one._'}

**Status:** {m.verification.status} · **Onboarded by:** {m.onboarded_by} · **Language:** {m.language} · **Runtime:** {m.runtime.kind}

## Inputs
| port | kind | type | required | notes |
|---|---|---|---|---|
{rows(m.inputs)}

## Outputs
| port | kind | type | required | notes |
|---|---|---|---|---|
{rows(m.outputs)}

## How it runs
```
{m.runtime.command if m.runtime.kind == 'cli' else f'{m.runtime.start}  ->  {m.runtime.method} {m.runtime.route}'}
```
Setup: {', '.join(f'`{s}`' for s in m.runtime.setup) or 'none'}

## Verified example
Inputs: {json.dumps(m.verification.example_inputs)} · Params: {json.dumps(m.verification.example_params)}
Observed outputs: {json.dumps(m.verification.observed_outputs)}

## When to use
Tags: {', '.join(m.tags)}
"""
    if llm.available():
        try:
            md += "\n" + llm.complete(
                "Write a short 'When to use / pitfalls' section (markdown, <120 words) for a pipeline node, "
                "for a planner AI that decides which nodes to chain.",
                md + "\n\nEVIDENCE:\n" + probe.evidence_text(ev, 6000), 400)
        except Exception as e:  # noqa
            md += f"\n_(LLM notes unavailable: {e})_\n"
    return md


# ------------------------------------------------------------------ entry point

def onboard(tool_dir: str, nid: str | None = None, use_llm: bool = True, log=print) -> list[Manifest]:
    tool_dir = os.path.abspath(tool_dir)
    nid = nid or slug(os.path.splitext(os.path.basename(tool_dir.rstrip("/\\")))[0])
    try:
        return _onboard(tool_dir, nid, use_llm, log)
    except Exception:  # noqa
        import traceback
        tb = traceback.format_exc()
        log(f"[{nid}] FAILED with an internal error (please report):\n{tb}")
        m = Manifest(id=nid, name=nid, tool_dir=tool_dir,
                     verification=Verification(status="failed", log="internal error:\n" + tb[-3000:]))
        save(m, f"# {nid}\n\nOnboarding crashed:\n```\n{tb[-3000:]}\n```\n")
        return [m]


def _onboard(tool_dir: str, nid: str, use_llm: bool, log) -> list[Manifest]:
    log(f"[{nid}] scanning {tool_dir}")
    ev = probe.scan(tool_dir)
    log(f"[{nid}] languages={ev['languages']} entries={[c['invoke'] or c['source'] for c in ev['entry_candidates']]}"
        f" services={[s['framework'] for s in ev['services']]} samples={len(ev['samples'])}")
    results = []
    if ev["services"]:
        for d in drafts_http(ev, nid):
            results.append(_finish(verify(d), ev, use_llm, log))
        if results:
            return results
    manual = _manifest_from_hints(tool_dir, nid)
    if manual:   # the author described the tool: nothing is started to find out how it works
        log(f"[{nid}] using constellation.json: {manual.runtime.command}")
        return [_finish(verify(manual, discover=True), ev, use_llm, log)]
    probe.probe_help(ev)
    for line in ev.get("probed", []):
        log(f"[{nid}] {line}")
    runnable = [c for c in ev["entry_candidates"] if c.get("invoke") and not c.get("runtime_problem") and not c.get("no_probe")]
    if not runnable:
        problems = sorted({c["runtime_problem"] for c in ev["entry_candidates"] if c.get("runtime_problem")})
        why = ("this machine can't run it: " + " | ".join(problems)) if problems else \
            "no runnable entry point found (GUI-only app? library without CLI?)"
        m = Manifest(id=nid, name=nid, tool_dir=tool_dir, language=next(iter(ev["languages"]), "unknown"),
                     verification=Verification(status="failed", log=why))
        if problems:  # an LLM can't install a compiler either: report, don't spin
            log(f"[{nid}] FAILED  {why}")
            save(m, node_md(m, ev))
            return [m]
        return [_finish(m, ev, use_llm, log)]
    cand = runnable[0]
    m = draft_cli(ev, cand, nid)
    _apply_hints(m)
    log(f"[{nid}] draft command: {m.runtime.command}")
    m = verify(m, discover=True)
    return [_finish(m, ev, use_llm, log)]


def _hints(tool_dir: str) -> dict:
    """Optional constellation.json next to a tool: the author states what the code cannot show.
    {"description": "...", "tags": [...], "inputs": {"port": {"mime": "*/*"}}, "outputs": {"port": {"mime": "text/plain"}}}"""
    p = os.path.join(tool_dir, "constellation.json") if os.path.isdir(tool_dir) else ""
    try:
        return json.load(open(p, encoding="utf-8")) if p and os.path.exists(p) else {}
    except (OSError, ValueError):
        return {}


def _manifest_from_hints(tool_dir: str, nid: str):
    """constellation.json with a "command" describes the whole node, for tools that cannot be probed safely
    (installers, C++ engines behind a Python wrapper, big folders of DLLs):
    {"command": "python server_cli.py {model} --out {outdir}",
     "inputs":  {"model": {"mime": "model/step", "kind": "file"}, "scale": {"kind": "param", "default": "1"}},
     "outputs": {"glb": {"mime": "model/gltf-binary", "path": "{outdir}/out.glb"}},
     "example_inputs": {"model": "examples/a.step"}, "setup": []}"""
    h = _hints(tool_dir)
    if not h.get("command"):
        return None
    ins = [Port(name=k, kind=v.get("kind", "file"), mime=v.get("mime", "*/*"), description=v.get("description", ""),
                required=v.get("required", v.get("kind", "file") != "param"), default=v.get("default"))
           for k, v in (h.get("inputs") or {}).items()]
    outs = [Port(name=k, mime=v.get("mime", "*/*"), description=v.get("description", ""),
                 path=v.get("path") or "{outdir}/" + k) for k, v in (h.get("outputs") or {}).items()]
    cmd = h["command"].replace("{python}", interp.resolve("python")[0] or "python")
    return Manifest(id=nid, name=nid, description=h.get("description", ""), tool_dir=tool_dir, language=h.get("language", "unknown"),
                    inputs=ins, outputs=outs, tags=h.get("tags", []),
                    runtime=Runtime(kind="cli", command=cmd, setup=h.get("setup", []), timeout_s=h.get("timeout_s", 600)),
                    verification=Verification(example_inputs=h.get("example_inputs", {}), example_params=h.get("example_params", {})))


def _apply_hints(m: Manifest):
    h = _hints(m.tool_dir)
    if h.get("description"):
        m.description = h["description"]
    m.tags = sorted(set(m.tags) | set(h.get("tags", [])))
    for group, ports in (("inputs", m.inputs), ("outputs", m.outputs)):
        for p in ports:
            ov = (h.get(group) or {}).get(p.name) or {}
            if ov.get("mime"):
                p.mime = ov["mime"]
            if ov.get("description"):
                p.description = ov["description"]


def _finish(m: Manifest, ev, use_llm, log):
    if m.verification.status != "verified" and use_llm and llm.available():
        log(f"[{m.id}] heuristics failed -> LLM repair agent ({llm.model_name()})")
        m = llm_repair(m, ev)
    log(f"[{m.id}] {m.verification.status.upper()}  inputs={[p.name + ':' + p.mime for p in m.inputs]} "
        f"outputs={[p.name + ':' + p.mime for p in m.outputs]}")
    if m.verification.status != "verified":
        log(f"[{m.id}] log:\n{m.verification.log}")
    save(m, node_md(m, ev))
    return m
