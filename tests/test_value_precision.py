"""Signal precision regressions at the production analysis boundary."""

import json
from datetime import UTC, datetime

import duckdb

from slopsniffer.config import load_config
from slopsniffer.signals import runner
from tests.test_value_contracts import database, event


def call(
    con: duckdb.DuckDBPyConnection,
    sid: str,
    seq: int,
    tool: str,
    body: object,
    result: str = "ok",
    error: bool = False,
) -> str:
    eid = f"{sid}-{seq}"
    event(con, eid, sid, seq)
    event(con, eid + "-result", sid, seq + 1)
    con.execute(
        "INSERT INTO tool_calls (call_id, session_id, use_event_id, result_event_id, "
        "tool_name, input, result_text, is_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [eid, sid, eid, eid + "-result", tool, json.dumps(body), result, error],
    )
    return eid


def test_precision_separates_progress_polling_transient_errors_and_failed_repairs() -> None:
    with database() as con:
        sessions = (
            "progress",
            "repair",
            "mutation",
            "rerun",
            "changing",
            "poll",
            "transient",
            "retry",
            "startup",
            "active",
            "human",
            "notification",
            "parallel",
            "parallel-failures",
            "pending-failures",
            "read-errors",
            "mixed-checks",
            "json-retry",
            "shell-reads",
            "old-open",
            "fresh-open",
            "skill-error",
            "skill-transient",
            "busy-wait",
            "stable-success",
            "inspect",
            "mutation-rerun",
            "instructions",
            "relative-clause",
            "recovery",
            "provider-notice",
            "code-feedback",
        )
        for sid in sessions:
            con.execute(
                "INSERT INTO sessions (session_id, harness, ended_at) "
                "VALUES (?, 'claude', '2026-01-01T00:00:00Z')",
                [sid],
            )
        for sid in ("progress", "repair", "mutation"):
            for n in range(5):
                call(con, sid, n * 10, "Edit", {"file_path": "file.py"})
                if sid == "progress":
                    call(con, sid, n * 10 + 2, "Bash", {"command": "pytest"}, "tests pass")
                if sid != "progress":
                    call(
                        con,
                        sid,
                        n * 10 + 2,
                        "Bash",
                        {"command": "pytest test_mutation.py" if sid == "mutation" else "pytest"},
                        "AssertionError: still broken",
                        True,
                    )
        for sid in ("read-errors", "mixed-checks", "shell-reads"):
            for n in range(5):
                call(con, sid, n * 10, "Edit", {"file_path": "file.py"})
                if n in (1, 2):
                    tool = "Read" if sid == "read-errors" else "Bash"
                    body = (
                        {"file_path": "missing"}
                        if sid == "read-errors"
                        else {"command": "pytest" if n == 1 else "jest"}
                    )
                    if sid == "shell-reads":
                        body = {"command": "cat missing/test.py"}
                    call(con, sid, n * 10 + 2, tool, body, "same deterministic failure", True)
        for n in range(2):
            call(
                con,
                "json-retry",
                n * 10,
                "exec_command",
                {"cmd": "python verify.py"},
                json.dumps({"output": "bad", "metadata": {"exit_code": 1}}),
            )
        con.execute("UPDATE tool_calls SET is_error=NULL WHERE session_id='json-retry'")
        for sid in (
            "rerun",
            "changing",
            "poll",
            "busy-wait",
            "stable-success",
            "inspect",
            "mutation-rerun",
        ):
            for n in range(3):
                call(
                    con,
                    sid,
                    n * 10,
                    "Bash",
                    {
                        "command": {
                            "poll": "sleep 1; ps",
                            "busy-wait": "echo idle",
                            "inspect": "git show HEAD",
                            "mutation-rerun": "pytest test_mutation.py",
                        }.get(sid, "pytest")
                    },
                    str(n) if sid == "changing" else "same",
                    sid in ("rerun", "mutation-rerun"),
                )
                if sid in ("rerun", "mutation-rerun"):
                    call(con, sid, n * 10 + 2, "Read", {"file_path": f"context-{n}"})
        for n in range(3):
            call(con, "parallel", n * 10, "Bash", {"command": f"echo unique-{n}"})
        # Claude can emit several calls in one assistant event. A result belongs
        # to its call identity, never to every call sharing the event.
        con.execute(
            "UPDATE tool_calls SET use_event_id = 'parallel-0' WHERE session_id = 'parallel'"
        )
        for sid in ("parallel-failures", "pending-failures"):
            for n in range(3):
                call(con, sid, n * 10, "Read", {"file_path": "missing"}, "File not found", True)
        con.execute(
            "UPDATE tool_calls SET use_event_id='parallel-failures-0', "
            "result_event_id='parallel-failures-20-result' WHERE session_id='parallel-failures'"
        )
        con.execute(
            "UPDATE events SET seq=100 WHERE event_id IN "
            "(SELECT result_event_id FROM tool_calls WHERE session_id='pending-failures')"
        )
        for sid in ("transient", "retry"):
            for n in range(2):
                call(
                    con,
                    sid,
                    n * 10,
                    "Read",
                    {"file_path": "missing"},
                    "Connection temporarily unavailable"
                    if sid == "transient"
                    else "File not found",
                    True,
                )
        event(con, "startup-compact", "startup", 1, "system", {"subtype": "compact_boundary"})
        event(con, "active-start", "active", 1, "system", {"subtype": "compact_boundary"})
        call(con, "active", 2, "Read", {"file_path": "file"})
        event(con, "active-compact", "active", 4, "system", {"subtype": "compact_boundary"})
        event(con, "active-summary", "active", 5, "user", {"isCompactSummary": True})
        user_texts = {
            "human": "That's wrong.",
            "notification": "<task-notification>You missed a case</task-notification>",
            "instructions": "<INSTRUCTIONS>If you didn't intend a change, stop.</INSTRUCTIONS>",
            "relative-clause": 'An "it is fine" that is wrong is worse than UNKNOWN.',
            "recovery": "I restarted docker desktop try again",
            "provider-notice": "The model repeated its output; change settings and try again.",
            "code-feedback": "That's wrong.",
        }
        for sid, text in user_texts.items():
            event(con, sid + "-assistant", sid, 1)
            event(
                con,
                sid + "-user",
                sid,
                2,
                "user",
                {"message": {"visibility": "user_only"}} if sid == "provider-notice" else {},
            )
            con.execute(
                "INSERT INTO messages VALUES (?, 'assistant', ?)",
                [sid + "-assistant", "```python\nx = 1\n```" if sid == "code-feedback" else "done"],
            )
            con.execute("INSERT INTO messages VALUES (?, 'user', ?)", [sid + "-user", text])
        for sid in ("old-open", "fresh-open"):
            event(con, sid + "-use", sid, 1)
            con.execute(
                "INSERT INTO tool_calls (session_id, call_id, use_event_id, tool_name) "
                "VALUES (?, ?, ?, 'Bash')",
                [sid, sid + "-call", sid + "-use"],
            )
        con.execute(
            "UPDATE sessions SET ended_at='2026-11-12T00:00:00Z' WHERE session_id='fresh-open'"
        )
        con.execute(
            "INSERT INTO skills (path, name, scope, content_hash) "
            "VALUES ('/idle-skill.md', 'idle', 'global', 'hash')"
        )
        for sid in ("skill-error", "skill-transient"):
            event(con, sid + "-invoke", sid, 1)
            con.execute(
                "INSERT INTO skill_invocations VALUES (?, ?, 'demo', 'skill_tool', NULL)",
                [sid + "-invoke", sid],
            )
            call(
                con,
                sid,
                2,
                "Bash",
                {"command": "pytest"},
                "Command timed out" if sid == "skill-transient" else "make: error",
                True,
            )
        runner.compute(con, load_config(), None, datetime(2026, 11, 12, tzinfo=UTC))
        by_kind: dict[str, set[str]] = {}
        for kind, sid in con.execute("SELECT kind, session_id FROM signals").fetchall():
            by_kind.setdefault(kind, set()).add(sid)
        assert by_kind.get("edit_churn") == {"repair"}
        assert by_kind.get("rerun_loop") == {
            "rerun",
            "busy-wait",
            "stable-success",
            "inspect",
            "mutation-rerun",
        }
        assert {
            sid
            for (sid,) in con.execute(
                "SELECT session_id FROM signals WHERE kind='rerun_loop' AND severity > 0"
            ).fetchall()
        } == {"rerun", "busy-wait"}
        assert by_kind.get("unchanged_retry") == {"retry", "json-retry"}
        assert by_kind.get("context_overflow") == {"active"}
        assert by_kind.get("user_correction") == {"human", "code-feedback"}
        assert by_kind.get("orphaned_session") == {"old-open"}
        assert by_kind.get("skill_abandoned") == {"skill-error"}
        assert con.execute(
            "SELECT kind, session_id, severity FROM signals "
            "WHERE kind IN ('context_overflow', 'never_fired_skill') ORDER BY kind"
        ).fetchall() == [
            ("context_overflow", "active", 0.0),
            ("never_fired_skill", "/idle-skill.md", 0.0),
        ]
        assert con.execute(
            "SELECT evidence_event_ids FROM signals WHERE kind='context_overflow'"
        ).fetchall() == [(["active-compact"],)]
