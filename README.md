# Constellation (prototype, formerly NodeForge)

Point it at **any** internal tool — a Python script, a Node CLI, a shell script, a C program with a
Makefile, a compiled binary, a FastAPI service — and it turns the tool into a **verified node**.
Then ask for a pipeline in plain words and it wires the nodes together on a canvas, type-checked,
ready to upload inputs and run.

```
pip install -r requirements.txt
python cli.py samples          # onboard the 5 demo tools (Python, Node, Bash, C, HTTP service)
python cli.py serve            # open http://127.0.0.1:8800  →  "3d bom affiliate listing"  →  Build pipeline
```

Run `python cli.py doctor` first: it shows which runtimes actually work on this machine.

**Windows notes.** Python and Node tools work natively. Bash-script tools need a real bash. Installing
**Git for Windows** gives you Git Bash, which is found automatically even when `bash` on PATH is the WSL
stub. C/C++ tools need **MinGW-w64 gcc** on PATH (e.g. via MSYS2). Without those, those tools are reported as
"this machine can't run it: ..." instead of being guessed at, and the planner tells you which step is missing.

> **Run it:** `start.bat` (Windows) or `./start.sh` (Mac/Linux), or `docker compose up -d --build`. See SHIPPING.md.

## How it works

```
 unknown tool ─► PROBE ─► DRAFT ─► VERIFY (real run) ─► [LLM REPAIR if failed] ─► registry/<id>/
                 static     heuristics   on shipped or        agent reads files,       manifest.json  (the contract)
                 + live     build the    synthesized samples  runs commands, resubmits node.md        (knowledge for planner/humans)
                 --help     manifest     outputs discovered   until verification passes app/          (pinned, built snapshot)
                                         empirically
 request ─► PLAN (LLM or heuristic) ─► VALIDATE (code) ─► canvas ─► EXECUTE (per-node backend: local | docker | remote | AI)
```

**Why it doesn't silently break:** nothing is trusted because a model (or a heuristic) said so.
- A node is `verified` only after it was actually run on sample data and produced the declared files,
  with content-sniffed types (a "GLB" must start with `glTF`, JSON must parse, etc.).
- Only verified nodes can be planned. Every edge is type-checked; required inputs must be connected; no cycles.
  The LLM proposes, the validator decides; invalid LLM plans are fed back with the errors, then fall back to heuristics.
- Each node runs from a pinned snapshot (`registry/<id>/app`) with its setup steps done once, so a colleague
  changing the original repo doesn't break pipelines mid-flight. Re-onboard to upgrade.

## Files

| file | what it does |
|---|---|
| `nodeforge/probe.py` | language-agnostic evidence: entry points, argparse AST, `--help`/`-h`/no-arg output, docs, build files, sample data, binaries (ELF/PE/Mach-O/JAR), web services |
| `nodeforge/onboard.py` | draft manifest from evidence → verify by real run → output discovery → LLM repair agent → node.md |
| `nodeforge/manifest.py` | the node contract (ports with MIME types, runtime, verification record) |
| `nodeforge/runner.py` | runs one node: CLI template rendering, HTTP services (start, call endpoint, stop), Docker, remote worker |
| `nodeforge/graph.py` | validator, topo sort, heuristic + LLM planner |
| `nodeforge/executor.py` | runs a graph, routes each node to its backend, AI nodes |
| `nodeforge/llm.py` | Claude, any OpenAI-compatible endpoint (OpenAI, OpenRouter, Ollama, vLLM), or none |
| `nodeforge/decider.py` | decisions: Laya → LLM → heuristic, with confidence and fallbacks |
| `nodeforge/interp.py` | finds runtimes that actually work (Git Bash vs the WSL stub, gcc, node, …) |
| `nodeforge/settings.py` | local settings file behind the UI's AI settings panel |
| `server.py` / `ui/index.html` | API, canvas, and `/worker/run/<node>` so any machine running NodeForge is a remote backend |

