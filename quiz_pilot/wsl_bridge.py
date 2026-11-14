"""Run shell commands inside WSL Ubuntu from Windows-side Python.

External programming assignments (C++ CLI submissions, Mininet
topologies) cannot run on native Windows: the starter code targets
Linux sockets and Mininet needs Linux network namespaces. The host
agent runs on Windows, so this bridge executes the agent's commands
inside the Ubuntu distribution over `wsl.exe`.

Design notes:
- Files live on the Windows side (the project folder); WSL sees them
  under /mnt/<drive>/..., so no copying is needed: paths are
  translated, not synced.
- `bash -lc` runs the command so pipes, redirects and `&&` chains
  work exactly as the agent wrote them.
- Output is capped (tail-kept) so a noisy compiler cannot flood the
  host's context; the exit code is always reported honestly.
- Nothing here ever prompts for a password: interactive password
  prompts inside this user's WSL console have been observed to
  receive empty input. Service/admin commands run via as_root=True
  (`wsl -u root`), which needs no password.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from typing import Any, Callable, Dict, List, Optional

OUTPUT_CAP = 6000  # chars kept (tail) per stream

_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def windows_to_wsl_path(path: str) -> str:
    """Translate a Windows path to its WSL /mnt/<drive>/ form.

    Already-WSL paths (starting with '/'), relative paths and UNC
    paths are returned unchanged: relative paths resolve against the
    command's working directory inside WSL, and UNC paths have no
    /mnt mapping.
    """
    if not path:
        return path
    if path.startswith("/"):
        return path
    m = _DRIVE_RE.match(path)
    if not m:
        return path
    drive, rest = m.group(1).lower(), m.group(2)
    return "/mnt/" + drive + "/" + rest.replace("\\", "/")


def build_wsl_command(
    command: str,
    workdir: Optional[str] = None,
    distro: str = "Ubuntu",
    as_root: bool = False,
) -> List[str]:
    """Build the `wsl.exe` argv for running `command` inside WSL."""
    if not command or not command.strip():
        raise ValueError("empty command")
    shell = command
    if workdir:
        shell = f"cd {shlex.quote(windows_to_wsl_path(workdir))} && {command}"
    argv = ["wsl.exe", "-d", distro]
    if as_root:
        argv += ["-u", "root"]
    argv += ["--", "bash", "-lc", shell]
    return argv


def _cap(text: str) -> str:
    if len(text) <= OUTPUT_CAP:
        return text
    return f"[output truncated, showing last {OUTPUT_CAP} chars]\n" + text[-OUTPUT_CAP:]


def run_in_wsl(
    command: str,
    workdir: Optional[str] = None,
    timeout_sec: int = 300,
    distro: str = "Ubuntu",
    as_root: bool = False,
    _runner: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Run `command` inside WSL and return an honest result dict.

    Never raises for expected failures (missing wsl.exe, timeout,
    nonzero exit): those come back as {"ok": False, ...} so the host
    agent can report them instead of crashing the tool call.
    """
    try:
        argv = build_wsl_command(command, workdir=workdir, distro=distro, as_root=as_root)
    except ValueError as e:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "", "error": str(e)}
    runner = _runner or subprocess.run
    try:
        res = runner(argv, capture_output=True, text=True, timeout=timeout_sec)
    except FileNotFoundError:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "",
                "error": "wsl.exe not found - WSL is not installed on this machine"}
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
