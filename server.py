"""NodeForge server: canvas UI + API + remote worker endpoint.

    python server.py                 # http://localhost:8800
    python server.py --port 8801     # a second machine/worker (use as 'remote' backend)
"""
import argparse
import io
import json
import os
import shutil
import tempfile
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

import threading
from nodeforge.manifest import REGISTRY_DIR

from nodeforge import executor, graph, llm, runner, settings
from nodeforge.manifest import Manifest, load_all, load_md, save
from nodeforge.onboard import onboard
from nodeforge.sandbox import WORK_ROOT

ROOT = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="NodeForge")


@app.on_event("startup")
def _builtin_nodes():
    from nodeforge import builtin
    threading.Thread(target=builtin.ensure, kwargs={"log": lambda m: None}, daemon=True).start()
ONBOARD_LOCK = threading.Lock()   # two onboardings at once would share registry folders


@app.get("/", response_class=HTMLResponse)
def index():
    return open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8").read()


@app.get("/vendor/{name}")
def vendor(name: str):
    """Front-end libraries are served locally: no CDN needed on a restricted network."""
    p = os.path.join(ROOT, "ui", "vendor", os.path.basename(name))
    if not os.path.isfile(p):
        raise HTTPException(404)
    import mimetypes
    mt = "font/woff2" if p.endswith(".woff2") else (mimetypes.guess_type(p)[0] or "application/octet-stream")
    return FileResponse(p, media_type=mt, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/models")
def api_models(base_url: str = "", api_key: str = ""):
    """List the models a local/remote server offers: Ollama (/api/tags) first, then OpenAI-style (/models)."""
    import urllib.request
    base = (base_url or "http://localhost:11434").rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    key = api_key or settings.load()["llm"].get("api_key") or ""
    if "*" in key:  # the settings panel shows a masked key; never send that
        key = ""

    def get(url, auth=False):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if auth and key else {})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    errs = []
    try:
        j = get(root + "/api/tags")
        return {"server": "ollama", "base_url": root + "/v1",
                "models": [{"name": m["name"], "size": m.get("size", 0)} for m in j.get("models", [])]}
    except Exception as e:  # noqa
        errs.append(f"{root}/api/tags: {e}")
    try:
        j = get((base if base.endswith("/v1") else base + "/v1") + "/models", auth=True)
        return {"server": "openai-compatible", "base_url": base if base.endswith("/v1") else base + "/v1",
                "models": [{"name": m["id"], "size": 0} for m in j.get("data", [])]}
    except Exception as e:  # noqa
        errs.append(f"/models: {e}")
    raise HTTPException(502, "could not list models. Is the server running? " + " | ".join(errs))


VERSION = "0.4.0"


@app.get("/api/health")
def health():
    """For Docker/Kubernetes health checks and the launcher's 'is it up yet' loop."""
    return {"ok": True, "version": VERSION, "nodes": len(load_all())}


@app.get("/api/status")
def status():
    from nodeforge import decider, interp
    return {"llm": llm.provider(), "model": llm.model_name(), "runtimes": interp.report(),
            "decider": decider.status()}


@app.get("/api/settings")
def get_settings():
    return settings.masked()


@app.post("/api/settings")
async def post_settings(req: Request):
    body = await req.json()
    allowed = {k: body[k] for k in ("llm", "decider", "laya") if k in body}
    settings.save(allowed)
    return settings.masked()


@app.post("/api/settings/test")
def test_settings():
    from nodeforge import decider
    out = {"llm": llm.test_connection(), "decider": decider.status()}
    try:
        r = decider.choose("The BOM has a quantity mismatch on WS-M6.", "Is anything wrong?",
                           {"problem": "something is wrong or mismatched", "fine": "all good"})
        out["decision_test"] = r
    except Exception as e:  # noqa
        out["decision_test"] = {"error": str(e)}
    return out


@app.get("/api/nodes")
def nodes():
    out = []
    for m in load_all().values():
        sk = os.path.join(REGISTRY_DIR, m.id, "SKILL.md")
        skill = open(sk, encoding="utf-8", errors="replace").read()[:6000] if os.path.exists(sk) else ""
        out.append({**m.model_dump(), "md": load_md(m.id), "skill": skill})
    return out


