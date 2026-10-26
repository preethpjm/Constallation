"""Pipeline graphs: validation (deterministic) and planning (LLM or heuristic).

The planner PROPOSES, the validator DECIDES. An invalid graph never reaches
the canvas: type mismatches, missing required inputs, unknown tools and
cycles are all rejected here, whoever produced the plan.
"""
from __future__ import annotations

import json
import os
import re

from . import decider, llm, settings
from .manifest import REGISTRY_DIR, Manifest, load_all, load_md
from .types import WORD_MIME, compatible, mime_from_name

AI_NODE = Manifest(id="ai", name="AI step", description="LLM transform: reads text inputs, follows a prompt, "
                   "writes a text/markdown/json result", tool_dir="", language="llm",
                   inputs=[], outputs=[])


def decide_options(node: dict) -> dict[str, str]:
    """options as {name: meaning}; accepts a dict or "name: meaning" lines."""
    o = node.get("options") or {"yes": "", "no": ""}
    if isinstance(o, str):
        out = {}
        for line in o.splitlines():
            if line.strip():
                k, _, v = line.partition(":")
                k = re.sub(r"[^a-zA-Z0-9_]+", "_", k.strip()).strip("_")
                if k:
                    out[k] = v.strip()
        return out or {"yes": "", "no": ""}
    return dict(o)


def ports_of(node: dict, reg: dict[str, Manifest]):
    """-> (inputs{name:mime}, outputs{name:mime}) for any node kind."""
    k = node["kind"]
    if k == "input":
        return {}, {"file": node.get("mime", "*/*")}
    if k == "output":
        return {"file": "*/*"}, {}
    if k == "ai":
        return {"in": "text/plain"}, {"out": node.get("mime", "text/markdown")}
    if k == "decide":  # routes its input, unchanged, to the port of the chosen option
        return {"in": "text/plain"}, {o: "*/*" for o in decide_options(node)}
    m = reg[node["tool"]]
    return ({p.name: p.mime for p in m.file_inputs()}, {p.name: p.mime for p in m.outputs})


def validate(g: dict, reg: dict[str, Manifest] | None = None) -> list[str]:
    reg = reg if reg is not None else load_all()
    errs = []
    ids = {}
    for n in g.get("nodes", []):
        if n.get("id") in ids:
            errs.append(f"duplicate node id {n.get('id')}")
        ids[n.get("id")] = n
        if n.get("kind") not in ("input", "output", "tool", "ai", "decide"):
            errs.append(f"node {n.get('id')}: bad kind {n.get('kind')}")
        if n.get("kind") == "tool" and n.get("tool") not in reg:
            errs.append(f"node {n['id']}: unknown tool '{n.get('tool')}'")
        if n.get("kind") == "tool" and n.get("tool") in reg and reg[n["tool"]].verification.status != "verified":
            errs.append(f"node {n['id']}: tool '{n['tool']}' is not verified")
    if errs:
        return errs
    incoming = {}
    for e in g.get("edges", []):
        s, d = ids.get(e["src"]), ids.get(e["dst"])
        if not s or not d:
            errs.append(f"edge {e}: unknown node"); continue
        sin, sout = ports_of(s, reg)
        din, dout = ports_of(d, reg)
        sp, dp = e.get("src_port"), e.get("dst_port")
        if sp not in sout:
            errs.append(f"{e['src']} has no output '{sp}' (has {list(sout)})"); continue
        if dp not in din:
            errs.append(f"{e['dst']} has no input '{dp}' (has {list(din)})"); continue
        if not compatible(sout[sp], din[dp]) and sout[sp] != "*/*":
            errs.append(f"type mismatch: {e['src']}.{sp} ({sout[sp]}) -> {e['dst']}.{dp} ({din[dp]})")
        key = (e["dst"], dp)
        if key in incoming and d["kind"] != "ai":
            errs.append(f"{e['dst']}.{dp} has more than one incoming edge")
        incoming[key] = e
    for n in ids.values():
        if n["kind"] == "tool":
            for p in reg[n["tool"]].file_inputs():
                if p.required and (n["id"], p.name) not in incoming:
                    errs.append(f"{n['id']}: required input '{p.name}' ({p.mime}) is not connected")
        if n["kind"] in ("output", "ai", "decide") and not any(k[0] == n["id"] for k in incoming):
            errs.append(f"{n['id']}: nothing connected")
    # cycles
    adj = {}
    for e in g.get("edges", []):
        adj.setdefault(e["src"], []).append(e["dst"])
    state = {}

    def dfs(u):
        state[u] = 1
        for v in adj.get(u, []):
            if state.get(v) == 1 or (state.get(v) is None and dfs(v)):
                return True
        state[u] = 2
        return False
    if any(state.get(u) is None and dfs(u) for u in ids):
        errs.append("graph has a cycle")
    return errs


