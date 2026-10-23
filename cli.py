"""NodeForge command line.

  python cli.py onboard <tool folder | script | exe> [--name ID] [--no-llm]
  python cli.py doctor                       # which runtimes (python/node/bash/gcc/...) work here
  python cli.py samples                      # onboard every tool in sample_tools/
  python cli.py crawl <folder | git url> [--run]   # find tools + wrappable functions; --run onboards them and writes skills
  python cli.py wrap <file.py> [function]           # turn a library function into a node (no function: list candidates)
  python cli.py skills [--check] [--zip out.zip]    # write SKILL.md for every verified node
  python cli.py list
  python cli.py plan "3d bom affiliate listing"
  python cli.py run  "3d bom affiliate listing" sample_inputs/bracket.step sample_inputs/bom.csv
  python cli.py serve [--port 8800]
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from nodeforge import executor, graph  # noqa: E402
from nodeforge.manifest import load_all  # noqa: E402
from nodeforge.onboard import onboard  # noqa: E402
from nodeforge.types import compatible, mime_from_name  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="NodeForge: turn any internal tool into a pipeline node")
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("onboard"); o.add_argument("path"); o.add_argument("--name"); o.add_argument("--no-llm", action="store_true")
    sub.add_parser("samples")
    sub.add_parser("doctor", help="show which runtimes this machine can run tools with")
    sub.add_parser("list")
    c = sub.add_parser("crawl"); c.add_argument("source"); c.add_argument("--run", action="store_true"); c.add_argument("--no-llm", action="store_true")
    w = sub.add_parser("wrap"); w.add_argument("file"); w.add_argument("function", nargs="?"); w.add_argument("--name")
    k = sub.add_parser("skills"); k.add_argument("--check", action="store_true"); k.add_argument("--zip")
    p = sub.add_parser("plan"); p.add_argument("request")
    r = sub.add_parser("run"); r.add_argument("request"); r.add_argument("files", nargs="+")
    r.add_argument("-p", "--param", action="append", default=[], help="name=value, applied to any node with that param")
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8800); s.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()

    if a.cmd == "onboard":
        onboard(a.path, a.name, use_llm=not a.no_llm)
    elif a.cmd == "doctor":
        from nodeforge import interp
        for k, v in interp.report().items():
            print(f"  {k:11} {v}")
        for k in ("bash", "cc"):
            if interp.resolve(k)[1]:
                print(f"\n  {k}: {interp.resolve(k)[1]}")
    elif a.cmd == "samples":
        base = os.path.join(ROOT, "sample_tools")
        for t in sorted(os.listdir(base)):
            onboard(os.path.join(base, t)); print()
    elif a.cmd == "list":
        for m in load_all().values():
            print(f"{m.id:28} {m.verification.status:10} "
                  f"{' + '.join(p.mime for p in m.file_inputs()) or '-'}  ->  {', '.join(p.mime for p in m.outputs)}")
    elif a.cmd == "crawl":
        from nodeforge import crawl
        r = crawl.crawl(a.source, onboard_units=a.run, use_llm=not a.no_llm)
        print(f"\nreport: crawled/{r['name']}/REPORT.md" + ("" if a.run else "   (add --run to onboard + wrap + write skills)"))
    elif a.cmd == "wrap":
        from nodeforge import skills, wrap
        if not a.function:
            for f in wrap.discover(a.file):
                print(f"  {f['function']:28} score {f['score']:2}  " + ("; ".join(f["problems"]) or "ok"))
        else:
            m = wrap.wrap(a.file, a.function, a.name, use_llm=False)
            skills.generate_all(polish=False)
            print(f"{m.id}: {m.verification.status}")
    elif a.cmd == "skills":
        from nodeforge import llm, skills
        skills.generate_all(polish=llm.available())
        if a.check:
            r = skills.activation_check()
            print(f"activation: right node {r['hit']}/{r['of']}, another valid node for the same type {r['alternative']}, "
                  f"genuine misses {r['of'] - r['hit'] - r['alternative']}")
            for nid, v in r["by_node"].items():
                for c in v["cases"]:
                    if c["verdict"] == "miss":
                        print(f"  miss  {nid}: \"{c['request']}\" -> {c['plan']}")
        if a.zip:
            open(a.zip, "wb").write(skills.bundle()); print("wrote", a.zip)
    elif a.cmd == "plan":
        g = graph.plan(a.request)
        print(json.dumps(g, indent=1)); print("validation:", graph.validate(g) or "OK")
    elif a.cmd == "run":
        g = graph.plan(a.request, a.files)
        errs = graph.validate(g)
        if errs:
            sys.exit("plan invalid: " + "; ".join(errs))
        ups, pool = {}, list(a.files)
        for n in g["nodes"]:
            if n["kind"] != "input":
                continue
            f = next((f for f in pool if compatible(mime_from_name(f) or "", n["mime"])), None)
            if not f:
                sys.exit(f"no file given for input '{n.get('label')}' ({n['mime']})")
            pool.remove(f); ups[n["id"]] = f
        kv = dict(x.split("=", 1) for x in a.param)
        for n in g["nodes"]:
            if n["kind"] == "tool":
                n["params"] = {k: kv.get(k, v) for k, v in n.get("params", {}).items()}
        rid, d = executor.new_run_dir()
        res = executor.execute(g, ups, d, on_event=lambda e: e.get("status") != "running" and
                               print(f"  {e['node']:6} {e['status']:7} {', '.join(e.get('errors') or [])}"))
        print("\nOutputs:")
        for n in g["nodes"]:
            if n["kind"] == "output" and res["nodes"][n["id"]]["ok"]:
                print(f"  {n['label']}: {res['nodes'][n['id']]['outputs']['file']}")
    elif a.cmd == "serve":
        import uvicorn
        from server import app
        uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