@app.post("/api/onboard")
async def api_onboard(path: str = Form(""), name: str = Form(""), archive: UploadFile | None = File(None)):
    logs = []
    if archive is not None and archive.filename:
        d = os.path.join(ROOT, "uploaded_tools", os.path.splitext(archive.filename)[0])
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        data = await archive.read()
        if archive.filename.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for member in z.namelist():  # zip-slip guard
                    if os.path.isabs(member) or ".." in member.split("/"):
                        raise HTTPException(400, "unsafe path in zip")
                z.extractall(d)
            entries = os.listdir(d)
            if len(entries) == 1 and os.path.isdir(os.path.join(d, entries[0])):
                d = os.path.join(d, entries[0])
        else:  # a single script or executable
            p = os.path.join(d, archive.filename)
            open(p, "wb").write(data)
            os.chmod(p, 0o755)
        path = d
    if not path or not os.path.exists(path):
        raise HTTPException(400, f"path not found: {path}")
    if not ONBOARD_LOCK.acquire(blocking=False):
        raise HTTPException(409, "another tool is being onboarded right now; try again when it finishes")
    try:
        import anyio
        ms = await anyio.to_thread.run_sync(lambda: onboard(path, name or None, log=logs.append))
    finally:
        ONBOARD_LOCK.release()
    return {"log": "\n".join(logs), "nodes": [m.id for m in ms],
            "statuses": {m.id: m.verification.status for m in ms}}


async def _exclusive(fn):
    """Run a long job off the event loop, one at a time (jobs share registry folders)."""
    if not ONBOARD_LOCK.acquire(blocking=False):
        raise HTTPException(409, "another job (onboard / crawl / wrap) is running; try again when it finishes")
    try:
        import anyio
        return await anyio.to_thread.run_sync(fn)
    finally:
        ONBOARD_LOCK.release()


@app.post("/api/crawl")
async def api_crawl(source: str = Form(...), run: str = Form("0")):
    """Read a folder or git repo and list what is in it. With run=1 also onboard, wrap and write skills."""
    from nodeforge import crawl
    logs = []

    def job():
        try:
            return crawl.crawl(source.strip(), onboard_units=run == "1", log=logs.append)
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e))
    rep = await _exclusive(job)
    return {"log": "\n".join(logs), "report": rep}


@app.get("/api/sources")
def api_sources():
    return {"sources": settings.load().get("sources", [])}


@app.post("/api/sources")
async def api_sources_set(req: Request):
    body = await req.json()
    src = [s.strip() for s in body.get("sources", []) if str(s).strip()]
    settings.save({"sources": src})
    return {"sources": src}


def _sync_sources(logs):
    from nodeforge import crawl
    done = []
    for src in settings.load().get("sources", []):
        try:
            rep = crawl.crawl(src, onboard_units=True, log=logs.append, skip_existing=True)
            done.append({"source": src, "new": [r["id"] for r in rep["results"]]})
        except Exception as e:  # noqa
            logs.append(f"{src}: {e}")
            done.append({"source": src, "error": str(e)})
    return done


@app.post("/api/sources/sync")
async def api_sources_sync():
    logs = []
    done = await _exclusive(lambda: _sync_sources(logs))
    return {"log": "\n".join(logs), "synced": done}


@app.get("/api/wrap/discover")
def api_wrap_discover(path: str):
    from nodeforge import wrap
    if not os.path.isfile(path):
        raise HTTPException(400, f"file not found: {path}")
    return [{k: f[k] for k in ("function", "doc", "params", "returns", "problems", "score", "side_effects")}
            for f in wrap.discover(path)]


@app.post("/api/wrap")
async def api_wrap(path: str = Form(...), function: str = Form(...), name: str = Form("")):
    from nodeforge import skills, wrap
    logs = []

    def job():
        try:
            m = wrap.wrap(path, function, name or None, use_llm=False, log=logs.append)
        except ValueError as e:
            raise HTTPException(400, str(e))
        skills.generate_all(polish=False, log=logs.append)
        return m
    m = await _exclusive(job)
    return {"log": "\n".join(logs), "node": m.id, "status": m.verification.status}


@app.get("/api/skills")
def api_skills():
    from nodeforge import skills
    reg = load_all()
    out = []
    for m in reg.values():
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "registry", m.id, "SKILL.md")
        out.append({"id": m.id, "status": m.verification.status, "has_skill": os.path.exists(p)})
    return out


