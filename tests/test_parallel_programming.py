"""Parallel programming (2026-11-14): the agent used to work one
programming assignment at a time and sit idle while Coursera graded
it. Programming must run like quizzes - up to 4 tabs at once across
any courses, polling in round-robin with short waits.

- A poll that times out must say the grader is STILL GRADING and
  direct the agent to another tab, not to "check manually".
- A finished poll reports PASSED/COMPLETED from the page state.
- The poll tool description carries the round-robin guidance.

All polling goes through a fake page: nothing here touches a browser.
"""
import asyncio

from quiz_pilot import mcp_server
from quiz_pilot.programming_solver import ProgrammingAssignmentManager


class FakePage:
    def __init__(self, result):
        self._result = result
        self.calls = 0

    async def evaluate(self, *a, **k):
        self.calls += 1
        return dict(self._result)


def _grading_forever():
    return {"is_grading": True, "score_pct": None, "passed": False,
            "test_cases": [], "grader_output": ""}


def _graded(score, passed):
    return {"is_grading": False, "score_pct": score, "passed": passed,
            "test_cases": [], "grader_output": "log"}


def test_poll_timeout_means_still_grading_and_directs_to_next_tab():
    mgr = ProgrammingAssignmentManager(cdp_url="http://127.0.0.1:9222")
    res = asyncio.run(mgr.poll_autograder_results(
        page=FakePage(_grading_forever()), timeout_sec=0))
    assert res["status"] == "TIMEOUT"
    assert "still grading" in res["message"]
    assert "Do NOT wait" in res["message"]
    assert "poll_autograder_results again" in res["message"]
    assert "manually" not in res["message"]


def test_poll_returns_passed_when_grading_done():
    mgr = ProgrammingAssignmentManager(cdp_url="http://127.0.0.1:9222")
    res = asyncio.run(mgr.poll_autograder_results(
        page=FakePage(_graded(95, True)), timeout_sec=120))
    assert res["status"] == "PASSED"
    assert res["score_pct"] == 95
    assert res["elapsed_sec"] == 0


def test_poll_returns_completed_when_grading_done_but_failed():
    mgr = ProgrammingAssignmentManager(cdp_url="http://127.0.0.1:9222")
    res = asyncio.run(mgr.poll_autograder_results(
        page=FakePage(_graded(40, False)), timeout_sec=120))
    assert res["status"] == "COMPLETED"
    assert res["passed"] is False


def test_poll_tool_description_mentions_round_robin():
    schema = next(t for t in mcp_server.TOOLS if t["name"] == "poll_autograder_results")
    desc = schema["description"]
    assert "round-robin" in desc
    assert "TIMEOUT" in desc
    assert "30-60" in desc
