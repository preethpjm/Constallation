"""Turn every verified node into an Agent Skill (SKILL.md) an LLM can load on demand.

Why: a node's manifest is for code, node.md is for people. A skill is for a model: a short
description that decides WHEN to load it, and a body that says exactly HOW to call the node,
what to feed it, what comes out, what it chains with and what goes wrong. Facts come from the
manifest and the verification record, never from the model's imagination; an LLM (if configured)
may only improve the wording of the description and the trigger phrases, and its output is
validated before it is used.

Outputs, all under registry/:
  <id>/SKILL.md          one skill per node
  constellation/SKILL.md the master playbook: how to plan, validate, run, and where the node skills are
  SKILLS_INDEX.md        the one-line-per-node index (progressive disclosure: models read this first)
  skills.zip             exported bundle (served by the API, installable by Copilot Studio / Claude)
Activation check: for each skill, sample requests are planned with the real planner; the report says
whether the planner would have picked that node.
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile

from . import graph, llm
from .manifest import REGISTRY_DIR, Manifest, _atomic_write, load_all
from .types import WORD_MIME

EXT_WORD = {}
for _w, _m in WORD_MIME.items():
    EXT_WORD.setdefault(_m, _w)
EXT_WORD.update({"text/plain": "text", "text/markdown": "markdown", "text/html": "web page",
                 "model/gltf-binary": "3D model", "model/step": "CAD file", "application/json": "json",
                 "text/csv": "csv", "application/pdf": "pdf", "image/png": "image"})


def skill_name(m: Manifest) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", m.id.lower()).strip("-")[:64] or "node"


def _word(mime: str) -> str:
    return EXT_WORD.get(mime, mime.split("/")[-1])


def _desc_heuristic(m: Manifest) -> str:
    ins = " + ".join(dict.fromkeys(_word(p.mime) for p in m.file_inputs() if p.mime != "*/*")) or "no file"
    outs = " + ".join(dict.fromkeys(_word(p.mime) for p in m.outputs)) or "a result"
    what = (m.description or m.name).strip().rstrip(".")
    return (f"{what}. Takes {ins} and produces {outs}. Use when a request needs {outs} from {ins}, "
            f"or when a pipeline step must turn {ins} into {outs}.")[:1000]


def triggers(m: Manifest) -> list[str]:
    """Sample requests that SHOULD lead to this node (used for the activation check)."""
    outs = list(dict.fromkeys(_word(p.mime) for p in m.outputs if p.mime != "*/*"))
    ins = list(dict.fromkeys(_word(p.mime) for p in m.file_inputs() if p.mime != "*/*"))
    t = []
    for o in outs[:2]:
        for i in (ins[:2] or [""]):
            if i == o:
                continue
            src = "" if not i else f" from the {i}" if i.endswith("file") else f" from the {i} file"
            t.append(f"make a {o}{src}")
            t.append(f"I need the {o}" + (f" for this {i}" if i else ""))
    t.append(m.id.replace("-", " "))
    return list(dict.fromkeys(t))[:5]


def _llm_polish(m: Manifest, base_desc: str, trig: list[str]) -> tuple[str, list[str]]:
    """Optional: better wording for the description and triggers. Validated; falls back silently."""
    if not llm.available():
        return base_desc, trig
    try:
        raw = llm.complete(
            "You write the metadata for an Agent Skill. Reply with ONLY JSON: "
            '{"description": "<=300 chars: what it does and when to use it, third person", '
            '"triggers": ["5 short requests a user might type that need this skill"]}. '
            "Use only facts in the node record. No angle brackets.",
            json.dumps({"id": m.id, "description": m.description, "inputs": [(p.name, p.mime) for p in m.inputs],
                        "outputs": [(p.name, p.mime) for p in m.outputs], "tags": m.tags}), 500)
        j = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        d, tr = str(j["description"]).strip(), [str(x).strip() for x in j["triggers"]][:6]
        ok = 20 <= len(d) <= 1000 and "<" not in d and ">" not in d and tr and all(1 < len(x) < 160 for x in tr)
        return (d, tr) if ok else (base_desc, trig)
    except Exception:  # noqa  wording is optional; facts are not
        return base_desc, trig


def neighbours(m: Manifest, reg: dict[str, Manifest]) -> tuple[list[str], list[str]]:
    """Which nodes can feed this one, and which can consume what it makes (by declared MIME types)."""
    from .types import compatible
    feeds, takes = [], []
    for o in reg.values():
        if o.id == m.id or o.verification.status != "verified":
            continue
        if any(compatible(op.mime, ip.mime) for op in o.outputs for ip in m.file_inputs() if ip.mime != "*/*"):
            feeds.append(o.id)
        if any(compatible(op.mime, ip.mime) for op in m.outputs for ip in o.file_inputs() if ip.mime != "*/*"):
            takes.append(o.id)
    return feeds[:6], takes[:6]


def skill_md(m: Manifest, reg: dict[str, Manifest] | None = None, polish: bool = True) -> str:
    reg = reg if reg is not None else load_all()
    desc, trig = _desc_heuristic(m), triggers(m)
    if polish and m.verification.status == "verified":
        desc, trig = _llm_polish(m, desc, trig)
    desc = desc.replace("\n", " ").replace("<", "").replace(">", "")
    feeds, takes = neighbours(m, reg)

    def rows(ports):
        return "\n".join(f"| `{p.name}` | {p.kind} | {p.mime if p.kind != 'param' else '-'} | "
                         f"{'yes' if p.required else 'no'} | {(p.description or (('default ' + str(p.default)) if p.default else '')).replace('|', '/')} |"
                         for p in ports) or "| - | | | | |"
    v = m.verification
    pitfalls = []
    if v.status != "verified":
        pitfalls.append(f"**Not verified** ({v.status}). Do not plan with this node. Last log: "
                        f"{(v.log.splitlines() or [''])[0][:200]}")
    if m.runtime.kind == "http":
        pitfalls.append("Runs as a service: it is started on demand and stopped afterwards; allow startup time.")
    if m.runtime.setup:
        pitfalls.append("First run installs dependencies: " + ", ".join(f"`{s}`" for s in m.runtime.setup) + ".")
    for p in m.params():
        if p.default is not None:
            pitfalls.append(f"Parameter `{p.name}` defaults to `{p.default}`; set it explicitly if the default is not what the user meant.")
    if m.origin == "wrapper":
        pitfalls.append(f"Generated wrapper around `{m.wrapper_of}`: it exposes only that function. "
                        "Parameters without a default in the source use a placeholder value unless set.")
    if m.verification.status == "verified" and not m.human_approved:
        pitfalls.append("Verified by a real run, but not yet approved by a person.")
    return f"""---
