"""Browser job-breakaway fix (2026-11-12).

Live episode: the host agent launched Chrome from Antigravity's terminal;
ensure_browser reported "Successfully launched ... (window brought to
front)! Running: True", yet the very next command (list-tabs) got
ECONNREFUSED on port 9222 and no Chrome window ever appeared - on any
desktop (Win+Tab showed only the Antigravity window). Cause: IDE
terminals run commands inside a Windows Job Object with kill-on-close,
so the browser was killed the instant the launching command exited.
DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP do not escape the job;
only CREATE_BREAKAWAY_FROM_JOB does.

Fix: launch_browser spawns with the breakaway flag, falls back to a
plain detached spawn when the job forbids breakaway (and says so in
its message), and every instruction file carries a Browser Lifetime
Rule: launch via the MCP tool only, never loop re-launching, and if a
launch succeeds but the next command cannot connect, the user
double-clicks launch_browser_windows.bat once.
"""
import sys
from pathlib import Path

import quiz_pilot.browser_launcher as bl

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION_FILES = [
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules",
    ".github/copilot-instructions.md",
    ".agents/skills/agentic-helper/SKILL.md",
    ".gemini/skills/agentic-helper/SKILL.md",
]
BREAKAWAY = 0x01000000


def test_windows_spawn_flags_include_breakaway():
    flags = bl._windows_spawn_flags()
    assert flags & BREAKAWAY, "breakaway flag missing - browser will be job-killed"
    assert flags & 0x00000008, "DETACHED_PROCESS missing"
    assert flags & 0x00000200, "CREATE_NEW_PROCESS_GROUP missing"


def _run_launch(monkeypatch, popen_behaviour):
    """Drive launch_browser's Windows spawn path with fakes. popen_behaviour
    is a list of outcomes ('ok' or 'raise') consumed per Popen call."""
    monkeypatch.setattr(sys, "platform", "win32")
    calls = {"popen": [], "running": 0}

    def fake_running(port=9222):
        calls["running"] += 1
        return calls["running"] > 1  # False on entry check, True in wait loop

    monkeypatch.setattr(bl, "is_browser_running", fake_running)
    monkeypatch.setattr(bl, "get_browser_candidates", lambda: ["C:\\fake\\chrome.exe"])
    monkeypatch.setattr(bl, "open_url_in_running_browser", lambda url, port=9222: True)
    monkeypatch.setattr(bl, "bring_browser_to_front", lambda port=9222: True)
    monkeypatch.setattr(bl.time, "sleep", lambda s: None)
    monkeypatch.setattr(bl.os, "makedirs", lambda *a, **k: None)

    def fake_popen(cmd, **kwargs):
        calls["popen"].append(kwargs.get("creationflags"))
        outcome = popen_behaviour.pop(0)
        if outcome == "raise":
            raise OSError("access denied: breakaway forbidden")
        return object()

    monkeypatch.setattr(bl.subprocess, "Popen", fake_popen)
    ok, msg = bl.launch_browser(url="https://www.coursera.org", port=9222)
    return ok, msg, calls


def test_launch_browser_spawns_with_breakaway_flags(monkeypatch):
    ok, msg, calls = _run_launch(monkeypatch, ["ok"])
    assert ok is True
    assert len(calls["popen"]) == 1
    assert calls["popen"][0] & BREAKAWAY
    assert "breakaway" not in msg.lower()


def test_launch_browser_falls_back_when_breakaway_refused(monkeypatch):
    ok, msg, calls = _run_launch(monkeypatch, ["raise", "ok"])
    assert ok is True
    assert len(calls["popen"]) == 2
    assert calls["popen"][0] & BREAKAWAY
    assert not (calls["popen"][1] & BREAKAWAY)
    assert "breakaway" in msg.lower()
    assert "launch_browser_windows.bat" in msg


def test_launch_browser_reports_failure_when_both_spawns_fail(monkeypatch):
    ok, msg, _ = _run_launch(monkeypatch, ["raise", "raise"])
    assert ok is False
    assert "Failed to launch browser" in msg


def test_instruction_files_carry_browser_lifetime_rule():
    for rel in INSTRUCTION_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "Browser Lifetime Rule" in text, f"{rel}: lifetime rule missing"
        assert "ECONNREFUSED" in text, f"{rel}: no ECONNREFUSED signal"
        assert "double-click" in text, f"{rel}: no .bat double-click fallback"
