"""Executes ONE node from its manifest, on a chosen backend.

Backends:
  local   - subprocess in the node's prepared app copy (registry/<id>/app)
  docker  - same app baked into an image from a generated Dockerfile (isolation, cloud-ready)
  remote  - POST to another NodeForge worker (an EC2 box, a GPU server, a colleague's machine)
The manifest is the only thing the runner reads, so every tool behaves the same here.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import subprocess
import time

from . import sandbox
from .manifest import REGISTRY_DIR, Manifest
from .types import MIME_EXT, compatible, sniff

def quote(v: str) -> str:
    if os.name == "nt":  # cmd.exe understands double quotes, not shlex's single quotes
        return '"' + v.replace('"', '\\"') + '"' if (" " in v or not v) else v
    return shlex.quote(v)


PH = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
OPT = re.compile(r"\[\[(.*?)\]\]")


def app_dir(m: Manifest) -> str:
    return os.path.join(REGISTRY_DIR, m.id, "app")


def prepare(m: Manifest, src_dir: str | None = None) -> dict:
    """Pin a snapshot of the tool and run its setup steps once."""
    dst = app_dir(m)
    if os.path.exists(dst):
        sandbox.rmtree(dst)
    src = src_dir or m.tool_dir
    if os.path.isdir(src):
        shutil.copytree(src, dst, ignore=sandbox.IGNORE, symlinks=True)
    else:
        os.makedirs(dst)
        shutil.copy2(src, dst)
    log = []
    for step in m.runtime.setup:
        r = sandbox.run(step, dst, timeout=900)
        log.append(f"$ {step}\n{r['output'][-2000:]}")
        if r["code"] != 0:
            return {"ok": False, "log": "\n".join(log)}
    return {"ok": True, "log": "\n".join(log)}


def output_path(port, outdir: str) -> str:
    if port.path:
        return port.path.replace("{outdir}", outdir)
    ext = MIME_EXT.get(port.mime, "")
    return os.path.join(outdir, port.name + ext)


def render(template: str, values: dict) -> str:
    def opt(mo):
        seg = mo.group(1)
        keys = PH.findall(seg)
        return seg if all(values.get(k) not in (None, "") for k in keys) else ""
    t = OPT.sub(opt, template)

    def sub(mo):
        v = values.get(mo.group(1))
        if v is None:
            raise KeyError(f"no value for placeholder {{{mo.group(1)}}}")
        return quote(str(v))
    return re.sub(r"\s+", " ", PH.sub(sub, t)).strip()


def _slash(p):
    return p.replace("\\", "/") if (os.name == "nt" and isinstance(p, str)) else p


def _values(m: Manifest, inputs: dict, params: dict, outdir: str, path_map=_slash):
    vals = {"outdir": path_map(outdir)}
    for p in m.inputs:
        if p.kind == "param":
            v = params.get(p.name, p.default)
            if v in (None, "") and p.required:
                raise ValueError(f"missing required parameter '{p.name}'")
            vals[p.name] = v
        else:
            v = inputs.get(p.name)
            if not v and p.required:
                raise ValueError(f"missing required input '{p.name}'")
            vals[p.name] = path_map(v) if v else None
    for o in m.outputs:
        vals[o.name] = path_map(output_path(o, outdir))
    return vals


def heal(m: Manifest) -> str | None:
    """A registry copied from another machine has absolute paths that do not exist here.
    Rebuild the pinned app folder from a local source if possible; return an error text if not."""
    ad = app_dir(m)
    if os.path.isdir(ad):
        return None
    root = os.path.dirname(REGISTRY_DIR)
    base = os.path.basename(m.tool_dir.replace("\\", "/").rstrip("/"))
    for cand in (m.tool_dir, os.path.join(root, "wrapped", m.id), os.path.join(root, "wrapped", base),
                 os.path.join(root, "sample_tools", base), os.path.join(root, "builtin_nodes", m.id)):
        if cand and os.path.isdir(cand):
            r = prepare(m, cand)
            return None if r["ok"] else "setup failed:\n" + r["log"][-600:]
    return (f"node '{m.id}' is not installed on this machine (its files from {m.tool_dir} are missing). "
            "Add it again under Add tools, e.g. wrap the function or onboard the folder here.")


def _fix_python(cmd: str) -> str:
    """'/usr/bin/python x.py' recorded on Linux: use this machine's interpreter."""
    import sys
    first, _, rest = cmd.partition(" ")
    if re.match(r"^(/|[A-Za-z]:[\\/]).*python[\d.]*(\.exe)?$", first, re.I) and not os.path.exists(first):
        return f'"{sys.executable}" {rest}' if " " in sys.executable else f"{sys.executable} {rest}"
    return cmd


