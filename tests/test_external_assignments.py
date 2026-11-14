"""External programming assignments (2026-11-13, user: "build").

The Cloud Computing specialization's programming assignments are
EXTERNAL, not browser uploads: Gossip Protocol (MP1) and the
Fault-Tolerant Key-Value Store (MP2) are C++ CLI submission-token
assignments, and Course 5's Programming Assignments 2/3/4 are
Mininet topology assignments. The old programming flow
(inspect -> write -> submit_code_file -> poll) only handles
Coursera's in-browser file upload, so these were silently skipped.

This build adds the plumbing, not a guessed submission mechanism:
- specs are classified (mininet / cpp_cli / upload) from their own
  text, and external kinds are flagged wsl_required;
- exec_command runs the agent's commands on the best available
  backend: WSL Ubuntu when present, else the native Windows
  toolchain (Git Bash / MinGW g++), so users without WSL can still
  do the C++ assignments; Mininet stays honestly gated on Linux;
- the orchestrator hands external items to the agent with WSL
  next-steps instead of the upload workflow;
- the instruction files carry an External Assignment Rule.
The submission step itself always follows the spec's own
instructions at runtime - nothing here invents a token format or a
submission URL.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from quiz_pilot import course_orchestrator as co
from quiz_pilot import mcp_server
from quiz_pilot import wsl_bridge
from quiz_pilot.programming_solver import (
    ProgrammingAssignmentManager,
    classify_assignment_kind,
)

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION_FILES = [
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules",
    ".github/copilot-instructions.md",
    ".agents/skills/agentic-helper/SKILL.md",
    ".gemini/skills/agentic-helper/SKILL.md",
]


# ---------- classification ----------

def test_mininet_spec_classifies_mininet():
    kind, markers = classify_assignment_kind(
        "Programming Assignment 2", "Build the topology in Mininet and run the test.",
        "https://www.coursera.org/learn/cloud-networking/programming/xyz", False)
    assert kind == "mininet"
    assert "mininet" in markers


def test_cli_token_spec_without_upload_classifies_cpp_cli():
    kind, markers = classify_assignment_kind(
        "Gossip Protocol", "Compile with g++ and submit using your submission token on the command line.",
        "https://www.coursera.org/learn/cloud-computing/programming/mp1", False)
    assert kind == "cpp_cli"
    assert "submission token" in markers


def test_upload_page_mentioning_local_compile_stays_upload():
    # Guard: merely mentioning local compilation must NOT flip a real
    # upload page to the external flow.
    kind, _ = classify_assignment_kind(
        "Programming Assignment", "Compile your code locally with g++ and upload the zip file.",
        "https://www.coursera.org/learn/x/programming/y", True)
    assert kind == "upload"


def test_plain_page_classifies_upload():
    kind, markers = classify_assignment_kind(
        "Programming Assignment", "Upload your solution file for grading.",
        "https://www.coursera.org/learn/x/programming/y", True)
    assert kind == "upload"
    assert markers == []


def test_is_programming_page_recognises_external_pages():
    assert ProgrammingAssignmentManager.is_programming_page(
        "https://www.coursera.org/learn/x/programming/y",
        "submit with your submission token") is True
    assert ProgrammingAssignmentManager.is_programming_page(
        "https://www.coursera.org/learn/x/programming/y",
        "run the topology in mininet") is True
    assert ProgrammingAssignmentManager.is_programming_page(
        "https://www.coursera.org/learn/x/quiz/y",
        "choose the correct answer") is False


class FakeSpecPage:
    """Stands in for a Playwright page for extract_task_spec: its
    evaluate() returns the canned JS-result dict directly."""

    def __init__(self, url, js_result):
        self.url = url
        self._js_result = js_result

    async def title(self):
        return "Gossip Protocol"

    async def evaluate(self, script):
        return dict(self._js_result)


def test_extract_task_spec_flags_external_kind_and_wsl():
    page = FakeSpecPage("https://www.coursera.org/learn/cloud-computing/programming/mp1", {
        "title": "Gossip Protocol", "task_type": "file_upload",
        "has_file_input": False, "accepted_types": [], "starter_files": [],
        "text_inputs_count": 0, "lab_launch_url": None, "latest_score": None,
        "is_passed": False,
        "instructions_snippet": "Compile with g++ and submit with your submission token.",
    })
    pm = ProgrammingAssignmentManager()
    spec = asyncio.run(pm.extract_task_spec(page=page))
    assert spec["kind"] == "cpp_cli"
    assert spec["wsl_required"] is True
    assert spec["task_type"] == "file_upload"  # existing field untouched
    assert spec["url"].endswith("/mp1")


# ---------- wsl_bridge ----------

def test_windows_to_wsl_path():
    assert wsl_bridge.windows_to_wsl_path(
        r"C:\Users\nandu\Downloads\Agentic-Helper") == "/mnt/c/Users/nandu/Downloads/Agentic-Helper"
    assert wsl_bridge.windows_to_wsl_path("D:/data/mp1") == "/mnt/d/data/mp1"
    assert wsl_bridge.windows_to_wsl_path("/mnt/c/data") == "/mnt/c/data"
    assert wsl_bridge.windows_to_wsl_path("/home/nandansv1356/mp1") == "/home/nandansv1356/mp1"
    assert wsl_bridge.windows_to_wsl_path("relative/dir") == "relative/dir"
    assert wsl_bridge.windows_to_wsl_path("") == ""


def test_build_wsl_command_shape():
    argv = wsl_bridge.build_wsl_command("g++ -o app main.cpp")
    assert argv == ["wsl.exe", "-d", "Ubuntu", "--", "bash", "-lc", "g++ -o app main.cpp"]
    argv = wsl_bridge.build_wsl_command("service openvswitch-switch start", as_root=True)
    assert argv[3:5] == ["-u", "root"]
    argv = wsl_bridge.build_wsl_command("make", workdir=r"C:\Users\nandu\mp1")
    assert argv[-1] == "cd /mnt/c/Users/nandu/mp1 && make"
    with pytest.raises(ValueError):
        wsl_bridge.build_wsl_command("   ")


class FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_in_wsl_success_and_failure():
    calls = []

    def runner(argv, capture_output, text, timeout):
        calls.append(argv)
        return FakeCompleted(0, stdout="built\n")

    res = wsl_bridge.run_in_wsl("make", _runner=runner)
    assert res == {"ok": True, "exit_code": 0, "stdout": "built\n", "stderr": ""}
    assert calls[0][:3] == ["wsl.exe", "-d", "Ubuntu"]

    res = wsl_bridge.run_in_wsl("make", _runner=lambda *a, **k: FakeCompleted(2, stderr="error: no target\n"))
    assert res["ok"] is False and res["exit_code"] == 2
    assert "no target" in res["stderr"]


def test_run_in_wsl_missing_wsl_timeout_and_empty():
    def missing(*a, **k):
        raise FileNotFoundError("wsl.exe")

    res = wsl_bridge.run_in_wsl("make", _runner=missing)
    assert res["ok"] is False and "WSL is not installed" in res["error"]

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="wsl.exe", timeout=5)

    res = wsl_bridge.run_in_wsl("make", timeout_sec=5, _runner=slow)
    assert res["ok"] is False and "timed out after 5s" in res["error"]

    res = wsl_bridge.run_in_wsl("  ")
    assert res["ok"] is False and res["exit_code"] is None


def test_run_in_wsl_truncates_long_output_tail_kept():
    big = "x" * 9000 + "TAIL-MARKER"
    res = wsl_bridge.run_in_wsl("make", _runner=lambda *a, **k: FakeCompleted(0, stdout=big))
    assert len(res["stdout"]) <= wsl_bridge.OUTPUT_CAP + 100
    assert res["stdout"].endswith("TAIL-MARKER")
    assert "truncated" in res["stdout"]


# ---------- MCP tool ----------

def test_exec_command_tool_registered_and_profiled(monkeypatch):
    names = [t["name"] for t in mcp_server.TOOLS]
    assert len(names) == len(set(names)) == 24
    assert "exec_command" in names
    schema = next(t for t in mcp_server.TOOLS if t["name"] == "exec_command")
    assert schema["inputSchema"]["required"] == ["command"]
    assert "backend" in schema["inputSchema"]["properties"]
    assert "setup_toolchain" in schema["inputSchema"]["properties"]
    assert "exec_command" in mcp_server.TOOL_PROFILES["programming"]
    monkeypatch.setenv("AGENTIC_TOOL_PROFILE", "programming")
    assert "exec_command" in {t["name"] for t in mcp_server.active_tools()}


def test_exec_command_handler_dispatches_to_backend(monkeypatch):
    seen = {}

    def fake_run(command, workdir=None, backend="auto", timeout_sec=300,
                 distro="Ubuntu", as_root=False, setup_toolchain=True):
        seen.update(command=command, workdir=workdir, backend=backend,
                    timeout_sec=timeout_sec, distro=distro, as_root=as_root,
                    setup_toolchain=setup_toolchain)
        return {"ok": True, "exit_code": 0, "stdout": "pong\n", "stderr": "", "backend": backend}

    monkeypatch.setattr(mcp_server.exec_backend, "run_command", fake_run)
    res = asyncio.run(mcp_server._handle_call_tool_impl(
        "exec_command", {"command": "mn --test pingall", "workdir": r"C:\mp1",
                         "backend": "wsl", "as_root": True}))
    payload = json.loads(res["content"][0]["text"])
    assert payload["ok"] is True and payload["stdout"] == "pong\n"
    assert seen == {"command": "mn --test pingall", "workdir": r"C:\mp1",
                    "backend": "wsl", "timeout_sec": 300, "distro": "Ubuntu",
                    "as_root": True, "setup_toolchain": True}
    # wsl_exec is the pre-rename alias: same handler, backend forced to wsl.
    res = asyncio.run(mcp_server._handle_call_tool_impl(
        "wsl_exec", {"command": "make"}))
    assert json.loads(res["content"][0]["text"])["backend"] == "wsl"
    assert seen["backend"] == "wsl" and seen["command"] == "make"


# ---------- orchestrator branch ----------

class FakeNavPage:
    async def evaluate(self, *a, **k):
        return None

    async def goto(self, *a, **k):
        return None

    async def wait_for_selector(self, *a, **k):
        return None


def _run_programming_branch(monkeypatch, spec):
    class FakePM:
        def __init__(self, cdp_url=None):
            pass

        async def extract_task_spec(self, page=None, tab_keyword=None):
            return dict(spec)

    monkeypatch.setattr(co, "ProgrammingAssignmentManager", FakePM)
    orch = co.CourseOrchestrator(cdp_url="http://127.0.0.1:9222")
    item = {"title": "Gossip Protocol", "type": "programming",
            "url": "https://www.coursera.org/learn/cloud-computing/programming/mp1"}
    return asyncio.run(orch._solve_item_on_page(FakeNavPage(), item))


def test_orchestrator_external_branch_uses_wsl_steps(monkeypatch):
    res = _run_programming_branch(monkeypatch, {
        "title": "Gossip Protocol", "kind": "cpp_cli", "wsl_required": True,
        "instructions_snippet": "submission token"})
    assert res["status"] == "programming_needs_agent"
    assert res["kind"] == "cpp_cli"
    joined = " ".join(res["next_steps"])
    assert "exec_command" in joined
    assert "never use submit_code_file" in joined


def test_orchestrator_mininet_branch_starts_ovs(monkeypatch):
    res = _run_programming_branch(monkeypatch, {
        "title": "Programming Assignment 2", "kind": "mininet", "wsl_required": True,
        "instructions_snippet": "mininet topology"})
    assert res["kind"] == "mininet"
    joined = " ".join(res["next_steps"])
    assert "openvswitch-switch" in joined and "as_root" in joined
    assert "backend=wsl" in joined and "Linux VM" in joined


def test_orchestrator_upload_branch_unchanged(monkeypatch):
    res = _run_programming_branch(monkeypatch, {
        "title": "Programming Assignment", "kind": "upload", "wsl_required": False,
        "instructions_snippet": "upload your file"})
    assert res["kind"] == "upload"
    joined = " ".join(res["next_steps"])
    assert "submit_code_file" in joined and "poll_autograder_results" in joined
    assert "exec_command" not in joined


# ---------- instruction files ----------

def test_instruction_files_carry_external_assignment_rule():
    for rel in INSTRUCTION_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "External Assignment Rule" in text, f"{rel}: external rule missing"
        assert "exec_command" in text, f"{rel}: no exec_command workflow"
        assert "submission token" in text, f"{rel}: no submission-token wording"
        assert "native Windows toolchain" in text, f"{rel}: no WSL-free fallback wording"
        assert "Linux VM" in text, f"{rel}: no Mininet-needs-Linux wording"


def test_instruction_files_carry_parallel_programming_rule():
    for rel in INSTRUCTION_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "Parallel Programming Rule" in text, f"{rel}: parallel rule missing"
        assert "4 tabs" in text, f"{rel}: no 4-tab wording"
        assert "round-robin" in text, f"{rel}: no round-robin polling wording"
        assert "TIMEOUT" in text, f"{rel}: no TIMEOUT-means-grading wording"
        assert "no matter which courses" in text, f"{rel}: no cross-course wording"


def test_orchestrator_branches_carry_parallel_hint(monkeypatch):
    for spec in (
            {"title": "PA", "kind": "upload", "wsl_required": False,
             "instructions_snippet": "upload your file"},
            {"title": "MP1", "kind": "cpp_cli", "wsl_required": True,
             "instructions_snippet": "submission token"},
            {"title": "PA2", "kind": "mininet", "wsl_required": True,
             "instructions_snippet": "mininet topology"}):
        res = _run_programming_branch(monkeypatch, spec)
        joined = " ".join(res["next_steps"])
        assert "parallel" in joined, f"{spec['kind']}: no parallel hint"
        assert "never wait idle" in joined, f"{spec['kind']}: no no-idle-wait hint"
