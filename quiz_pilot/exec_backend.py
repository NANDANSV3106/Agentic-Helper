"""Execution backends for external programming assignments.

Many users have no WSL, so external-assignment commands must run
wherever the toolchain exists:

- "wsl": run inside WSL Ubuntu via quiz_pilot.wsl_bridge. Preferred
  when WSL is present: the C++ starter code targets Linux sockets
  and Mininet needs Linux network namespaces.
- "native": run directly on Windows via Git Bash when present
  (Linux-style commands work there), else PowerShell. A MinGW/MSYS2
  g++ on PATH compiles the C++ assignments; POSIX socket code may
  still need porting, and the agent fixes compile errors from the
  reported output like any other failure.
- "auto" (default): WSL when wsl.exe exists, else native.

Honest limits, enforced here rather than papered over:
- Mininet / Open vSwitch commands can NEVER run natively on
  Windows: they need Linux network namespaces. On a WSL-less
  machine they fail with a clear error naming WSL or a Linux VM
  instead of a confusing "command not found".
- as_root only means something under WSL (wsl -u root); under the
  native backend it is refused with a clear error instead of being
  silently ignored.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from quiz_pilot import toolchain
from quiz_pilot.wsl_bridge import OUTPUT_CAP, run_in_wsl

# First tokens that can only ever run on Linux.
_LINUX_ONLY_COMMANDS = ("mn", "ovs-vsctl", "ovs-vswitchd", "ovs-ofctl", "ovsdb-tool")

# First tokens that need a C++ toolchain: when none is found, the
# native backend bootstraps the portable one (see toolchain.py).
_COMPILER_COMMANDS = ("g++", "gcc", "c++", "make", "mingw32-make", "cmake", "gdb")


def _cap(text: str) -> str:
    if len(text) <= OUTPUT_CAP:
        return text
    return f"[output truncated, showing last {OUTPUT_CAP} chars]\n" + text[-OUTPUT_CAP:]


def detect_environment(_which: Callable[[str], Optional[str]] = shutil.which) -> Dict[str, Any]:
    """Report which execution pieces exist on this machine."""
    return {
        "wsl": _which("wsl.exe") is not None,
        "gpp": _which("g++"),
        "bash": _which("bash"),
        "powershell": _which("powershell"),
    }


def _first_token(command: str) -> str:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.strip().split()
    while tokens and tokens[0] in ("sudo", "env"):
        tokens = tokens[1:]
    return tokens[0] if tokens else ""


def run_native(
    command: str,
    workdir: Optional[str] = None,
    timeout_sec: int = 300,
    _runner: Optional[Callable[..., Any]] = None,
    _which: Callable[[str], Optional[str]] = shutil.which,
    extra_path_dirs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run `command` directly on Windows: Git Bash if present, else PowerShell.

    `extra_path_dirs` are prepended to PATH for the child only, so a
    project-local toolchain resolves without touching system PATH."""
    if not command or not command.strip():
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "",
                "error": "empty command"}
    bash = _which("bash")
    if bash:
        argv: List[str] = [bash, "-lc", command]
        shell_name = "bash"
    else:
        ps = _which("powershell") or "powershell"
        argv = [ps, "-NoProfile", "-Command", command]
        shell_name = "powershell"
    runner = _runner or subprocess.run
    env = None
    if extra_path_dirs:
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([*extra_path_dirs, env.get("PATH", "")])
    try:
        res = runner(argv, capture_output=True, text=True, timeout=timeout_sec,
                     cwd=workdir, env=env)
    except FileNotFoundError:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "",
                "error": f"{shell_name} not found - no native shell available"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "",
                "error": f"command timed out after {timeout_sec}s"}
    except OSError as e:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "", "error": str(e)}
    return {
        "ok": res.returncode == 0,
        "exit_code": res.returncode,
        "stdout": _cap(res.stdout or ""),
        "stderr": _cap(res.stderr or ""),
    }


def run_command(
    command: str,
    workdir: Optional[str] = None,
    backend: str = "auto",
    timeout_sec: int = 300,
    distro: str = "Ubuntu",
    as_root: bool = False,
    setup_toolchain: bool = True,
    _runner: Optional[Callable[..., Any]] = None,
    _which: Callable[[str], Optional[str]] = shutil.which,
    _tools_dir: Optional[Path] = None,
    _bootstrap: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run `command` on the best available backend. Never raises for
    expected failures: those come back as {"ok": False, ...} with a
    plain-English error and the backend that was (or would have been)
    used. When a compiler command runs on native and no compiler
    exists, setup_toolchain=True (default) downloads the portable
    MinGW toolchain into .tools/ first; False fails honestly."""
    if backend not in ("auto", "wsl", "native"):
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "",
                "backend": None, "error": f"unknown backend: {backend!r}"}
    env = detect_environment(_which)
    resolved = backend
    if backend == "auto":
        resolved = "wsl" if env["wsl"] else "native"
    if not command or not command.strip():
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "",
                "backend": resolved, "error": "empty command"}
    if resolved == "native" and as_root:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "",
                "backend": "native",
                "error": "as_root requires the wsl backend - no WSL on this machine"}
    if resolved == "native" and _first_token(command) in _LINUX_ONLY_COMMANDS:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "",
                "backend": "native",
                "error": ("Mininet / Open vSwitch commands need Linux and cannot run "
                          "natively on Windows. Install WSL2 + Ubuntu or use a Linux VM.")}
    if resolved == "wsl":
        result = run_in_wsl(command, workdir=workdir, timeout_sec=timeout_sec,
                            distro=distro, as_root=as_root, _runner=_runner)
        result["backend"] = "wsl"
        return result
    extra_path_dirs = None
    toolchain_note = None
    if _first_token(command) in _COMPILER_COMMANDS:
        status = toolchain.compiler_status(_which=_which, tools_dir=_tools_dir)
        if status["source"] == "local":
            extra_path_dirs = [str(Path(status["path"]).parent)]
            toolchain_note = "local"
        elif status["source"] == "none":
            if not setup_toolchain:
                return {"ok": False, "exit_code": None, "stdout": "", "stderr": "",
                        "backend": "native",
                        "error": ("No C++ compiler found on this machine (no g++ on PATH, "
                                  "no local toolchain). Re-run with setup_toolchain=true to "
                                  "download the portable MinGW toolchain, or install MinGW/MSYS2, "
                                  "or use backend=wsl.")}
            boot = _bootstrap or toolchain.bootstrap_toolchain
            res = boot(tools_dir=_tools_dir)
            if not res.get("ok"):
                return {"ok": False, "exit_code": None, "stdout": "", "stderr": "",
                        "backend": "native",
                        "error": res.get("error", "toolchain setup failed")}
            extra_path_dirs = [res["bin_dir"]]
            toolchain_note = "downloaded"
        else:
            toolchain_note = "path"
    result = run_native(command, workdir=workdir, timeout_sec=timeout_sec,
                        _runner=_runner, _which=_which, extra_path_dirs=extra_path_dirs)
    result["backend"] = "native"
    if toolchain_note:
        result["toolchain"] = toolchain_note
    return result