## Turning on the AI parts

Everything works without an LLM. With one, failed onboardings get an agent that investigates and fixes them,
the planner understands arbitrary requests, node.md gets "when to use" notes, and AI-step nodes work.

**Easiest:** open the UI → **AI settings** → pick a provider, paste the key → **Save & test**. It makes one tiny
real call and shows the reply, so a wrong key/model/URL shows up immediately. Settings live in
`nodeforge.settings.json` on your machine (git-ignored).

| provider | settings |
|---|---|
| Claude | provider `Anthropic`, key `sk-ant-…`, model e.g. `claude-sonnet-5-5` (or `claude-opus-5-5` for hard repos) |
| OpenAI / OpenRouter | provider `OpenAI-compatible`, key, model, base URL (`https://openrouter.ai/api/v1` for OpenRouter) |
| Local (Ollama) | provider `OpenAI-compatible`, base URL `http://localhost:11434/v1`, model = the name you pulled, key empty |

Environment variables still work and take priority (PowerShell: `$env:ANTHROPIC_API_KEY="sk-ant-..."`, then
`python cli.py serve`).

## Decision engine (Laya)

Some steps are *decisions*, not generation: which of several tools should make an input, which branch a Decision
node takes. Those go to the decision engine, in this order (`auto`):

1. **Laya** (`pip install laya`): a local open decision model (typed choice/score/yes-no answers in one pass,
   calibrated probabilities, CPU works). It downloads its checkpoint on first use.
2. the configured **LLM**, asked for a JSON answer
3. a **heuristic** (word overlap), which never fails

A backend that errors falls through to the next. Decisions below the minimum confidence keep the heuristic's
choice. When the heuristic already has a clear winner, nobody is asked. Every decision is shown in the UI
with who made it.

**Decision node** (`+ Decision`): a question plus options (`name: meaning`, one per line). The input file is
routed unchanged to the chosen option's port; nodes on branches not taken are **skipped**, not failed.
This is the n8n "Switch" node, powered by Laya/LLM.

Note: Laya's out-of-the-box accuracy is modest; its own docs show a large gain from fine-tuning on your
decisions. Start with `auto`, and look at the logged decisions before trusting it for important branches.

## Routing a node somewhere else

In the inspector, set a tool node's **Run on**:
- `local`: subprocess on this server
- `docker`: builds `registry/<id>/app` into an image from the generated Dockerfile and runs it with no network
- `remote`: `python cli.py serve --host 0.0.0.0` on another machine (EC2, GPU box), onboard the tool there,
  paste its URL. Inputs are uploaded, outputs pulled back.
- AI step: a separate node kind that sends its inputs + prompt to the configured LLM.

## Finding tools, wrapping libraries, writing skills

```
python cli.py crawl <folder | git url>          # read-only: lists tools, services, wrappable functions
python cli.py crawl <folder | git url> --run    # then onboards + verifies them (this runs the tools) and writes skills
python cli.py wrap sample_libs/textkit.py csv_to_json
python cli.py skills --check --zip skills.zip
```
- **Crawler** (`nodeforge/crawl.py`): reads a folder or shallow-clones a git URL, reads it without executing anything,
  and sorts what it finds into *onboard* (CLI tool, script, service), *wrap* (pure converter function) or *skip* (with the reason).
- **Wrapper nodes** (`nodeforge/wrap.py`): a Python function with typed parameters and a return value gets a generated argparse
  shim and is onboarded like any tool: verified by a real run, planned and chained with the rest. Text/path parameters become input
  files, the return value becomes an output file (dict/list -> JSON, str -> text). Functions it can't map honestly are reported, not guessed.
- **Skills** (`nodeforge/skills.py`): one `SKILL.md` per verified node (facts from the manifest and the verification run), a
  `constellation/SKILL.md` playbook, and `SKILLS_INDEX.md`. An LLM may reword the description and trigger phrases; it can't change facts.
  `--check` plans sample requests and reports whether the right node (or another valid one) was picked.
