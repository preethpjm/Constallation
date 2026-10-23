"""Decisions: pick ONE option from a fixed set (or yes/no), with a confidence.

This is exactly what a decision model like Laya is built for (typed questions,
one forward pass, calibrated probabilities, local, no free text to parse).
Backends, in `auto` order:
  laya       - `pip install laya` (local, CPU ok). Used when installed.
  llm        - the configured LLM, asked to answer in JSON.
  heuristic  - word overlap. Always available, never fails.
A failing backend falls through to the next, so a decision is always made,
and every decision records which backend made it.
"""
from __future__ import annotations

import importlib.util
import json
import re
import threading

from . import llm, settings

_laya_router = None
_laya_lock = threading.Lock()


def laya_installed() -> bool:
    return importlib.util.find_spec("laya") is not None


def backend_order() -> list[str]:
    pref = settings.load().get("decider", "auto")
    if pref == "laya":
        return ["laya", "heuristic"]
    if pref == "llm":
        return ["llm", "heuristic"]
    if pref == "heuristic":
        return ["heuristic"]
    order = []
    if laya_installed():
        order.append("laya")
    if llm.available():
        order.append("llm")
    return order + ["heuristic"]


def status() -> dict:
    return {"preference": settings.load().get("decider", "auto"), "order": backend_order(),
            "laya_installed": laya_installed(), "laya_loaded": _laya_router is not None}


# ------------------------------------------------------------------ public API

def choose(state: str, question: str, options: dict[str, str]) -> dict:
    """options: {name: what it means}. -> {choice, confidence, backend, errors}"""
    names = list(options)
    if len(names) == 1:
        return {"choice": names[0], "confidence": 1.0, "backend": "only-option", "errors": []}
    errors = []
    for b in backend_order():
        try:
            r = {"laya": _laya_choose, "llm": _llm_choose, "heuristic": _heur_choose}[b](state, question, options)
            if r["choice"] in options:
                r["backend"] = b
                r["errors"] = errors
                return r
            errors.append(f"{b}: answered '{r['choice']}', not an option")
        except Exception as e:  # noqa
            errors.append(f"{b}: {type(e).__name__}: {str(e)[:200]}")
    raise RuntimeError("no decision backend worked: " + "; ".join(errors))  # heuristic never fails


def yes_no(state: str, question: str) -> dict:
    """-> {yes: probability, backend, errors}"""
    errors = []
    for b in backend_order():
        try:
            if b == "laya":
                ans = _laya_predict(state, {"q": {"type": "noul", "instructions": question}})["q"]
                return {"yes": float(ans["noul"]), "backend": b, "errors": errors}
            if b == "llm":
                r = _llm_choose(state, question, {"yes": "yes", "no": "no"})
                p = r["confidence"] if r["choice"] == "yes" else 1 - r["confidence"]
                return {"yes": p, "backend": b, "errors": errors}
            r = _heur_choose(state, question, {"yes": question, "no": ""})
            return {"yes": 0.5 + 0.5 * (r["confidence"] - 0.5) if r["choice"] == "yes" else 0.4, "backend": b,
                    "errors": errors}
        except Exception as e:  # noqa
            errors.append(f"{b}: {type(e).__name__}: {str(e)[:200]}")
    return {"yes": 0.5, "backend": "none", "errors": errors}


# ------------------------------------------------------------------ laya

def _router():
    global _laya_router
    with _laya_lock:
        if _laya_router is None:
            from laya import Router  # heavy (torch); imported only when actually used
            _laya_router = Router()
        return _laya_router


def _laya_predict(state: str, questions: dict) -> dict:
    kw = {}
    model = settings.load().get("laya", {}).get("model")
    if model:
        kw["model"] = model
    res = _router().predict(state[:20000], questions, **kw)
    return res.get("answers", {})


def _laya_choose(state, question, options):
    ans = _laya_predict(state, {"q": {"type": "choice", "instructions": question,
                                      "criteria": {k: (v or k) for k, v in options.items()}}})["q"]
    choice = ans.get("choice")
    conf = None
    for key in ("confidence", "probability", "prob"):
        if isinstance(ans.get(key), (int, float)):
            conf = float(ans[key]); break
    if conf is None:
        probs = ans.get("probs") or ans.get("probabilities") or ans.get("distribution")
        if isinstance(probs, dict) and choice in probs:
            conf = float(probs[choice])
        elif isinstance(probs, list) and probs:
            conf = float(max(p if isinstance(p, (int, float)) else 0 for p in probs))
    return {"choice": choice, "confidence": conf if conf is not None else 0.0}


# ------------------------------------------------------------------ llm

def _llm_choose(state, question, options):
    opts = "\n".join(f"- {k}: {v}" for k, v in options.items())
    txt = llm.complete(
        "You make one decision. Answer ONLY with JSON: {\"choice\": \"<option name>\", \"confidence\": <0..1>}.",
        f"QUESTION: {question}\nOPTIONS:\n{opts}\n\nINPUT:\n{state[:12000]}", 200)
    d = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    return {"choice": str(d.get("choice", "")).strip(), "confidence": float(d.get("confidence", 0.5))}


# ------------------------------------------------------------------ heuristic

def _tok(s):
    return set(t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) > 1)


def _heur_choose(state, question, options):
    st = _tok(state) | _tok(question)
    scores = {k: len(st & (_tok(k) | _tok(v))) for k, v in options.items()}
    best = max(scores, key=lambda k: (scores[k], -list(options).index(k)))
    total = sum(scores.values()) or 1
    return {"choice": best, "confidence": round(scores[best] / total, 3) if scores[best] else 1 / len(options)}
