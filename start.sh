#!/usr/bin/env bash
# One-command launcher: creates a virtual environment on first run, installs dependencies, starts Constellation.
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "Python 3.10+ is required (set PYTHON=/path/to/python)"; exit 1; }
"$PY" -c 'import sys; sys.exit(sys.version_info < (3,10))' || { echo "Python 3.10 or newer is required"; exit 1; }
if [ ! -d .venv ]; then echo "First run: creating .venv ..."; "$PY" -m venv .venv; fi
. .venv/bin/activate
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Installing dependencies ..."; pip install -q -r requirements.txt && touch .venv/.installed
fi
exec python cli.py serve --open "$@"
