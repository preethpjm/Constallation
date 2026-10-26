@echo off
rem One-command launcher for Windows: creates a virtual environment on first run, installs dependencies, starts Constellation.
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -c "import sys; sys.exit(sys.version_info < (3,10))" || (echo Python 3.10 or newer is required & pause & exit /b 1)
if not exist .venv (
  echo First run: creating .venv ...
  %PY% -m venv .venv || (pause & exit /b 1)
)
call .venv\Scripts\activate.bat
if not exist .venv\.installed (
  echo Installing dependencies ...
  pip install -q -r requirements.txt || (pause & exit /b 1)
  echo ok> .venv\.installed
)
python cli.py serve --open %*
pause