def topo(g: dict) -> list[str]:
    deps = {n["id"]: set() for n in g["nodes"]}
    for e in g["edges"]:
        deps[e["dst"]].add(e["src"])
    order = []
    while deps:
        ready = sorted(k for k, v in deps.items() if not v)
        if not ready:
            raise ValueError("cycle")
        for k in ready:
            order.append(k); deps.pop(k)
        for v in deps.values():
            v.difference_update(ready)
    return order


# ---------------------------------------------------------------- planning

SYN = {"3d": ["glb", "step", "cad", "model", "mesh", "assembly"], "bom": ["bill", "materials", "parts", "compare"],
       "listing": ["html", "page", "shop", "buy"], "affiliate": ["listing", "shop", "buy", "link"],
       "price": ["pricer", "pricing", "retail", "margin", "currency"], "pricing": ["pricer", "price", "margin"],
       "supplier": ["risk", "score", "vendor"], "risk": ["supplier", "score"], "preview": ["glb", "3d"],
       "cost": ["price", "total", "aggregate"], "aggregate": ["compare", "total"]}
OUTPUT_WORDS = {"listing": "text/html", "page": "text/html", "website": "text/html", "html": "text/html",
                "glb": "model/gltf-binary", "preview": "model/gltf-binary", "3d": "model/gltf-binary",
                "pdf": "application/pdf", "report": "text/csv", "risk": "application/json",
                "pricing": "text/csv", "price": "text/csv", "json": "application/json"}


def _stem(t):
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")) else t


