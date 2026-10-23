"""Runs a validated pipeline graph, node by node, routing each to its backend."""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid

from . import llm, runner
from . import decider
from .graph import decide_options, topo, validate
from .manifest import load_all
from .sandbox import WORK_ROOT


def new_run_dir() -> tuple[str, str]:
    rid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = os.path.join(WORK_ROOT, rid)
    os.makedirs(d)
    return rid, d


def execute(g: dict, uploads: dict[str, str], run_dir: str, on_event=lambda e: None) -> dict:
    """uploads: input-node-id -> file path. Returns per-node results."""
    reg = load_all()
    errs = validate(g, reg)
    if errs:
        return {"ok": False, "errors": errs, "nodes": {}}
    nodes = {n["id"]: n for n in g["nodes"]}
    produced: dict[tuple[str, str], str] = {}
    results = {}
    untaken: set[tuple[str, str]] = set()   # decision branches not chosen
    skipped: set[str] = set()
    for nid in topo(g):
        n = nodes[nid]
        inc = [e for e in g["edges"] if e["dst"] == nid]
        ins = {e["dst_port"]: produced.get((e["src"], e["src_port"])) for e in inc}
        ai_ins = [produced.get((e["src"], e["src_port"])) for e in inc]
        on_event({"node": nid, "status": "running"})
        t0 = time.time()
        if inc and any(e["src"] in skipped or (e["src"], e["src_port"]) in untaken for e in inc):
            skipped.add(nid)
            res = {"ok": True, "skipped": True, "outputs": {}, "errors": [], "log": "branch not taken"}
        elif n["kind"] == "input":
            p = uploads.get(nid)
            p = os.path.abspath(p) if p else p
            if not p:
                res = {"ok": False, "errors": [f"no file uploaded for '{n.get('label', nid)}'"]}
            else:
                produced[(nid, "file")] = p
                res = {"ok": True, "outputs": {"file": p}}
        elif n["kind"] == "output":
            src = ins.get("file")
            res = {"ok": bool(src), "outputs": {"file": src} if src else {},
                   "errors": [] if src else ["upstream produced nothing"]}
        elif any(v is None for v in ins.values()):
            res = {"ok": False, "errors": ["skipped: an upstream node failed"]}
        elif n["kind"] == "decide":
            res = _run_decide(n, ins.get("in"))
            if res["ok"]:
                chosen = res["decision"]["choice"]
                produced[(nid, chosen)] = ins["in"]
                untaken.update((nid, o) for o in decide_options(n) if o != chosen)
        elif n["kind"] == "ai":
            res = _run_ai(n, ai_ins, os.path.join(run_dir, nid))
            if res["ok"]:
                produced[(nid, "out")] = res["outputs"]["out"]
        else:
            m = reg[n["tool"]]
            res = runner.run_node(m, ins, n.get("params", {}), os.path.join(run_dir, nid),
                                  backend=n.get("backend", "local"), remote_url=n.get("remote_url"))
            if res["ok"]:  # never pass partial files from a failed node downstream
                for port, path in res.get("outputs", {}).items():
                    produced[(nid, port)] = path
        res["seconds"] = round(time.time() - t0, 2)
        res["status"] = "skipped" if res.get("skipped") else ("done" if res["ok"] else "failed")
        results[nid] = res
        on_event({"node": nid, **{k: v for k, v in res.items() if k != "log"}})
    with open(os.path.join(run_dir, "run.json"), "w", encoding="utf-8") as f:
        json.dump({"graph": g, "results": results}, f, indent=1, default=str)
    return {"ok": all(r["ok"] for r in results.values()), "nodes": results}


def _run_decide(n, path):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()[:20000]
        r = decider.choose(text, n.get("question") or "Which option applies?", decide_options(n))
    except Exception as e:  # noqa
        return {"ok": False, "errors": [f"decision failed: {e}"]}
    return {"ok": True, "outputs": {r["choice"]: path}, "decision": r,
            "log": f"decision: {r['choice']} (confidence {r['confidence']}, by {r['backend']})"
                   + (f"\nfallbacks: {r['errors']}" if r["errors"] else "")}


def _run_ai(n, inputs, outdir):
    os.makedirs(outdir, exist_ok=True)
    if not llm.available():
        return {"ok": False, "errors": ["AI node needs an LLM (set ANTHROPIC_API_KEY or OPENAI_BASE_URL)"]}
    ctx = ""
    for p in inputs:
        if p and os.path.isfile(p):
            ctx += f"\n--- {os.path.basename(p)} ---\n" + open(p, errors="replace").read()[:30000]
    try:
        txt = llm.complete("You are a pipeline step. Follow the instruction using the provided files. "
                           "Output only the result content.", f"INSTRUCTION: {n.get('prompt')}\n\nFILES:{ctx}", 4000)
    except Exception as e:  # noqa
        return {"ok": False, "errors": [str(e)]}
    ext = {"application/json": ".json", "text/html": ".html", "text/csv": ".csv"}.get(n.get("mime"), ".md")
    p = os.path.join(outdir, "out" + ext)
    open(p, "w").write(txt)
    return {"ok": True, "outputs": {"out": p}}
