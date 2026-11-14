"""Per-tool-call telemetry for token/speed measurement (Phase 1).

The MCP server cannot see the host model's real token counters, so this
records the honest proxies for every tool call: elapsed time, request
and response byte sizes, an estimated token count (bytes / 4, the usual
rough conversion), and - for packet calls - how many questions were
served from verified memory vs sent to the host. Aggregated over a run
these give the scoreboard that matters: estimated tokens per passed
quiz, tool calls per attempt, memory hit rate, and latency per call.

Privacy: only counts, sizes and timings are stored - never argument or
result content. Stored as JSONL under .assessment_cache/telemetry/.

Usage:
    python -m quiz_pilot.telemetry            # print aggregate summary
    python -m quiz_pilot.telemetry <file>     # summarize a specific file
"""

import json
import math
import os
import sys
import time
from typing import Any, Dict, Optional


def default_telemetry_path(base_cache_dir: Optional[str] = None) -> str:
    override = os.environ.get("AGENTIC_TELEMETRY_PATH")
    if override:
        return override
    base = base_cache_dir or os.path.join(os.getcwd(), ".assessment_cache")
    return os.path.join(base, "telemetry", "tool_calls.jsonl")


def _response_text(response: Any) -> str:
    try:
        content = (response or {}).get("content") or []
        if content and isinstance(content[0], dict):
            return str(content[0].get("text", ""))
    except Exception:
        pass
    try:
        return json.dumps(response, default=str)
    except Exception:
        return ""


def record_tool_call(path: str, *, tool: str, profile: str, elapsed_ms: float,
                     request_obj: Any, response: Any, error: bool = False) -> None:
    """Appends one telemetry line. NEVER raises: telemetry must not be
    able to break a tool call."""
    try:
        if os.environ.get("AGENTIC_TELEMETRY", "1") == "0":
            return
        try:
            req_bytes = len(json.dumps(request_obj, default=str).encode("utf-8"))
        except Exception:
            req_bytes = 0
        resp_text = _response_text(response)
        resp_bytes = len(resp_text.encode("utf-8"))
        entry: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "tool": tool,
            "profile": profile,
            "elapsed_ms": round(float(elapsed_ms), 1),
            "request_bytes": req_bytes,
            "response_bytes": resp_bytes,
            "est_tokens": int(math.ceil((req_bytes + resp_bytes) / 4.0)),
            "error": bool(error or (isinstance(response, dict) and response.get("isError"))),
        }
        # Packet stats: how much the server absorbed vs sent to the host.
        try:
            parsed = json.loads(resp_text) if resp_text else None
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("known_count", "unknown_count", "total",
                        "coverage_pct", "score_pct", "status"):
                if key in parsed:
                    entry[key] = parsed[key]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        pass


def summarize(path: str) -> Dict[str, Any]:
    """Aggregates a telemetry JSONL file. Missing file -> empty summary."""
    per_tool: Dict[str, Dict[str, Any]] = {}
    calls = errors = 0
    total_ms = 0.0
    total_tokens = 0
    total_resp = 0
    pkt_known = pkt_total = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            e = json.loads(line)
        except Exception:
            continue
        calls += 1
        errors += 1 if e.get("error") else 0
        total_ms += float(e.get("elapsed_ms", 0))
        total_tokens += int(e.get("est_tokens", 0))
        total_resp += int(e.get("response_bytes", 0))
        t = per_tool.setdefault(e.get("tool", "?"), {
            "calls": 0, "errors": 0, "elapsed_ms": 0.0,
            "est_tokens": 0, "response_bytes": 0,
        })
        t["calls"] += 1
        t["errors"] += 1 if e.get("error") else 0
        t["elapsed_ms"] += float(e.get("elapsed_ms", 0))
        t["est_tokens"] += int(e.get("est_tokens", 0))
        t["response_bytes"] += int(e.get("response_bytes", 0))
        if "known_count" in e and "total" in e:
            pkt_known += int(e.get("known_count", 0))
            pkt_total += int(e.get("total", 0))
    for t in per_tool.values():
        t["avg_elapsed_ms"] = round(t["elapsed_ms"] / t["calls"], 1) if t["calls"] else 0
        t["elapsed_ms"] = round(t["elapsed_ms"], 1)
    return {
        "calls": calls,
        "errors": errors,
        "total_elapsed_ms": round(total_ms, 1),
        "avg_elapsed_ms": round(total_ms / calls, 1) if calls else 0,
        "total_est_tokens": total_tokens,
        "total_response_bytes": total_resp,
        "packet_memory_hit_pct": (round(100.0 * pkt_known / pkt_total, 1)
                                  if pkt_total else None),
        "per_tool": per_tool,
    }


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = argv[0] if argv else default_telemetry_path()
    print(json.dumps(summarize(path), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