def _tok(s):
    return [_stem(t) for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


def _near(words, text):
    """Words that appear in the text, allowing a word to be the start of a longer one (cgm ~ cgmconverter)."""
    hit = set()
    for w in words:
        if w in text or (len(w) >= 3 and any(t.startswith(w) or (len(t) >= 4 and w.startswith(t)) for t in text)):
            hit.add(w)
    return hit


def _expand(words):
    out = set(words)
    for w in words:
        out.update(SYN.get(w, []))
    return out


def relevance(m: Manifest, words: set) -> float:
    text = set(_tok(m.id) + [_stem(t) for t in m.tags] + _tok(m.description))
    return len(_near(words, text)) + 0.5 * len(words & set(_tok(load_md(m.id))))


STOP = {"the", "a", "an", "and", "or", "of", "for", "in", "on", "to", "from", "with", "by", "is", "it", "this", "that",
        "me", "my", "i", "need", "make", "get", "convert", "match", "into", "as", "at", "be", "can", "do"}


def _strong(m: Manifest, words: set) -> int:
    """Overlap between the request's meaningful words and what the node says about itself (id, tags, description)."""
    return len(_near(words - STOP, set(_tok(m.id) + [_stem(t) for t in m.tags] + _tok(m.description))))


CUE_VOCAB = {"convert", "make", "create", "generate", "produce", "pipeline", "workflow", "please", "image", "picture", "drawing",
             "markdown", "report", "listing", "preview", "upper", "lower", "case", "price", "pricing", "supplier", "email", "slug",
             "dedupe", "duplicate", "compare", "extract", "summary", "table", "spreadsheet", "document", "translate", "resize"}


def _correct(request: str, reg: dict) -> str:
    """Fix typos and split/joined words against what the registry and the type words actually contain:
    'pipline ... liek ...' and 'testrep oshout' still find their tool."""
    import difflib
    vocab, ids = set(CUE_VOCAB) | set(OUTPUT_WORDS) | set(WORD_MIME) | set(SYN) | {"like", "from", "into", "that"}, {}
    for m in reg.values():
        vocab.update(_tok(m.id) + [_stem(t) for t in m.tags] + _tok(m.description))
        ids[re.sub(r"[^a-z0-9]", "", m.id.lower())] = m.id
    raw = re.findall(r"[A-Za-z0-9]+|[^A-Za-z0-9]+", request)
    words = [t for t in raw if t[0].isalnum()]
    out, i = [], 0
    while i < len(words):
        w = words[i].lower()
        if i + 1 < len(words):   # "testrep oshout" -> testrepo-shout
            j = w + words[i + 1].lower()
            hit = difflib.get_close_matches(j, list(ids), n=1, cutoff=0.86)
            if hit and w not in vocab:
                out.append(ids[hit[0]].replace("-", " ")); i += 2; continue
        if len(w) >= 4 and _stem(w) not in vocab and w not in vocab and not any(c.isdigit() for c in w):
            hit = [h for h in difflib.get_close_matches(_stem(w), list(vocab) + list(ids), n=3, cutoff=0.84)
                   if h[0] == w[0] and abs(len(h) - len(w)) <= 1][:1] or \
                [v for v in vocab if len(v) == len(w) and v[0] == w[0] and sorted(v) == sorted(w)][:1]
            if hit:
                out.append(ids.get(hit[0], hit[0]).replace("-", " ")); i += 1; continue
        elif len(w) >= 6 and any(c.isdigit() for c in w):   # stepp2glb
            hit = difflib.get_close_matches(w, list(ids), n=1, cutoff=0.8)
            if hit:
                out.append(ids[hit[0]].replace("-", " ")); i += 1; continue
        out.append(words[i]); i += 1
    return " ".join(out)


def plan_heuristic(request: str, reg: dict[str, Manifest], uploads: list[str] | None = None) -> dict:
    request = _correct(request, {k: v for k, v in reg.items() if v.verification.status == "verified"})
    words = _expand(_tok(request))
    all_reg = reg
    reg = {k: v for k, v in reg.items() if v.verification.status == "verified"}
    notes = []
    for w in _tok(request):  # asked for something only a broken tool could make? say so
        mm = OUTPUT_WORDS.get(w)
        if mm and not any(o.mime == mm for m in reg.values() for o in m.outputs):
            broken = [m for m in all_reg.values() if m.verification.status != "verified" and
                      (any(o.mime == mm for o in m.outputs) or relevance(m, _expand([w])) > 0)]
            if broken:
                notes.append(f"'{w}' needs {', '.join(m.id for m in broken)}, which is not verified: "
                             + "; ".join(m.verification.log.splitlines()[0][:200] for m in broken if m.verification.log))
            else:
                notes.append(f"no node produces {mm} for '{w}' yet; onboard a tool that does")
    targets = []   # (mime, the word that asked for it)
    for w in _tok(request):
        mm = OUTPUT_WORDS.get(w)
        if mm and mm not in [t[0] for t in targets] and any(o.mime == mm for m in reg.values() for o in m.outputs):
            targets.append((mm, w))
    # "convert X to Y", "make a Y from the X": the type named as the result wins over words that describe inputs
    m_t = re.search(r"\b(?:to|into|as)\s+(?:an?\s+|the\s+)?([a-z0-9]+)|\b(?:make|create|generate|produce|need|get)\s+(?:me\s+)?(?:an?\s+|the\s+)?([a-z0-9]+)", request.lower())
    if m_t:
        w = _stem(m_t.group(1) or m_t.group(2))
        mm = OUTPUT_WORDS.get(w) or WORD_MIME.get(w) or {"text": "text/plain", "txt": "text/plain", "markdown": "text/markdown"}.get(w)
        if mm and any(o.mime == mm for m in reg.values() for o in m.outputs):
            targets = [(mm, w)]
    if True:   # "convert cgms to images like svg": a type word some node can make, after the cue word if any
        toks = _tok(request)
        cues = [i for i, w in enumerate(toks) if w in ("to", "into", "as", "like", "make", "create", "generate")]
        cue = -1
        FAMILY = {"image": "image/", "picture": "image/", "drawing": "image/", "graphic": "image/", "vector": "image/",
                  "illustration": "image/", "photo": "image/"}
        def producer_mime(w):   # exact type first; then any type of the family a node can make ("pictures" -> svg)
            mm = OUTPUT_WORDS.get(w) or WORD_MIME.get(w) or {"text": "text/plain", "txt": "text/plain", "markdown": "text/markdown"}.get(w)
            outs = [o.mime for m in reg.values() for o in m.outputs]
            if mm in outs:
                return mm
            return next((x for x in outs if w in FAMILY and x.startswith(FAMILY[w])), None)
        cands = [(i, w, producer_mime(w)) for i, w in enumerate(toks)]
        cands = [(i, w, mm) for i, w, mm in cands if mm]
        direct = [c for c in cands if any(0 < c[0] - q <= 2 for q in cues)]   # the type word right after "to"/"like"/"make"
        if direct or (cands and not targets):
            _, w, mm = direct[-1] if direct else cands[-1]
            targets = [(mm, w)]
    if not targets:  # fall back: the most relevant node's outputs
        best = max(reg.values(), key=lambda m: relevance(m, words), default=None)
        # a weak match is not a plan: better an honest "nothing fits" than a random chain of tools
        meaningful = {w for w in words - STOP if len(w) >= 4}
        ranked = sorted(reg.values(), key=lambda m: -_strong(m, meaningful))
        top = _strong(ranked[0], meaningful) if ranked else 0
        clear = top >= 1 and (len(ranked) < 2 or top - _strong(ranked[1], meaningful) >= 1)
        targets = [(best.outputs[0].mime, "")] if best and best.outputs and (_strong(best, words) >= 2 or best.id.lower() in request.lower() or clear) else []
    nodes, edges, producing = [], [], {}   # producing[mime] = [(node_id, port)]
    decisions = []

    def pick(cands, state, question):
        """Several tools could do it: let the decision engine choose (Laya/LLM if present)."""
        cands = sorted(cands, key=lambda c: -c[0])
        if len(cands) == 1 or decider.backend_order()[0] == "heuristic":
            return cands[0]
        top = cands[0][0]
        close = [c for c in cands if c[0] >= 0.7 * top][:8]
        if len(close) == 1:          # a clear winner: no need to ask anyone
            return cands[0]
        opts = {f"{m.id}.{o.name}": f"{m.description} (outputs {o.name}: {o.mime})" for _, m, o in close}
        r = decider.choose(state, question, opts)
        floor = float(settings.load().get("laya", {}).get("min_confidence") or 0.5)
        use = r["confidence"] >= floor
        decisions.append({"question": question, "options": list(opts), "choice": r["choice"],
                          "confidence": r["confidence"], "backend": r["backend"],
                          "used": use, **({} if use else {"kept": f"{close[0][1].id}.{close[0][2].name}"})})
        if not use:                  # unsure decision: keep the heuristic's pick
            return close[0]
        return next(c for c in close if f"{c[1].id}.{c[2].name}" == r["choice"])
    counter = {"n": 0}

    def nid(prefix):
        counter["n"] += 1
        return f"{prefix}{counter['n']}"

    def add_upload(mime, label):
        for n in nodes:
            if n["kind"] == "input" and n["mime"] == mime and n["label"] == label:
                return n["id"]
        i = nid("in")
        nodes.append({"id": i, "kind": "input", "mime": mime, "label": label})
        return i

    cur_target = [None]

    def satisfy(mime, port_name, consumer_tool, depth, chain):
        # 1) reuse something already in the graph
        for (src, sp) in producing.get(mime, []):
            if src not in chain:
                return src, sp
        generic = {"csv", "json", "file", "input", "in", "out", "txt", "path", "data", "html", "step", "stp"}
        pw = set(_tok(port_name)) - generic
        said = set(_tok(request)) | {t for u in uploads or [] for t in _tok(os.path.basename(u))}
        # 2) the user explicitly has this thing ("my bom", bom.csv uploaded for port bom_csv)
        if pw & said:
            return add_upload(mime, port_name), "file"
        # 2b) a generic port ("in_csv", "input") and the user named that type or uploaded such a file
        if not pw and mime in said_mimes:
            return add_upload(mime, port_name), "file"
        # 2c) a generic port and the user named exactly one type it can take: that is the upload
        if not pw and mime != "model/step":
            have = {x for x in said_mimes if x != cur_target[0] and x != "text/plain" and compatible(x, mime)}
            if len(have) == 1:
                return add_upload(next(iter(have)), port_name), "file"
        # 3) a relevant tool can make it
        if depth < 6:
            cands = []
            for m in reg.values():
                if m.id == consumer_tool or m.id in chain:
                    continue
                for o in m.outputs:
                    if compatible(o.mime, mime):
                        cands.append((relevance(m, words) + 3 * len(pw & set(_tok(o.name))), m, o))
            cands = [c for c in cands if c[0] > 0 and (_strong(c[1], words) > 0 or pw & set(_tok(c[2].name)))]
            if cands:
                _, m, o = pick(cands, f"{request}\nNeeded: '{port_name}' ({mime}) as input of {consumer_tool}",
                               f"Which tool should produce the '{port_name}' input of {consumer_tool}?")
                n_id = add_tool(m, depth + 1, chain | {m.id})
                return n_id, o.name
        return add_upload(mime, port_name), "file"

    def add_tool(m: Manifest, depth, chain):
        for n in nodes:
            if n.get("tool") == m.id:
                return n["id"]
        i = nid("t")
        params = {p.name: (p.default or "") for p in m.params()}
        nodes.append({"id": i, "kind": "tool", "tool": m.id, "params": params, "backend": "local"})
        for p in m.file_inputs():
            if not p.required:
                continue
            src, sp = satisfy(p.mime, p.name, m.id, depth, chain)
            edges.append({"src": src, "src_port": sp, "dst": i, "dst_port": p.name})
        for o in m.outputs:  # registered only after its inputs are wired: no self-loops
            producing.setdefault(o.mime, []).append((i, o.name))
        return i

    TEXTWORDS = {"text": "text/plain", "txt": "text/plain", "markdown": "text/markdown", "md": "text/markdown"}
    said_mimes = {WORD_MIME.get(w) or TEXTWORDS[w] for w in _tok(request) if w in WORD_MIME or w in TEXTWORDS}
    said_mimes |= {mm for u in uploads or [] for mm in [mime_from_name(os.path.basename(u))] if mm}

    def fit_inputs(m, target):
        """Bonus for tools whose required file inputs are things the user said they have (the target excluded)."""
        need = [p.mime for p in m.file_inputs() if p.required and p.mime != "*/*"]
        have = said_mimes - {target}
        return 6.0 * sum(x in have for x in need) / len(need) if need else 0.0

    for t, word in targets:
        cur_target[0] = t
        ask = _expand([word]) if word else set()
        prods = [(relevance(m, words) + 5 * relevance(m, ask) + fit_inputs(m, t), m, o)
                 for m in reg.values() for o in m.outputs if o.mime == t]
        if not prods:
            continue
        _, m, o = pick(prods, request, f"Which tool output best delivers the requested {t} result?")
        src = add_tool(m, 0, {m.id})
        out = nid("out")
        nodes.append({"id": out, "kind": "output", "label": f"{m.id}.{o.name}"})
        edges.append({"src": src, "src_port": o.name, "dst": out, "dst_port": "file"})
    g = {"nodes": nodes, "edges": edges, "planner": "heuristic"}
    if decisions:
        g["decisions"] = decisions
        g["planner"] = f"heuristic + {sum(d['used'] for d in decisions)}/{len(decisions)} decisions by {decisions[0]['backend']}"
    if notes:
        g["note"] = " | ".join(dict.fromkeys(notes))
    return g


PLAN_SYSTEM = """You design data pipelines from a registry of verified tool nodes.
Return ONLY JSON: {"nodes":[...],"edges":[...]}.
Node kinds:
 - {"id":"in1","kind":"input","mime":"<type>","label":"what the user uploads"}
 - {"id":"t1","kind":"tool","tool":"<registry id>","params":{...},"backend":"local"}
 - {"id":"a1","kind":"ai","prompt":"...","mime":"text/markdown"}   (LLM step; input port "in", output port "out")
 - {"id":"out1","kind":"output","label":"..."}                    (input port "file")
 - {"id":"d1","kind":"decide","question":"...","options":{"yes":"meaning","no":"meaning"}}
   (branch: input port "in"; the input is passed unchanged to the output port named after the chosen option;
    nodes on branches not taken are skipped)
Edges: {"src":"t1","src_port":"<output port>","dst":"t2","dst_port":"<input port>"}; input nodes expose port "file".
Types on both ends of an edge must match. Connect every required input. Use only tools listed.
Prefer reusing one tool's output for several consumers over duplicating tools.
TOOLS FIRST: every step a listed tool can do must use that tool, matched by its description, tags, ports and
"when_to_use" phrases (plural/typo/loose wording in the request still counts: "cgms to images like svg" means the
cgm -> svg tool). An "ai" node is only a helper for a step no tool can do (summarising, rewriting, judging text);
never replace a tool step with an ai node, and never return a plan made only of ai nodes when a tool fits.
Never return an empty plan. If no listed tool can do a step, use an "ai" node for that step (give it a precise prompt)
and add a top-level "note" string saying which kind of tool is missing from the registry. Every plan ends in an output node."""


def registry_brief(reg):
    lines = []
    for m in reg.values():
        if m.verification.status != "verified":
            continue
        when = []
        sk = os.path.join(REGISTRY_DIR, m.id, "SKILL.md")
        if os.path.exists(sk):
            txt = open(sk, encoding="utf-8", errors="replace").read()
            sec = re.search(r"## When to use(.*?)(?:\n## |\Z)", txt, re.S)
            when = re.findall(r'^\s*[-*]\s*"([^"]+)"', sec.group(1), re.M)[:4] if sec else []
        lines.append(json.dumps({"id": m.id, "description": m.description, "tags": m.tags[:10], "when_to_use": when,
                                 "inputs": [{"port": p.name, "kind": p.kind, "type": p.mime, "required": p.required}
                                            for p in m.inputs],
                                 "outputs": [{"port": o.name, "type": o.mime} for o in m.outputs]}))
    return "\n".join(lines)


def _strip_think(txt: str) -> str:
    """Reasoning models (qwen3, deepseek-r1) put <think>...</think> before the answer; braces in there break parsing."""
    return re.sub(r"<think>.*?</think>", "", txt, flags=re.S | re.I)


def _usable(g: dict) -> list[str]:
    """validate() accepts an empty graph (nothing is wrong with nothing); a plan must actually do something."""
    if not any(n.get("kind") in ("tool", "ai") for n in g.get("nodes", [])):
        return ["the plan has no tool or ai nodes; if no tool fits, use ai nodes and explain what is missing in 'note'"]
    if not any(n.get("kind") == "output" for n in g.get("nodes", [])):
        return ["the plan has no output node"]
    return []


def plan_ai_only(request: str, uploads: list[str] | None, why: str) -> dict:
    """No registered tool fits: an honest AI-step pipeline instead of an empty canvas."""
    rl = request.lower()
    mime = next((m for w, m in (("pdf", "application/pdf"), ("csv", "text/csv"), ("json", "application/json"),
                                ("html", "text/html")) if w in rl), "*/*")
    return {"nodes": [{"id": "in1", "kind": "input", "mime": mime, "label": "input"},
                      {"id": "a1", "kind": "ai", "prompt": request.strip(), "mime": "text/markdown"},
                      {"id": "out1", "kind": "output", "label": "result"}],
            "edges": [{"src": "in1", "src_port": "file", "dst": "a1", "dst_port": "in"},
                      {"src": "a1", "src_port": "out", "dst": "out1", "dst_port": "file"}],
            "planner": "ai-only fallback",
            "note": f"No registered tool fits this request ({why}). Showing a single AI step so you can start; "
                    "onboard tools for the parts that should be exact (e.g. a PDF text extractor, a scoring script) "
                    "and ask again."}


def plan(request: str, uploads: list[str] | None = None) -> dict:
    g = _plan(request, uploads)
    if _usable(g):
        return plan_ai_only(request, uploads, g.get("note") or "no matching verified node")
    return g


def _plan(request: str, uploads: list[str] | None = None) -> dict:
    reg = load_all()
    if llm.available():
        msg = f"REGISTRY:\n{registry_brief(reg)}\n\nUSER UPLOADS: {uploads or []}\nREQUEST: {request}"
        last = None
        for _ in range(3):
            try:
                txt = _strip_think(llm.complete(PLAN_SYSTEM, msg, 3000))
                g = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
                errs = validate(g, reg) or _usable(g)
                if not errs:
                    g["planner"] = f"llm:{llm.model_name()}"
                    if not any(n.get("kind") == "tool" for n in g["nodes"]):   # tools first: AI-only plan while a tool fits
                        h = plan_heuristic(request, reg, uploads)
                        if any(n.get("kind") == "tool" for n in h["nodes"]) and not validate(h, reg):
                            h["note"] = f"The LLM proposed only AI steps; {llm.model_name()} was overruled because registered tools fit."
                            return h
                    return g
                msg += f"\n\nYOUR PLAN:\n{json.dumps(g)}\nVALIDATION ERRORS (fix them):\n" + "\n".join(errs)
                last = errs
            except Exception as e:  # noqa
                last = [str(e)]
        fallback = plan_heuristic(request, reg, uploads)
        fallback["note"] = f"LLM plan rejected ({last}); heuristic plan used"
        return fallback
    return plan_heuristic(request, reg, uploads)
