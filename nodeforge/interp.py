"""Finds a WORKING runtime for each kind of tool, on this machine.

"bash is on PATH" is not enough: on Windows, bash.exe is often the WSL stub that
only prints an error when no Linux distro is installed. Every runtime is
smoke-tested before the analyzer trusts anything it prints.
"""
import os
import shutil
import sys
import tempfile
from functools import lru_cache

from . import sandbox

WIN = os.name == "nt"

HINTS = {
    "bash": ("needs bash. Install Git for Windows (Git Bash is auto-detected) or a WSL distro "
             "(`wsl --install -d Ubuntu`), then onboard again." if WIN else "needs bash (apt install bash)."),
    "node": "needs Node.js on PATH (https://nodejs.org).",
    "cc": ("needs a C/C++ compiler. On Windows install MinGW-w64 gcc (e.g. via MSYS2) and add it to PATH, "
           "or run NodeForge inside WSL." if WIN else "needs gcc or clang (apt install build-essential)."),
    "powershell": "needs PowerShell (pwsh).",
    "java": "needs a Java runtime on PATH.",
    "go": "needs Go on PATH.",
    "cargo": "needs Rust/cargo on PATH.",
    "wsl": "is a Linux binary; on Windows it needs a WSL distro (`wsl --install -d Ubuntu`).",
    "wine": "is a Windows .exe; on Linux it needs wine (or onboard it on a Windows worker).",
    "cmd": "is a Windows batch file; onboard it on a Windows machine/worker.",
}


def q(path: str) -> str:
    """Quote an executable path for the shell (cmd.exe needs backslashes in program paths)."""
    return f'"{path}"' if " " in path else path


def _works(cmd: str, expect: str = "", timeout: int = 20) -> bool:
    d = tempfile.mkdtemp(prefix="nf-smoke-")
    try:
        r = sandbox.run(cmd, d, timeout=timeout)
        return r["code"] == 0 and (expect in r["output"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _bash_candidates():
    c = []
    if WIN:
        git = shutil.which("git")
        if git:  # ...\Git\cmd\git.exe -> ...\Git\bin\bash.exe
            root = os.path.dirname(os.path.dirname(git))
            c += [os.path.join(root, "bin", "bash.exe"), os.path.join(root, "usr", "bin", "bash.exe")]
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            if base:
                c.append(os.path.join(base, "Git", "bin", "bash.exe"))
        c.append(r"C:\msys64\usr\bin\bash.exe")
    w = shutil.which("bash")
    if w:
        c.append(w)  # last on Windows: probably the WSL stub
    seen, out = set(), []
    for p in c:
        if p and os.path.exists(p) and p.lower() not in seen:
            seen.add(p.lower()); out.append(p)
    return out


@lru_cache(maxsize=None)
def resolve(kind: str) -> tuple[str | None, str | None]:
    """-> (shell command prefix, problem). Exactly one of them is None."""
    if kind == "python":
        return q(sys.executable), None
    if kind == "bash":
        for b in _bash_candidates():
            if _works(f'{q(b)} -c "echo nodeforge_ok"', "nodeforge_ok"):
                return q(b), None
        return None, HINTS["bash"]
    if kind == "node":
        n = shutil.which("node")
        return (q(n), None) if n and _works(f"{q(n)} --version", "v") else (None, HINTS["node"])
    if kind == "cc":
        for c in ("gcc", "clang", "cc"):
            p = shutil.which(c)
            if p and _works(f"{q(p)} --version"):
                return q(p), None
        return None, HINTS["cc"]
    if kind == "make":
        for m in ("make", "mingw32-make", "gmake"):
            p = shutil.which(m)
            if p and _works(f"{q(p)} --version"):
                return q(p), None
        return None, "no make found (will compile directly instead)"
    if kind == "powershell":
        for c in ("pwsh", "powershell"):
            p = shutil.which(c)
            if p:
                return q(p) + " -NoProfile -ExecutionPolicy Bypass -File", None
        return None, HINTS["powershell"]
    if kind == "cmd":
        return ("cmd /c", None) if WIN else (None, HINTS["cmd"])
    if kind == "java":
        p = shutil.which("java")
        return (q(p) + " -jar", None) if p else (None, HINTS["java"])
    if kind == "go":
        p = shutil.which("go")
        return (q(p) + " run .", None) if p else (None, HINTS["go"])
    if kind == "cargo":
        p = shutil.which("cargo")
        return (q(p) + " run -q --", None) if p else (None, HINTS["cargo"])
    if kind == "wsl":
        p = shutil.which("wsl")
        return ("wsl -e", None) if p and _works("wsl -e echo nodeforge_ok", "nodeforge_ok") else (None, HINTS["wsl"])
    if kind == "wine":
        p = shutil.which("wine")
        return ("wine", None) if p else (None, HINTS["wine"])
    return None, f"unknown runtime {kind}"


def exe_path(rel: str) -> str:
    """How to invoke a binary that lives inside the tool folder."""
    if WIN:
        return q(rel.replace("/", "\\"))
    return "./" + rel if not rel.startswith(("/", "./")) else rel


def report() -> dict:
    return {k: (resolve(k)[0] or "MISSING") for k in ("python", "node", "bash", "cc", "make", "powershell", "java")}
