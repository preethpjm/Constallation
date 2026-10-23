"""Running untrusted internal tools: timeouts, process-group kill, captured output."""
import os
import shutil
import signal
import subprocess
import tempfile

WORK_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runs")
IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv", ".idea", ".vscode")


KEEP_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
            # Windows programs break without these
            "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "USERNAME",
            "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
            "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "HOMEDRIVE", "HOMEPATH"}


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the tool AND everything it started (shell=True wraps it in sh/cmd.exe)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa
        try:
            proc.kill()
        except Exception:  # noqa
            pass


def popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def rmtree(path: str) -> None:
    """shutil.rmtree that also removes read-only files (git objects, venvs on Windows)."""
    import stat

    def onerr(func, p, _):
        try:
            os.chmod(p, stat.S_IWRITE); func(p)
        except Exception:  # noqa
            pass
    shutil.rmtree(path, onerror=onerr)


def run(cmd: str, cwd: str, timeout: int = 60, env: dict | None = None) -> dict:
    e = {k: v for k, v in os.environ.items() if k.upper() in KEEP_ENV}
    e["PYTHONUNBUFFERED"] = "1"
    e.update(env or {})
    try:
        p = subprocess.Popen(cmd, shell=True, cwd=cwd, env=e, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace", **popen_kwargs())
        try:
            out, _ = p.communicate(timeout=timeout)
            return {"code": p.returncode, "output": out[-8000:], "timeout": False}
        except subprocess.TimeoutExpired:
            kill_tree(p)
            out, _ = p.communicate()
            return {"code": -9, "output": (out or "")[-8000:], "timeout": True}
    except Exception as ex:  # noqa
        return {"code": -1, "output": f"failed to start: {ex}", "timeout": False}


def fresh_copy(tool_dir: str, label: str) -> str:
    """A throwaway copy of the tool, so probing never touches the original."""
    os.makedirs(WORK_ROOT, exist_ok=True)
    d = tempfile.mkdtemp(prefix=f"probe-{label}-", dir=WORK_ROOT)
    dst = os.path.join(d, "tool")
    if os.path.isdir(tool_dir):
        shutil.copytree(tool_dir, dst, ignore=IGNORE, symlinks=True)
    else:  # a single file (e.g. a lone .exe or script)
        os.makedirs(dst)
        shutil.copy2(tool_dir, dst)
    return dst


def snapshot(root: str) -> dict:
    s = {}
    for cur, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in ("node_modules", ".git", "__pycache__")]
        for f in files:
            p = os.path.join(cur, f)
            try:
                st = os.stat(p)
                s[p] = (st.st_size, st.st_mtime_ns)
            except OSError:
                pass
    return s
