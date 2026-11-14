"""Phase 1: tool profiles + telemetry (2026-11-06).

Profiles: tools/list exposes only the active profile's tools (env
AGENTIC_TOOL_PROFILE, default full = all 23, unknown value falls back
to full), and out-of-profile calls are refused before any work runs.

Telemetry: every wrapped call appends one JSONL line with counts and
sizes only (never argument/result content); summarize() aggregates
calls, latency, estimated tokens and the packet memory hit rate.
"""
import asyncio
import json

import pytest

from quiz_pilot import mcp_server, telemetry


# ----------------------------------------------------------------------
# profiles
# ----------------------------------------------------------------------

def test_default_profile_is_full_23(monkeypatch):
    monkeypatch.delenv("AGENTIC_TOOL_PROFILE", raising=False)
    assert mcp_server.get_tool_profile() == "full"
    assert len(mcp_server.active_tools()) == 24


def test_quiz_profile_exposes_only_packet_tools(monkeypatch):
    monkeypatch.setenv("AGENTIC_TOOL_PROFILE", "quiz")
    names = {t["name"] for t in mcp_server.active_tools()}
    assert names == {"launch_browser", "prepare_packet", "apply_packet", "get_feedback"}
    # The whole point: a much smaller schema payload for the host.
    assert len(json.dumps(mcp_server.active_tools())) < len(json.dumps(mcp_server.TOOLS)) // 2


def test_course_and_specialist_profiles(monkeypatch):
    monkeypatch.setenv("AGENTIC_TOOL_PROFILE", "course")
    names = {t["name"] for t in mcp_server.active_tools()}
    assert "auto_complete_entire_course" in names
    assert "inspect_programming_task" not in names
    monkeypatch.setenv("AGENTIC_TOOL_PROFILE", "programming")
    names = {t["name"] for t in mcp_server.active_tools()}
    assert "submit_code_file" in names and "prepare_packet" not in names
    monkeypatch.setenv("AGENTIC_TOOL_PROFILE", "peer")
    names = {t["name"] for t in mcp_server.active_tools()}
    assert "complete_peer_reviews" in names and "prepare_packet" not in names


def test_unknown_profile_falls_back_to_full(monkeypatch):
    monkeypatch.setenv("AGENTIC_TOOL_PROFILE", "nonsense")
    assert mcp_server.get_tool_profile() == "full"
    assert len(mcp_server.active_tools()) == 24


def test_out_of_profile_call_refused_before_work(monkeypatch):
    monkeypatch.setenv("AGENTIC_TOOL_PROFILE", "quiz")
    monkeypatch.setenv("AGENTIC_TELEMETRY", "0")
    res = asyncio.run(mcp_server.handle_call_tool(
        "solve_multicast", {"mode": "fifo", "multicasts": [], "arrivals": []}))
    assert res.get("isError") is True
    assert "quiz" in res["content"][0]["text"]


def test_refusal_is_recorded_in_telemetry(tmp_path, monkeypatch):
    # A refused call is a wasted host turn; it must show up in telemetry
    # as an error under the active profile.
    path = str(tmp_path / "tool_calls.jsonl")
    monkeypatch.setenv("AGENTIC_TOOL_PROFILE", "quiz")
    monkeypatch.setenv("AGENTIC_TELEMETRY_PATH", path)
    monkeypatch.delenv("AGENTIC_TELEMETRY", raising=False)
    res = asyncio.run(mcp_server.handle_call_tool("solve_multicast", {}))
    assert res.get("isError") is True
    entry = json.loads(open(path, encoding="utf-8").read().strip())
    assert entry["tool"] == "solve_multicast"
    assert entry["profile"] == "quiz" and entry["error"] is True


# ----------------------------------------------------------------------
# telemetry
# ----------------------------------------------------------------------

def test_record_and_summarize_no_content_leak(tmp_path):
    path = str(tmp_path / "tool_calls.jsonl")
    resp = {"content": [{"type": "text", "text": json.dumps(
        {"packet_id": "pkt-x", "known_count": 7, "unknown_count": 3, "total": 10})}]}
    telemetry.record_tool_call(path, tool="prepare_packet", profile="quiz",
                               elapsed_ms=120.0,
                               request_obj={"prompt": "SECRET-QUESTION-TEXT"},
                               response=resp)
    telemetry.record_tool_call(path, tool="apply_packet", profile="quiz",
                               elapsed_ms=80.0, request_obj={},
                               response={"isError": True,
                                         "content": [{"type": "text", "text": "boom"}]})
    raw = open(path, encoding="utf-8").read()
    assert "SECRET-QUESTION-TEXT" not in raw  # counts/sizes only, never content
    s = telemetry.summarize(path)
    assert s["calls"] == 2 and s["errors"] == 1
    assert s["total_est_tokens"] > 0
    assert s["packet_memory_hit_pct"] == 70.0
    assert s["per_tool"]["prepare_packet"]["calls"] == 1


def test_summarize_missing_file_is_empty(tmp_path):
    s = telemetry.summarize(str(tmp_path / "nope.jsonl"))
    assert s["calls"] == 0 and s["packet_memory_hit_pct"] is None


def test_wrapper_records_telemetry(tmp_path, monkeypatch):
    path = str(tmp_path / "tool_calls.jsonl")
    monkeypatch.setenv("AGENTIC_TELEMETRY_PATH", path)
    monkeypatch.delenv("AGENTIC_TOOL_PROFILE", raising=False)

    async def fake_impl(name, arguments):
        return {"content": [{"type": "text", "text": '{"ok":true}'}]}

    monkeypatch.setattr(mcp_server, "_handle_call_tool_impl", fake_impl)
    res = asyncio.run(mcp_server.handle_call_tool("prepare_packet", {"quiz_id": "q1"}))
    assert res["content"][0]["text"] == '{"ok":true}'
    lines = open(path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "prepare_packet" and entry["profile"] == "full"
    assert entry["est_tokens"] > 0 and entry["error"] is False
