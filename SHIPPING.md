# Running and shipping Constellation

| Way | For | Command |
|---|---|---|
| Launcher script | a colleague's laptop, no Docker | Windows: double-click `start.bat` · Mac/Linux: `./start.sh` |
| Docker | a shared server, cloud, or a remote worker | `docker compose up -d --build` then open http://localhost:8800 |
| Plain Python | development | `pip install -r requirements.txt && python cli.py serve --open` |

The launchers create `.venv` on first run, install the dependencies, start the server and open the browser.
Extra arguments pass through: `./start.sh --port 8801`.

## Docker notes
- State is kept in mounted folders (`registry`, `runs`, `wrapped`, `crawled`, `data`), so upgrades don't lose nodes or settings.
  AI settings are stored in `data/settings.json` (`NODEFORGE_SETTINGS` overrides the path).
- A local Ollama on the host: set `NODEFORGE_LLM=openai`, `OPENAI_BASE_URL=http://host.docker.internal:11434/v1` in `docker-compose.yml`.
- A second machine as a remote worker: run the same image there and add its URL to a node (Run on -> remote).
- Health check: `GET /api/health`.
- The image is NOT yet built or tested in this prototype's CI (no Docker daemon was available); build it once and report problems.

## Before real users
Add sign-in (Windows/AD via your reverse proxy or OIDC), run it behind HTTPS, and keep `--host 127.0.0.1`
unless a proxy is in front: the API can run onboarded tools and read server paths.