def run_node(m: Manifest, inputs: dict, params: dict, outdir: str, backend: str = "local",
             cwd: str | None = None, remote_url: str | None = None) -> dict:
    os.makedirs(outdir, exist_ok=True)
    if backend != "remote" and not cwd:
        err = heal(m)
        if err:
            return {"ok": False, "outputs": {}, "log": err, "errors": [err]}
    try:
        if backend == "remote":
            return _run_remote(m, inputs, params, outdir, remote_url)
        if m.runtime.kind == "http":
            res = _run_http(m, inputs, params, outdir, cwd or app_dir(m))
        elif backend == "docker":
            res = _run_docker(m, inputs, params, outdir)
        else:
            vals = _values(m, inputs, params, outdir)
            cmd = _fix_python(render(m.runtime.command, vals))
            r = sandbox.run(cmd, cwd or app_dir(m), timeout=m.runtime.timeout_s, env=m.runtime.env)
            res = {"cmd": cmd, "code": r["code"], "log": r["output"], "timeout": r["timeout"]}
    except (ValueError, KeyError) as e:
        return {"ok": False, "outputs": {}, "log": str(e), "errors": [str(e)]}
    return _collect(m, outdir, res)


def _collect(m: Manifest, outdir: str, res: dict) -> dict:
    outputs, errors, observed = {}, [], {}
    if res.get("code", 0) != 0:
        errors.append(f"exit code {res.get('code')}" + (" (timeout)" if res.get("timeout") else ""))
    claimed = set()
    for o in m.outputs:
        p = output_path(o, outdir)
        if not os.path.exists(p) and (o.description or "").startswith("discovered output"):
            # the tool names its file after its input (drawing.cgm -> drawing.svg): take the new file of that type
            ext = os.path.splitext(p)[1].lower()
            found = [os.path.join(c, f) for c, _, fs in os.walk(outdir) for f in fs if f.lower().endswith(ext)]
            found = [f for f in found if os.path.normpath(f) not in claimed and
                     os.path.normpath(f) not in {os.path.normpath(output_path(x, outdir)) for x in m.outputs}]
            if found:
                p = max(found, key=os.path.getmtime)
        claimed.add(os.path.normpath(p))
        if not os.path.exists(p) or (os.path.isfile(p) and os.path.getsize(p) == 0):
            if o.required:
                errors.append(f"output '{o.name}' not produced at {p}")
            continue
        got = sniff(p)
        observed[o.name] = got
        if not compatible(got, o.mime):
            errors.append(f"output '{o.name}' is {got}, manifest says {o.mime}")
        outputs[o.name] = p
    return {"ok": not errors, "outputs": outputs, "observed": observed, "errors": errors,
            "log": (f"$ {res['cmd']}\n" if res.get("cmd") else "") + res.get("log", "")}


# ---------------- http services ----------------

