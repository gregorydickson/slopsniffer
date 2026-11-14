"""Synthetic examples of recently introduced transcript records."""

from typing import Any

CODEX_PAYLOADS: list[dict[str, Any]] = [
    {
        "type": "mcp_tool_call_end",
        "call_id": "mcp-ok",
        "invocation": {"server": "memory", "tool": "lookup", "arguments": {"query": "needle"}},
        "result": {"Ok": {"content": [{"type": "text", "text": "found"}], "isError": False}},
    },
    {
        "type": "mcp_tool_call_end",
        "call_id": "mcp-error",
        "invocation": {"server": "memory", "tool": "lookup", "arguments": {"query": "missing"}},
        "result": {"Err": "transport failed"},
    },
    {"type": "web_search_end", "call_id": "web", "action": {"type": "search", "query": "sqlite"}},
    {"type": "tool_search_call", "call_id": "search", "arguments": {"query": "tools"}},
    {"type": "tool_search_output", "call_id": "search", "tools": [{"name": "lookup"}]},
    {
        "type": "collab_agent_spawn_end",
        "call_id": "spawn",
        "new_thread_id": "child",
        "prompt": "check this",
        "status": "pending_init",
    },
    {"type": "sub_agent_activity", "agent_thread_id": "child", "kind": "started"},
    {"type": "context_compacted"},
    {"type": "thread_rolled_back", "num_turns": 1},
    {"type": "entered_review_mode", "target": {"type": "uncommittedChanges"}},
    {"type": "exited_review_mode"},
    {
        "type": "collab_agent_interaction_end",
        "call_id": "interaction",
        "receiver_thread_id": "peer",
        "prompt": "hello",
        "status": {"completed": "done"},
    },
]
CODEX = [
    {
        "type": "response_item" if p["type"].startswith("tool_search") else "event_msg",
        "timestamp": "2026-11-09T12:00:00Z",
        "payload": p,
    }
    for p in CODEX_PAYLOADS
]
CLAUDE: list[dict[str, Any]] = [
    {"type": "file-history-snapshot", "snapshot": {"trackedFileBackups": {"x.py": {"version": 1}}}},
    {"type": "file-history-delta", "trackingPath": "x.py", "backup": {"version": 2}},
    {
        "type": "worktree-state",
        "worktreeSession": {"worktreePath": "/work/review", "worktreeBranch": "review"},
    },
    {"type": "relocated", "relocatedCwd": "/work/final"},
    {"type": "mode", "mode": "plan"},
    {"type": "permission-mode", "permissionMode": "default"},
    {"type": "fork-context-ref", "agentId": "child", "parentSessionId": "ancestor"},
    {"type": "started", "agentId": "worker"},
    {"type": "result", "agentId": "worker", "result": {"findings": ["a finding"]}},
    {"type": "failed", "agentId": "failed-worker"},
    {"type": "launched"},
]