- The UI has the same three things under **Add tools** and **Library**.

## Known limits (honest list)

- GUI-only apps and interactive tools (prompts on stdin) can't be wrapped automatically.
- Tools needing licenses, API keys, dongles or a database: onboarding fails with the reason; add env/setup by hand in manifest.json.
- Heuristics understand argparse and `usage:` lines, positional/flag args, outdir-style and output-file-style tools,
  and FastAPI services (via their OpenAPI). Anything stranger needs the LLM agent.
- Without sample data, simple text/CSV inputs get synthetic fixtures; anything else needs a sample or the agent.
- Docker backend is generated but was not exercised in the build environment (no Docker daemon there).
- The Laya integration follows Laya's documented API and was tested against a stand-in; the real model
  could not be downloaded in the build environment.
- Runs are synchronous (fine for a prototype; use a job queue such as Celery/Temporal for production).
- Crawling reads code statically, but `--run` executes what it found (in the onboarding sandbox, not a container). Use a disposable machine for repos you don't trust.
- Wrappers cover Python functions only so far, and only simple parameter types (str/int/float/bool/Path).
- The heuristic planner picks one node per requested type; when several nodes make the same type, a sharper description or the LLM/Laya decision decides.

## Previews, controls and keyboard
- Nodes preview what they hold: GLB/glTF as an orbitable 3D viewer, SVG/PNG/JPG as images, HTML, CSV tables, JSON/text. Anything else (CGM, STEP, binaries) shows a file card; the converter's output is what gets rendered.
- Tool parameters become controls on the node: colour picker, number, slider, toggle, dropdown. A manifest param can set `widget` (`color`, `number`, `bool`, `range:0-1`, `select:a|b|c`, `area`); otherwise it is inferred from the name/default.
- AI nodes pick their own model (list comes from your Ollama/OpenAI-compatible server or Anthropic).
- Keyboard: `Shift+Enter` or `Ctrl+K` opens the command bar (type a request and Enter to build, or search nodes/commands), `Ctrl+B` collapses the side panel, `/` opens the bar, `Ctrl+Enter` runs, `F` fits, `Ctrl+Z` undo.

## Built-in nodes and `constellation.json`
- `builtin_nodes/to-text` ships with Constellation and registers itself at start-up: saves any text-based file (svg, xml, html, json, csv, markdown) as a `.txt`. The planner chains it after other tools (cgm -> svg -> txt).
- A tool author can drop a `constellation.json` next to the code to state what the code cannot show: description, tags and the mime type of any port (`{"inputs": {"port": {"mime": "*/*"}}, "outputs": {"port": {"mime": "text/plain"}}}`). Onboarding applies it and will not overwrite it with observed types.

### Tools that cannot be probed safely (C++ engines, installers, big DLL folders)
Onboarding never starts scripts whose name or content looks like a build/install/download step (build, setup, vcvars, msbuild, winget, pip install, ...). They are listed as "skipped" in the log. If automatic discovery is not enough, describe the node yourself in `constellation.json` with a `command` and nothing is probed:
```json
{"command": "{python} server_cli.py --input {model} --output {glb}",
 "inputs":  {"model": {"mime": "model/step"}},
 "outputs": {"glb": {"mime": "model/gltf-binary", "path": "{outdir}/out.glb"}},
 "example_inputs": {"model": "samples/a.step"}}
```

## Canvas selection and clipboard

- Drag on empty canvas to box-select (Shift/Ctrl adds); Shift/Ctrl-click toggles a node; Ctrl A selects everything.
- Selected nodes move together; arrow keys nudge (Shift = 10 px). Pan with Space+drag, the hand tool or middle mouse.
- Ctrl C / Ctrl X / Ctrl V copy, cut and paste nodes with the connections between them (also across pipelines and browser tabs); Ctrl D duplicates.
- Click a connection to select it; Del removes it (double-click also removes). Del removes the whole selection.