name: {skill_name(m)}
description: {desc}
---

# {m.name}

{m.description or 'No description in the source; judge by the ports below.'}

## When to use
{chr(10).join('- "' + t + '"' for t in trig)}

Do not use it when the request needs something other than {', '.join(_word(p.mime) for p in m.outputs) or 'its output'}.

## Contract
**Inputs**

| port | kind | type | required | notes |
|---|---|---|---|---|
{rows(m.inputs)}

**Outputs**

| port | kind | type | required | notes |
|---|---|---|---|---|
{rows(m.outputs)}

## How to call it
Through Constellation, never by running the tool yourself. Add node `{m.id}` to a plan, connect each file
input to an upload or to an upstream output of the same type, then run the plan. Constellation checks the
connections, runs the node on its configured backend and returns the output files.

```
command: {m.runtime.command if m.runtime.kind == 'cli' else f'{m.runtime.start}  ->  {m.runtime.method} {m.runtime.route}'}
```

## Verified example
- inputs: `{json.dumps(v.example_inputs)}`
- parameters: `{json.dumps(v.example_params)}`
- observed outputs: `{json.dumps(v.observed_outputs)}`

## Chains with
- Can be fed by: {', '.join(f'`{x}`' for x in feeds) or 'uploads only'}
- Its output can go to: {', '.join(f'`{x}`' for x in takes) or 'the final result'}

## Pitfalls
{chr(10).join('- ' + p for p in pitfalls) or '- None recorded.'}
"""


MASTER = """---
name: constellation
description: Plans, validates and runs multi-step pipelines out of verified internal tools (nodes). Use when a request needs several tools chained, files converted between formats, or an internal tool run safely and logged.
---

# Constellation playbook

Constellation holds {n} verified nodes. Each is an onboarded internal tool with a typed contract.
Never run a tool directly: ask Constellation to plan, validate and run.

## Procedure
1. Read `SKILLS_INDEX.md` (one line per node). Load a node's `SKILL.md` only if it looks relevant.
2. Plan: `POST /api/plan` with the request and the names of the uploaded files. A plan is a graph of nodes and typed edges.
3. Validate: `POST /api/validate`. Every connection must be type-compatible, every required input connected, no cycles.
   Fix problems by choosing another node or adding a converter node. Never edit a plan to hide an error.
