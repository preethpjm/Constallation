"""Small general-purpose nodes that ship with Constellation and register themselves on start-up."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILTIN_DIR = os.path.join(ROOT, "builtin_nodes")


def ensure(log=print):
    from .manifest import load_all
    from .onboard import onboard
    if not os.path.isdir(BUILTIN_DIR):
        return
    have = load_all()
    for name in sorted(os.listdir(BUILTIN_DIR)):
        d = os.path.join(BUILTIN_DIR, name)
        if os.path.isdir(d) and (name not in have or have[name].verification.status != "verified"):
            try:
                onboard(d, name, use_llm=False, log=log)
            except Exception as e:  # noqa
                log(f"[builtin] {name}: {e}")