def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _wait_port(port, timeout=30):
    t = time.time()
    while time.time() - t < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def _run_http(m: Manifest, inputs, params, outdir, cwd):
    import httpx
    port = _free_port()
    start = render(m.runtime.start, {"port": port})
    proc = subprocess.Popen(start, shell=True, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **sandbox.popen_kwargs())
    try:
        if not _wait_port(port):
            return {"code": -1, "log": "service did not open its port\n$ " + start}
        url = f"http://127.0.0.1:{port}{m.runtime.route}"
        files, data = {}, {}
        for p in m.inputs:
            if p.kind == "param":
                v = params.get(p.name, p.default)
                if v not in (None, ""):
                    data[p.name] = v
            elif inputs.get(p.name):
                files[p.name] = (os.path.basename(inputs[p.name]), open(inputs[p.name], "rb"))
            elif p.required:
                return {"code": -1, "log": f"missing required input '{p.name}'"}
        if m.runtime.body == "json":
            import json as _j
            body = dict(data)
            for k, (_, fh) in files.items():
                body[k] = _j.load(fh)
            r = httpx.request(m.runtime.method, url, json=body, timeout=m.runtime.timeout_s)
        else:
            r = httpx.request(m.runtime.method, url, files=files or None, data=data or None,
                              timeout=m.runtime.timeout_s)
        target = next((o for o in m.outputs if o.name == m.runtime.response_output), m.outputs[0])
        with open(output_path(target, outdir), "wb") as f:
            f.write(r.content)
        return {"cmd": f"{m.runtime.method} {m.runtime.route} (service: {start})",
                "code": 0 if r.status_code < 400 else r.status_code, "log": r.text[:2000]}
    finally:
        for _, fh in (files or {}).values() if "files" in locals() else []:
            fh.close()
        sandbox.kill_tree(proc)


# ---------------- docker ----------------

def dockerfile(m: Manifest) -> str:
    lines = [f"FROM {m.runtime.base_image}", "WORKDIR /app", "COPY . /app"]
    lines += [f"RUN {s}" for s in m.runtime.setup]
    return "\n".join(lines) + "\n"


def _run_docker(m: Manifest, inputs, params, outdir):
    ad = app_dir(m)
    with open(os.path.join(ad, "Dockerfile.nodeforge"), "w") as f:
        f.write(dockerfile(m))
    tag = f"nodeforge/{m.id}:latest"
    b = sandbox.run(f"docker build -q -f Dockerfile.nodeforge -t {tag} .", ad, timeout=1800)
    if b["code"] != 0:
        return {"code": b["code"], "log": "docker build failed:\n" + b["output"]}
    run_root = os.path.dirname(os.path.abspath(outdir))
    ind = os.path.join(run_root, "_in_" + m.id)
    os.makedirs(ind, exist_ok=True)
    local = {}
    for k, v in inputs.items():
        if v:
            dst = os.path.join(ind, os.path.basename(v)); shutil.copy2(v, dst); local[k] = dst
    mp = lambda p: p.replace(ind, "/io/in").replace(os.path.abspath(outdir), "/io/out") if p else p
    vals = _values(m, local, params, os.path.abspath(outdir), mp)
    cmd = render(m.runtime.command, vals)
    full = (f"docker run --rm --network none -v {shlex.quote(ind)}:/io/in:ro "
            f"-v {shlex.quote(os.path.abspath(outdir))}:/io/out {tag} sh -c {shlex.quote(cmd)}")
    r = sandbox.run(full, ad, timeout=m.runtime.timeout_s)
    return {"cmd": full, "code": r["code"], "log": r["output"], "timeout": r["timeout"]}


# ---------------- remote worker ----------------

def _run_remote(m: Manifest, inputs, params, outdir, url):
    import json as _j
    import httpx
    if not url:
        return {"ok": False, "outputs": {}, "errors": ["remote backend needs a worker URL"], "log": ""}
    files = {k: (os.path.basename(v), open(v, "rb")) for k, v in inputs.items() if v}
    r = httpx.post(f"{url.rstrip('/')}/worker/run/{m.id}", files=files,
                   data={"params": _j.dumps(params)}, timeout=m.runtime.timeout_s + 30)
    if r.status_code != 200:
        return {"ok": False, "outputs": {}, "errors": [f"worker HTTP {r.status_code}"], "log": r.text[:2000]}
    res = r.json()
    outputs = {}
    for port, rel in res.get("outputs", {}).items():
        fr = httpx.get(f"{url.rstrip('/')}{rel}", timeout=120)
        p = output_path(next(o for o in m.outputs if o.name == port), outdir)
        with open(p, "wb") as f:
            f.write(fr.content)
        outputs[port] = p
    return {"ok": res.get("ok", False), "outputs": outputs, "errors": res.get("errors", []),
            "log": f"[remote {url}]\n" + res.get("log", "")}
