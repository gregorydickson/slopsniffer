import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slopsniffer.db import open_write
from slopsniffer.ingest import engine
from slopsniffer.signals.runner import compute
from tests.ingest.crush_support import fixture_config, message, part


@pytest.mark.parametrize("failed", [True, False])
def test_crush_shell_edits_errors_compactions_and_unanswered_calls_reach_signals(
    tmp_path: Path,
    failed: bool,
) -> None:
    config, database = fixture_config(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE messages SET parts=? WHERE id='t1'",
            [
                json.dumps(
                    [
                        part(
                            "tool_result",
                            tool_call_id="call",
                            content="failed output" if failed else "done",
                            is_error=failed,
                        )
                    ]
                )
            ],
        )
        db.execute("INSERT INTO sessions VALUES ('startup',NULL,'Startup summary',0,0,0,90,120)")
        message(
            db,
            "startup",
            "only-summary",
            "assistant",
            [part("text", text="Inherited summary")],
            summary=True,
        )
        message(
            db, "startup", "after-startup", "user", [part("text", text="that's wrong")], when=105
        )
        for i in range(2):
            message(
                db,
                "parent",
                f"retry-{i}",
                "assistant",
                [
                    part("text", text="retrying"),
                    part(
                        "tool_call",
                        id=f"retry-{i}",
                        name="bash",
                        input=json.dumps({"command": "pytest -q"}),
                    ),
                    part(
                        "tool_result",
                        tool_call_id=f"retry-{i}",
                        content="failed output" if failed else "done",
                        is_error=failed,
                    ),
                ],
                when=110 + i,
            )
        for i, name in enumerate(["edit", "multiedit", "write", "lsp_replace_symbol", "edit"]):
            message(
                db,
                "parent",
                f"edit-{i}",
                "assistant",
                [
                    part("text", text="editing"),
                    part("tool_call", id=f"edit-{i}", name=name, input='{"file_path":"same.py"}'),
                    part("tool_result", tool_call_id=f"edit-{i}", content="done", is_error=False),
                ],
                when=120 + i * 2,
            )
            if i < 4:
                message(
                    db,
                    "parent",
                    f"check-{i}",
                    "assistant",
                    [
                        part(
                            "tool_call",
                            id=f"check-{i}",
                            name="bash",
                            input='{"command":"pytest -q"}',
                        ),
                        part(
                            "tool_result",
                            tool_call_id=f"check-{i}",
                            content="failed output" if failed else "done",
                            is_error=failed,
                        ),
                    ],
                    when=121 + i * 2,
                )
        message(
            db,
            "child",
            "before-summary",
            "assistant",
            [
                part("tool_call", id="child-call", name="bash", input='{"command":"pwd"}'),
                part("tool_result", tool_call_id="child-call", content="project", is_error=False),
            ],
            when=99,
        )
        message(db, "parent", "correction", "user", [part("text", text="that's wrong")], when=130)
        message(
            db,
            "parent",
            "unanswered",
            "assistant",
            [part("tool_call", id="unanswered", name="view", input='{"file_path":"same.py"}')],
            when=131,
        )
        message(db, "child", "after-summary", "user", [part("text", text="Continue")], when=105)
    with open_write(config) as con:
        summary = engine.ingest(con, config, ["crush"], workers=1)
        assert summary["harnesses"]["crush"]["files_failed"] == []
        compute(con, config, None, datetime(2026, 11, 12, tzinfo=UTC))
        parent = con.execute(
            "SELECT session_id FROM sessions WHERE title='Parent title'"
        ).fetchall()[0][0]
        child = con.execute("SELECT session_id FROM sessions WHERE title='Child title'").fetchall()[
            0
        ][0]
        expected = {
            (parent, "rerun_loop"),
            (parent, "user_correction"),
            (parent, "orphaned_session"),
            (child, "context_overflow"),
        }
        if failed:
            expected |= {(parent, "edit_churn"), (parent, "unchanged_retry")}
        assert set(con.execute("SELECT session_id,kind FROM signals").fetchall()) == expected
        details = {
            kind: json.loads(raw)
            for kind, raw in con.execute(
                "SELECT kind,details::VARCHAR FROM signals WHERE session_id=?", [parent]
            ).fetchall()
        }
        if failed:
            assert details["edit_churn"]["path"] == "same.py"
        assert con.execute(
            "SELECT severity > 0 FROM signals WHERE kind='rerun_loop'"
        ).fetchall() == [(failed,)]
        assert details["rerun_loop"]["command"] == "pytest -q"
