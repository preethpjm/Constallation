"""Pluggable LLM. Works with Claude, any OpenAI-compatible endpoint (OpenAI,
OpenRouter, Ollama, vLLM, LM Studio), or nothing at all (heuristics only).

Env:
  NODEFORGE_LLM=anthropic|openai|none     (default: auto-detect from keys)
  NODEFORGE_MODEL=<model name>
  ANTHROPIC_API_KEY / OPENAI_API_KEY
  OPENAI_BASE_URL=http://localhost:11434/v1   (for Ollama / local models)
"""
import json
import os

from . import settings


def provider() -> str:
    """Explicit choice (env NODEFORGE_LLM or the settings panel) wins; else detect from env keys."""
    p = os.environ.get("NODEFORGE_LLM") or settings.load()["llm"].get("provider")
    if p:
        return p
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL"):
        return "openai"
    return "none"


def model_name() -> str:
    p = provider()
    return os.environ.get("NODEFORGE_MODEL") or settings.load()["llm"].get("model") or \
        {"anthropic": "claude-sonnet-5-5", "openai": "gpt-4.1"}.get(p, "none")


def available() -> bool:
    return provider() != "none"


def _anthropic():
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY") or settings.load()["llm"].get("api_key") or None
    base = settings.load()["llm"].get("base_url") or None if provider() == "anthropic" else None
    return anthropic.Anthropic(api_key=key, base_url=base)


def _openai():
    from openai import OpenAI
    key = os.environ.get("OPENAI_API_KEY") or settings.load()["llm"].get("api_key") or "not-needed"
    base = settings.llm_value("base_url", "OPENAI_BASE_URL") or None
    return OpenAI(api_key=key, base_url=base)


def test_connection() -> dict:
    """One tiny real call, so a wrong key/model/URL shows up in the UI, not mid-pipeline."""
    if not available():
        return {"ok": False, "error": "no LLM configured"}
    try:
        txt = complete("Reply with exactly: pong", "ping", 20)
        return {"ok": True, "provider": provider(), "model": model_name(), "reply": txt.strip()[:80]}
    except Exception as e:  # noqa
        return {"ok": False, "provider": provider(), "model": model_name(), "error": f"{type(e).__name__}: {e}"[:500]}


def tool_loop(system: str, user: str, tools: list[dict], handlers: dict, max_turns: int = 20,
              stop_tool: str | None = None) -> str:
    """Run a tool-using conversation. tools: [{name, description, schema}].
    handlers[name](**args) -> str. Stops when the model stops calling tools,
    or when stop_tool's handler returns a string starting with 'ACCEPTED'."""
    p = provider()
    if p == "anthropic":
        return _anthropic_loop(system, user, tools, handlers, max_turns, stop_tool)
    if p == "openai":
        return _openai_loop(system, user, tools, handlers, max_turns, stop_tool)
    raise RuntimeError("no LLM configured")


def complete(system: str, user: str, max_tokens: int = 1500, model: str | None = None) -> str:
    p = provider()
    mdl = model or model_name()
    if p == "anthropic":
        r = _anthropic().messages.create(model=mdl, max_tokens=max_tokens, system=system,
                                                  messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in r.content if b.type == "text")
    if p == "openai":
        r = _openai().chat.completions.create(model=mdl, max_tokens=max_tokens, messages=[
            {"role": "system", "content": system}, {"role": "user", "content": user}])
        return r.choices[0].message.content or ""
    raise RuntimeError("no LLM configured")


def _call(handlers, name, args):
    try:
        return str(handlers[name](**args))[:12000]
    except Exception as e:  # noqa
        return f"ERROR: {type(e).__name__}: {e}"


def _anthropic_loop(system, user, tools, handlers, max_turns, stop_tool):
    client = _anthropic()
    tdefs = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]
    msgs = [{"role": "user", "content": user}]
    for _ in range(max_turns):
        r = client.messages.create(model=model_name(), max_tokens=4096, system=system, tools=tdefs, messages=msgs)
        msgs.append({"role": "assistant", "content": r.content})
        calls = [b for b in r.content if b.type == "tool_use"]
        if not calls:
            return "".join(b.text for b in r.content if b.type == "text")
        results, done = [], False
        for c in calls:
            out = _call(handlers, c.name, c.input)
            done = done or (c.name == stop_tool and out.startswith("ACCEPTED"))
            results.append({"type": "tool_result", "tool_use_id": c.id, "content": out})
        msgs.append({"role": "user", "content": results})
        if done:
            return "accepted"
    return "max turns"


def _openai_loop(system, user, tools, handlers, max_turns, stop_tool):
    client = _openai()
    tdefs = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                               "parameters": t["schema"]}} for t in tools]
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for _ in range(max_turns):
        r = client.chat.completions.create(model=model_name(), messages=msgs, tools=tdefs)
        m = r.choices[0].message
        msgs.append(m.model_dump(exclude_none=True))
        if not m.tool_calls:
            return m.content or ""
        done = False
        for c in m.tool_calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            out = _call(handlers, c.function.name, args)
            done = done or (c.function.name == stop_tool and out.startswith("ACCEPTED"))
            msgs.append({"role": "tool", "tool_call_id": c.id, "content": out})
        if done:
            return "accepted"
    return "max turns"