@app.post("/api/skills/generate")
async def api_skills_generate():
    from nodeforge import skills
    logs = []
    r = await _exclusive(lambda: skills.generate_all(polish=llm.available(), log=logs.append))
    return {"log": "\n".join(logs), **r}


@app.get("/api/nodes/{nid}/skill")
def api_node_skill(nid: str):
    from nodeforge import skills
    reg = load_all()
    if nid not in reg:
        raise HTTPException(404)
    p = os.path.join(ROOT, "registry", nid, "SKILL.md")
    text = open(p, encoding="utf-8").read() if os.path.exists(p) else skills.skill_md(reg[nid], reg, polish=False)
    return {"id": nid, "generated": os.path.exists(p), "skill": text}


@app.get("/api/skills/check")
async def api_skills_check():
    from nodeforge import skills
    import anyio
    return await anyio.to_thread.run_sync(skills.activation_check)


@app.get("/api/skills.zip")
def api_skills_zip():
    from fastapi.responses import Response
    from nodeforge import skills
    return Response(skills.bundle(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=constellation-skills.zip"})


@app.post("/api/nodes/{nid}/approve")
def approve(nid: str):
    reg = load_all()
    if nid not in reg:
        raise HTTPException(404)
    m = reg[nid]
    m.human_approved = True
    save(m, load_md(nid))
    return {"ok": True}


@app.post("/api/plan")
async def api_plan(req: Request):
    body = await req.json()
    import anyio
    g = await anyio.to_thread.run_sync(lambda: graph.plan(body.get("request", ""), body.get("uploads")))
    synced = None
    if g.get("planner") == "ai-only fallback" and settings.load().get("sources"):
        logs = []   # nothing registered fits: look in the user's tool sources, onboard what is there, plan again
        synced = await _exclusive(lambda: _sync_sources(logs))
        g2 = await anyio.to_thread.run_sync(lambda: graph.plan(body.get("request", ""), body.get("uploads")))
        if g2.get("planner") != "ai-only fallback":
            g = g2
            g["note"] = "Found the tool in your tool sources and added it. " + (g.get("note") or "")
    return {"graph": g, "errors": graph.validate(g), "synced": synced}


@app.post("/api/validate")
async def api_validate(req: Request):
    return {"errors": graph.validate(await req.json())}


@app.post("/api/run")
async def api_run(req: Request):
    form = await req.form()
    g = json.loads(form["graph"])
    rid, d = executor.new_run_dir()
    ups = {}
    os.makedirs(os.path.join(d, "_uploads"))
    for k, v in form.multi_items():
        if k.startswith("file:") and hasattr(v, "filename"):
            p = os.path.join(d, "_uploads", os.path.basename(v.filename))
            open(p, "wb").write(await v.read())
            ups[k[5:]] = p
    import anyio
    res = await anyio.to_thread.run_sync(lambda: executor.execute(g, ups, d))
    for r in res.get("nodes", {}).values():
        r["urls"] = {k: "/files/" + os.path.relpath(v, WORK_ROOT) for k, v in r.get("outputs", {}).items() if v}
    return {"run": rid, **res}


@app.get("/files/{path:path}")
def files(path: str):
    p = os.path.realpath(os.path.join(WORK_ROOT, path))
    if not p.startswith(os.path.realpath(WORK_ROOT)) or not os.path.isfile(p):
        raise HTTPException(404)
    return FileResponse(p, filename=os.path.basename(p) if not p.endswith((".html", ".json", ".csv")) else None)


# ---- remote worker: any machine running this server can execute nodes for another one
@app.post("/worker/run/{nid}")
async def worker_run(nid: str, request: Request):
    reg = load_all()
    if nid not in reg:
        raise HTTPException(404, f"node {nid} not installed on this worker")
    form = await request.form()
    params = json.loads(form.get("params", "{}"))
    rid, d = executor.new_run_dir()
    ins = {}
    for k, v in form.multi_items():
        if hasattr(v, "filename"):
            p = os.path.join(d, "in_" + os.path.basename(v.filename))
            open(p, "wb").write(await v.read())
            ins[k] = p
    r = runner.run_node(reg[nid], ins, params, os.path.join(d, "out"))
    return {"ok": r["ok"], "errors": r["errors"], "log": r["log"],
            "outputs": {k: "/files/" + os.path.relpath(v, WORK_ROOT) for k, v in r["outputs"].items()}}


if __name__ == "__main__":
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    uvicorn.run(app, host=a.host, port=a.port)