4. Show the plan to the person. Anything that writes to another system needs their approval first.
5. Run: `POST /api/run`. Report the output files and the decision log (what was chosen, by what, with what confidence).

## Rules
- Only verified nodes are used. If none produces what is needed, say which kind of tool is missing.
- A converter node is the right fix for a type mismatch; do not reinterpret a file as another type.
- If the decision model is unavailable, the heuristic fallback is used and the log says so.
- Report failures with the node's log line, not a guess.

## Nodes
{index}
"""


def index_md(reg: dict[str, Manifest]) -> str:
    lines = ["# Skills index", "", "| node | does | in -> out |", "|---|---|---|"]
    for m in reg.values():
        if m.verification.status != "verified":
            continue
        ins = ", ".join(_word(p.mime) for p in m.file_inputs()) or "-"
        outs = ", ".join(_word(p.mime) for p in m.outputs) or "-"
        lines.append(f"| [`{m.id}`]({m.id}/SKILL.md) | {(m.description or m.name)[:90].replace('|', '/')} | {ins} -> {outs} |")
    return "\n".join(lines) + "\n"


def generate_all(polish: bool = True, log=print) -> dict:
    reg = load_all()
    made = []
    for m in reg.values():
        if m.verification.status != "verified":
            continue
        _atomic_write(os.path.join(REGISTRY_DIR, m.id, "SKILL.md"), skill_md(m, reg, polish))
        made.append(m.id)
    idx = index_md(reg)
    _atomic_write(os.path.join(REGISTRY_DIR, "SKILLS_INDEX.md"), idx)
    os.makedirs(os.path.join(REGISTRY_DIR, "constellation"), exist_ok=True)
    _atomic_write(os.path.join(REGISTRY_DIR, "constellation", "SKILL.md"),
                  MASTER.format(n=len(made), index="\n".join(l for l in idx.splitlines()[2:])))
    log(f"skills: wrote {len(made)} node skills + playbook + index")
    return {"nodes": made}


def bundle() -> bytes:
    """Zip of constellation/SKILL.md + <id>/SKILL.md (+ node.md/manifest.json for reference)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(REGISTRY_DIR):
            rel = os.path.relpath(root, REGISTRY_DIR)
            if rel == "." or "app" in rel.split(os.sep) or "verify_out" in rel.split(os.sep):
                continue
            for f in files:
                if f in ("SKILL.md", "node.md", "manifest.json"):
                    z.write(os.path.join(root, f), os.path.join(rel, f))
        z.write(os.path.join(REGISTRY_DIR, "SKILLS_INDEX.md"), "SKILLS_INDEX.md") if os.path.exists(
            os.path.join(REGISTRY_DIR, "SKILLS_INDEX.md")) else None
    return buf.getvalue()


def activation_check(polish: bool = False) -> dict:
    """Would the planner pick this node for the requests the skill says it is for?
    right = the node is in the plan; alternative = another verified node makes the same type (the request
    does not tell them apart, which is a description problem, not a planner bug); miss = neither."""
    reg = load_all()
    report = {}
    for m in reg.values():
        if m.verification.status != "verified":
            continue
        res = []
        want = {o.mime for o in m.outputs}
        for t in triggers(m):
            verdict, used = "miss", []
            try:
                g = graph.plan_heuristic(t, reg, [])
                used = [n["tool"] for n in g.get("nodes", []) if n.get("kind") == "tool"]
                if m.id in used:
                    verdict = "right"
                else:
                    byid = {n["id"]: n for n in g["nodes"]}
                    made = set()
                    for e in g["edges"]:
                        if byid.get(e["dst"], {}).get("kind") == "output" and byid.get(e["src"], {}).get("kind") == "tool":
                            tm = reg.get(byid[e["src"]]["tool"])
                            made |= {o.mime for o in tm.outputs if o.name == e["src_port"]} if tm else set()
                    if made & want and not graph.validate(g):
                        verdict = "alternative"
            except Exception as e:  # noqa
                used = [f"error: {e}"]
            res.append({"request": t, "picked": verdict == "right", "verdict": verdict, "plan": used})
        report[m.id] = {"hit": sum(r["picked"] for r in res), "of": len(res), "cases": res}
    alt = sum(c["verdict"] == "alternative" for v in report.values() for c in v["cases"])
    return {"hit": sum(v["hit"] for v in report.values()), "of": sum(v["of"] for v in report.values()),
            "alternative": alt, "by_node": report}
