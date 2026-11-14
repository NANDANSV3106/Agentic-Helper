# MCP Tool Profiles & Telemetry (Phase 1 — token efficiency)

## Why

The MCP server exposes 24 tools. Every tool schema sits in the host
agent's context for the whole session, even for a plain quiz run that
only ever calls 4 of them. Tool profiles let each task load only its
own tools. Telemetry then measures whether any of this actually helps.

## Profiles

Set `AGENTIC_TOOL_PROFILE` in the MCP server's environment:

| Profile | Tools | Use for |
|---|---|---|
| `quiz` (4) | `launch_browser`, `prepare_packet`, `apply_packet`, `get_feedback` | A single quiz — the normal Host-Packet flow |
| `course` (9) | quiz set + `crawl_syllabus`, `smart_multiworker`, `open_and_solve_parallel`, `auto_complete_entire_course`, `auto_complete_specialization` | Whole courses / specializations |
| `programming` (6) | `launch_browser`, `inspect_programming_task`, `submit_code_file`, `poll_autograder_results`, `auto_solve_current_assignment`, `exec_command` | Programming assignments |
| `peer` (4) | `launch_browser`, `submit_peer_assignment`, `complete_peer_reviews`, `auto_solve_current_assignment` | Peer-graded assignments |
| `solvers` (2) | `solve_multicast`, `solve_diagram` | Local deterministic solvers only |
| `full` (23, default) | everything | Manual / mixed work — behaviour unchanged when the variable is unset |

Calls to a tool outside the active profile are refused with an error
naming the profile to restart with — nothing runs silently.

## Example configuration (Antigravity / Cursor style)

Register one server per profile and enable the one the task needs:

```json
{
  "mcpServers": {
    "agentic-helper-quiz": {
      "command": "python",
      "args": ["-m", "quiz_pilot.mcp_server"],
      "env": { "AGENTIC_TOOL_PROFILE": "quiz" }
    },
    "agentic-helper-course": {
      "command": "python",
      "args": ["-m", "quiz_pilot.mcp_server"],
      "env": { "AGENTIC_TOOL_PROFILE": "course" }
    },
    "agentic-helper-full": {
      "command": "python",
      "args": ["-m", "quiz_pilot.mcp_server"],
      "env": { "AGENTIC_TOOL_PROFILE": "full" }
    }
  }
}
```

Run the server from the project folder so the package and its
`.assessment_cache` resolve the same way as the default setup.

## Telemetry

Every MCP tool call is timed and measured into
`.assessment_cache/telemetry/tool_calls.jsonl`: tool name, profile,
elapsed ms, request/response bytes, estimated tokens (bytes / 4), and
for packet calls the known/unknown counts. **Counts and sizes only —
no question text, answers, or arguments are stored.**

The MCP server cannot see the host model's real token counters;
bytes / estimated tokens, tool-call counts and latency are the honest
proxies.

Read the scoreboard:

```bat
python -m quiz_pilot.telemetry
agentic-helper stats
agentic-helper stats --json
```

Key numbers: total estimated tokens, calls per tool, average latency,
and `packet_memory_hit_pct` — the share of packet questions the server
answered from verified memory instead of sending to the host.

Disable recording with `AGENTIC_TELEMETRY=0`, or redirect the file
with `AGENTIC_TELEMETRY_PATH=<path>`.
