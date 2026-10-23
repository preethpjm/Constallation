"""Local settings (nodeforge.settings.json next to cli.py). Environment variables still win,
so CI/servers can configure NodeForge without the file.

{
  "llm":     {"provider": "anthropic|openai|none", "model": "...", "base_url": "...", "api_key": "..."},
  "decider": "auto|laya|llm|heuristic",
  "laya":    {"model": "english|multilingual|...", "min_confidence": 0.5}
}
The API key is stored in plain text on this machine only; the file is git-ignored.
"""
import json
import os
import threading

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nodeforge.settings.json")
_lock = threading.Lock()
DEFAULTS = {"llm": {"provider": "", "model": "", "base_url": "", "api_key": ""},
            "decider": "auto", "laya": {"model": "", "min_confidence": 0.0}}


def load() -> dict:
    s = json.loads(json.dumps(DEFAULTS))
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k].update(v)
            else:
                s[k] = v
    except (OSError, ValueError):
        pass
    return s


def save(new: dict) -> dict:
    with _lock:
        s = load()
        for k, v in new.items():
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                for kk, vv in v.items():
                    if kk == "api_key" and (vv is None or "…" in str(vv)):
                        continue  # masked value sent back unchanged: keep the stored key
                    s[k][kk] = vv
            else:
                s[k] = v
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, PATH)
        return s


def masked() -> dict:
    s = load()
    k = s["llm"].get("api_key") or ""
    s["llm"]["api_key"] = (k[:7] + "…" + k[-4:]) if len(k) > 14 else ("set…" if k else "")
    return s


def llm_value(key: str, env: str) -> str:
    return os.environ.get(env) or load()["llm"].get(key) or ""
