"""Execution backends (2026-11-13): external assignments must work
without WSL, because many users will not have it.

- backend auto picks WSL when wsl.exe exists, else the native
  Windows toolchain (Git Bash preferred, PowerShell fallback).
- C++ compile/test commands run on either backend; the agent fixes
  failures from the reported output.
- Mininet / Open vSwitch commands are honestly refused on the native
  backend: they need Linux and cannot run on Windows.
- as_root is refused on the native backend instead of silently
  ignored: it only means something under WSL.

All subprocess interaction goes through injected fakes: nothing here
executes a real shell.
"""
import os
import subprocess

from quiz_pilot import exec_backend


class FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_which(mapping):
    def which(name):
        return mapping.get(name)
    return which


class Recorder:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self._result = result or FakeCompleted(0, stdout="ok\n")
        self._exc = exc

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._result


# ---------- detection ----------

def test_detect_environment_reports_what_exists():
    env = exec_backend.detect_environment(make_which({
        "wsl.exe": r"C:\Program Files\WSL\wsl.exe", "g++": r"C:\msys64\usr\bin\g++.exe",
        "bash": r"C:\Program Files\Git\bin\bash.exe", "powershell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    }))
    assert env["wsl"] is True
    assert env["gpp"] is not None and env["bash"] is not None

    env = exec_backend.detect_environment(make_which({}))
    assert env == {"wsl": False, "gpp": None, "bash": None, "powershell": None}


# ---------- native runner ----------

def test_run_native_prefers_bash_and_uses_cwd():
    rec = Recorder()
    res = exec_backend.run_native(
        "g++ -o app main.cpp && ./app", workdir=r"C:\mp1",
        _runner=rec, _which=make_which({"bash": "bash"}))
    assert res["ok"] is True
    argv, kwargs = rec.calls[0]
    assert argv == ["bash", "-lc", "g++ -o app main.cpp && ./app"]
    assert kwargs["cwd"] == r"C:\mp1"


def test_run_native_falls_back_to_powershell():
    rec = Recorder()
    res = exec_backend.run_native("g++ --version", _runner=rec,
                                  _which=make_which({"powershell": "powershell"}))
    assert res["ok"] is True
    assert rec.calls[0][0][:3] == ["powershell", "-NoProfile", "-Command"]


def test_run_native_failures_are_error_dicts():
    res = exec_backend.run_native("  ", _runner=Recorder())
    assert res["ok"] is False and res["error"] == "empty command"

    def missing(*a, **k):
        raise FileNotFoundError("bash")

    res = exec_backend.run_native("make", _runner=missing, _which=make_which({"bash": "bash"}))
    assert res["ok"] is False and "bash" in res["error"]

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="bash", timeout=30)

    res = exec_backend.run_native("make", timeout_sec=30, _runner=slow,
                                  _which=make_which({"bash": "bash"}))
    assert res["ok"] is False and "timed out after 30s" in res["error"]

    rec = Recorder(FakeCompleted(1, stderr="g++: error: foo.h: No such file\n"))
    res = exec_backend.run_native("make", _runner=rec, _which=make_which({"bash": "bash"}))
    assert res["ok"] is False and res["exit_code"] == 1
    assert "foo.h" in res["stderr"]


# ---------- dispatch ----------

def test_auto_prefers_wsl_when_present():
    rec = Recorder()
    res = exec_backend.run_command(
        "make", _runner=rec,
        _which=make_which({"wsl.exe": "wsl.exe", "bash": "bash"}))
    assert res["ok"] is True and res["backend"] == "wsl"
    assert rec.calls[0][0][:3] == ["wsl.exe", "-d", "Ubuntu"]


def test_auto_falls_back_to_native_without_wsl():
    rec = Recorder()
    res = exec_backend.run_command(
        "make", _runner=rec, _which=make_which({"bash": "bash", "g++": "g++"}))
    assert res["ok"] is True and res["backend"] == "native"
    assert rec.calls[0][0][0] == "bash"


def test_explicit_backend_is_respected():
    rec = Recorder()
    res = exec_backend.run_command(
        "make", backend="native", _runner=rec,
        _which=make_which({"wsl.exe": "wsl.exe", "bash": "bash", "g++": "g++"}))
    assert res["backend"] == "native"
    res = exec_backend.run_command(
        "make", backend="wsl", _runner=rec,
        _which=make_which({"bash": "bash"}))
    assert res["backend"] == "wsl"
    assert rec.calls[-1][0][0] == "wsl.exe"


def test_unknown_backend_is_an_error():
    res = exec_backend.run_command("make", backend="docker")
    assert res["ok"] is False and "unknown backend" in res["error"]


def test_as_root_refused_on_native_backend():
    res = exec_backend.run_command(
        "service openvswitch-switch start", as_root=True,
        _which=make_which({"bash": "bash"}))
    assert res["ok"] is False and res["backend"] == "native"
    assert "as_root requires the wsl backend" in res["error"]


def test_mininet_commands_refused_natively_but_allowed_under_wsl():
    for cmd in ("mn --test pingall", "sudo mn --custom topo.py", "ovs-vsctl show"):
        res = exec_backend.run_command(cmd, _which=make_which({"bash": "bash"}))
        assert res["ok"] is False and res["backend"] == "native"
        assert "WSL2 + Ubuntu or use a Linux VM" in res["error"]
        assert "cannot run natively on Windows" in res["error"]

    rec = Recorder()
    res = exec_backend.run_command(
        "mn --test pingall", _runner=rec,
        _which=make_which({"wsl.exe": "wsl.exe"}))
    assert res["ok"] is True and res["backend"] == "wsl"


def test_non_mininet_commands_pass_the_guard_natively():
    rec = Recorder()
    res = exec_backend.run_command(
        "g++ -o app main.cpp", _runner=rec,
        _which=make_which({"bash": "bash", "g++": "g++"}))
    assert res["ok"] is True and res["backend"] == "native"


# ---------- portable toolchain bootstrap ----------

def _no_compiler_which():
    return make_which({"bash": "bash"})


def test_native_compiler_command_bootstraps_when_no_compiler(tmp_path):
    bin_dir = tmp_path / "mingw64" / "bin"
    seen = {}

    def fake_bootstrap(tools_dir=None):
        seen["tools_dir"] = tools_dir
        return {"ok": True, "bin_dir": str(bin_dir),
                "compiler": str(bin_dir / "g++.exe"), "already_present": False}

    rec = Recorder()
    res = exec_backend.run_command(
        "g++ --version", _runner=rec, _which=_no_compiler_which(),
        _tools_dir=tmp_path, _bootstrap=fake_bootstrap)
    assert res["ok"] is True and res["backend"] == "native"
    assert res["toolchain"] == "downloaded"
    assert seen["tools_dir"] == tmp_path
    env = rec.calls[0][1]["env"]
    assert env["PATH"].startswith(str(bin_dir) + os.pathsep)


def test_native_compiler_command_without_setup_toolchain_fails_honestly(tmp_path):
    def no_bootstrap(*a, **k):
        raise AssertionError("must not bootstrap")

    rec = Recorder()
    res = exec_backend.run_command(
        "g++ --version", setup_toolchain=False, _runner=rec,
        _which=_no_compiler_which(), _tools_dir=tmp_path, _bootstrap=no_bootstrap)
    assert res["ok"] is False and res["backend"] == "native"
    assert "No C++ compiler found" in res["error"]
    assert rec.calls == []


def test_bootstrap_failure_is_an_error_dict(tmp_path):
    def failing_bootstrap(tools_dir=None):
        return {"ok": False, "error": "toolchain download failed: network down"}

    rec = Recorder()
    res = exec_backend.run_command(
        "make", _runner=rec, _which=_no_compiler_which(),
        _tools_dir=tmp_path, _bootstrap=failing_bootstrap)
    assert res["ok"] is False and "network down" in res["error"]
    assert rec.calls == []


def test_native_compiler_command_uses_local_toolchain(tmp_path):
    bin_dir = tmp_path / "mingw64" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "g++.exe").write_bytes(b"fake")

    def no_bootstrap(*a, **k):
        raise AssertionError("must not bootstrap")

    rec = Recorder()
    res = exec_backend.run_command(
        "make", _runner=rec, _which=_no_compiler_which(),
        _tools_dir=tmp_path, _bootstrap=no_bootstrap)
    assert res["ok"] is True and res["toolchain"] == "local"
    env = rec.calls[0][1]["env"]
    assert env["PATH"].startswith(str(bin_dir) + os.pathsep)


def test_native_compiler_command_uses_path_compiler_without_bootstrap():
    def no_bootstrap(*a, **k):
        raise AssertionError("must not bootstrap")

    rec = Recorder()
    res = exec_backend.run_command(
        "g++ --version", _runner=rec,
        _which=make_which({"bash": "bash", "g++": r"C:\gcc\bin\g++.exe"}),
        _bootstrap=no_bootstrap)
    assert res["ok"] is True and res["toolchain"] == "path"
    assert rec.calls[0][1]["env"] is None


def test_non_compiler_command_never_bootstraps(tmp_path):
    def no_bootstrap(*a, **k):
        raise AssertionError("must not bootstrap")

    rec = Recorder()
    res = exec_backend.run_command(
        "python check.py", _runner=rec, _which=_no_compiler_which(),
        _tools_dir=tmp_path, _bootstrap=no_bootstrap)
    assert res["ok"] is True and "toolchain" not in res


def test_wsl_backend_never_bootstraps(tmp_path):
    def no_bootstrap(*a, **k):
        raise AssertionError("must not bootstrap")

    rec = Recorder()
    res = exec_backend.run_command(
        "g++ --version", _runner=rec, _which=make_which({"wsl.exe": "wsl.exe"}),
        _tools_dir=tmp_path, _bootstrap=no_bootstrap)
    assert res["ok"] is True and res["backend"] == "wsl"
    assert "toolchain" not in res
