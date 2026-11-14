"""Retry-until-pass + programming-assignment fixes (2026-11-12).

Live reports: (1) a failed quiz was NOT re-attempted - the apply_packet
FAILED receipt carried only a soft prose hint, and get_feedback returned
raw feedback with no retry guidance at all, so the manual flow ended at
feedback; (2) programming assignments were skipped - the orchestrator
had NO programming branch (items fell through as silent "pending") and
the instruction files contained only one vague sentence about them.

These tests prove, by outcomes: FAILED receipts now carry a
machine-readable RETRY directive with the attempt budget (and an honest
CAP_REACHED at attempt 10); get_feedback adds the same directive; the
orchestrator hands programming items over visibly with their spec; and
all 7 instruction files carry both new rules.
"""
import asyncio
import json
from pathlib import Path

import pytest

from quiz_pilot import mcp_server
from quiz_pilot.attempt_protector import FastAttemptManager
from quiz_pilot.course_orchestrator import CourseOrchestrator
from quiz_pilot.packet_mode import PacketManager
from quiz_pilot.programming_solver import ProgrammingAssignmentManager

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION_FILES = [
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules",
    ".github/copilot-instructions.md",
    ".agents/skills/agentic-helper/SKILL.md",
    ".gemini/skills/agentic-helper/SKILL.md",
]
QUIZ = "Module 1 Quiz [course-1]"
QUESTIONS = [
    {"index": 0, "type": "radio", "prompt": "What is 2 + 2?",
     "options": ["3", "4", "5"], "images": []},
    {"index": 1, "type": "radio", "prompt": "Capital of France?",
     "options": ["London", "Paris", "Rome"], "images": []},
]


class FakeAdapter:
    def __init__(self, feedback):
        self._feedback = feedback

    async def batch_fill_answers_async(self, answers, auto_submit=False):
        return {"filled": len(answers)}

    async def submit_assessment_async(self):
        return True

    async def extract_feedback_async(self):
        return self._feedback

    async def extract_questions_async(self):
        return QUESTIONS


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    mgr = FastAttemptManager(base_cache_dir=str(tmp_path / "cache"))
    monkeypatch.setattr(mcp_server, "attempt_mgr", mgr)
    monkeypatch.setattr(mcp_server, "packet_mgr", PacketManager(mgr))

    async def _nosleep(_x):
        return None

    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    return mgr


def _receipt_text(res):
    return json.loads(res["content"][0]["text"])


def _run(coro):
    return asyncio.run(coro)


def test_apply_packet_failed_receipt_demands_retry(wired, monkeypatch):
    fb = {"feedback": {"0": True, "1": False}, "score_pct": 50.0, "passed": False}
    monkeypatch.setattr(mcp_server, "get_lms_adapter", lambda page: FakeAdapter(fb))
    packet = mcp_server.packet_mgr.build_packet(quiz_id=QUIZ, questions=QUESTIONS)
    res = _run(mcp_server._dispatch_browser_tool(
        "apply_packet", {"packet_id": packet["packet_id"], "answers": {0: 1, 1: 0}},
        object(), "http://127.0.0.1:9222"))
    r = _receipt_text(res)
    assert r["status"] == "FAILED"
    assert r["next_action"] == "RETRY"
    assert r["retry_required"] is True
    assert r["attempt"] == 1 and r["attempts_remaining"] == 9
    assert r["wrong_indices"] == [1]
    assert "prepare_packet" in r["next"]


def test_apply_packet_cap_reached_at_tenth_attempt(wired, monkeypatch):
    fb = {"feedback": {"0": True, "1": False}, "score_pct": 50.0, "passed": False}
    monkeypatch.setattr(mcp_server, "get_lms_adapter", lambda page: FakeAdapter(fb))
    monkeypatch.setattr(wired, "next_attempt_num", lambda quiz_id: 10)
    packet = mcp_server.packet_mgr.build_packet(quiz_id=QUIZ, questions=QUESTIONS)
    res = _run(mcp_server._dispatch_browser_tool(
        "apply_packet", {"packet_id": packet["packet_id"], "answers": {0: 1, 1: 0}},
        object(), "http://127.0.0.1:9222"))
    r = _receipt_text(res)
    assert r["status"] == "FAILED"
    assert r["next_action"] == "CAP_REACHED"
    assert r["retry_required"] is False and r["attempts_remaining"] == 0


def test_get_feedback_failed_directs_retry(monkeypatch):
    fb = {"feedback": {"0": True, "1": False}, "score_pct": 50.0, "passed": False}
    monkeypatch.setattr(mcp_server, "get_lms_adapter", lambda page: FakeAdapter(fb))
    res = _run(mcp_server._dispatch_browser_tool(
        "get_feedback", {}, object(), "http://127.0.0.1:9222"))
    r = _receipt_text(res)
    assert r["status"] == "FAILED"
    assert r["next_action"] == "RETRY" and r["retry_required"] is True
    assert r["feedback"] == {"0": True, "1": False}  # raw feedback preserved


def test_get_feedback_passed_has_no_retry(monkeypatch):
    fb = {"feedback": {"0": True, "1": True}, "score_pct": 100.0, "passed": True}
    monkeypatch.setattr(mcp_server, "get_lms_adapter", lambda page: FakeAdapter(fb))
    res = _run(mcp_server._dispatch_browser_tool(
        "get_feedback", {}, object(), "http://127.0.0.1:9222"))
    r = _receipt_text(res)
    assert r["status"] == "PASSED"
    assert "next_action" not in r


class FakePage:
    url = "https://www.coursera.org/learn/x/programming/1"

    async def evaluate(self, *args, **kwargs):
        return None

    async def goto(self, *args, **kwargs):
        return None

    async def wait_for_selector(self, *args, **kwargs):
        return None


def test_orchestrator_programming_item_needs_agent(monkeypatch):
    async def fake_extract(self, page=None, tab_keyword=None):
        return {"instructions": "Write a function add(a, b).", "starter": ["add.py"]}

    monkeypatch.setattr(ProgrammingAssignmentManager, "extract_task_spec", fake_extract)
    orch = CourseOrchestrator()
    res = _run(orch._solve_item_on_page(
        FakePage(),
        {"title": "Programming Assignment 1",
         "url": "https://www.coursera.org/learn/x/programming/1",
         "type": "programming"}))
    assert res["status"] == "programming_needs_agent"
    assert res["spec"]["instructions"].startswith("Write a function")
    assert any("submit_code_file" in s for s in res["next_steps"])
    assert any("poll_autograder_results" in s for s in res["next_steps"])


def test_instructions_carry_retry_and_programming_rules():
    for rel in INSTRUCTION_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "Retry-Until-Pass Rule" in text, f"{rel}: retry rule missing"
        assert "Programming Assignment Rule" in text, f"{rel}: programming rule missing"
        assert "10-attempt" in text, f"{rel}: attempt cap missing"
