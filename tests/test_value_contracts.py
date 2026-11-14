"""Cost, navigation and precision contracts, including ordinary-work negatives."""

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from slopsniffer import pricing, report, status
from slopsniffer.config import ModelPrice, load_config
from slopsniffer.contracts import Harness, Signal
from slopsniffer.db import apply_schema
from slopsniffer.grading.rubrics import load_rubrics
from slopsniffer.ingest import engine
from slopsniffer.outcomes import linkage
from slopsniffer.report import findings, html
from slopsniffer.signals import runner


def database() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(config={"threads": 1, "memory_limit": "32MB"})
    apply_schema(con)
    con.execute(
        "INSERT INTO source_files (source_file_id, path, harness) "
        "VALUES (1, '/transcripts/source.jsonl', 'claude')"
    )
    return con


def event(
    con: duckdb.DuckDBPyConnection,
    eid: str,
    sid: str,
    seq: int,
    kind: str = "assistant",
    raw: object = None,
) -> None:
    con.execute(
        "INSERT INTO events (event_id, session_id, seq, source_file_id, record_type, "
        "kind, byte_offset, byte_length, parsed) VALUES (?, ?, ?, 1, ?, 'message', ?, 100, ?)",
        [eid, sid, seq, kind, seq * 100, json.dumps(raw or {})],
    )


def test_costs_keep_known_dollars_exact_override_dates_cache_rates_and_unknowns() -> None:
    config = replace(
        load_config(),
        pricing={
            "known": ModelPrice(2, 0.2, 10, 3),
            "known-20261101": ModelPrice(4, 0.4, 20, 6),
            "no-write-rate": ModelPrice(2, 0.2, 10),
        },
    )
    with database() as con:
        subjects = {"complete", "partial", "unknown", "reported", "empty", "unknown-write"}
        for sid in subjects:
            con.execute("INSERT INTO sessions (session_id, harness) VALUES (?, 'claude')", [sid])
        models = [
            ("a", "complete", "known-2026-11-01"),
            ("b", "complete", "known-20261101"),
            ("c", "partial", "known"),
            ("d", "partial", "known-fast"),
            ("e", "unknown", None),
            ("f", "reported", "mystery"),
            ("g", "unknown-write", "no-write-rate"),
        ]
        for n, (eid, sid, model) in enumerate(models):
            event(con, eid, sid, n)
            con.execute(
                "INSERT INTO usage VALUES (?, 1000000, 2000000, 3000000, 4000000, ?)", [eid, model]
            )
        con.execute("INSERT INTO session_cost VALUES ('reported', 7.5, 'cost_state')")
        engine.ingest(con, config, [])
        assert dict(con.execute("SELECT session_id, cost_usd FROM session_cost").fetchall()) == {
            "complete": pytest.approx((2 + 0.2 * 3 + 3 * 4 + 10 * 2) * 3),
            "partial": pytest.approx(2 + 0.2 * 3 + 3 * 4 + 10 * 2),
            "unknown": 0,
            "reported": 7.5,
            "unknown-write": 0,
        }
        assert dict(con.execute("SELECT session_id, method FROM session_cost").fetchall()) == {
            "complete": "price_table",
            "partial": "partial_price_table",
            "unknown": "unpriced",
            "reported": "cost_state",
            "unknown-write": "unpriced",
        }
        collected = status.collect(con, config, ["claude"], [])
        assert collected["pricing_coverage"] == pricing.coverage(con)
        assert collected["unpriced_models"] == pricing.unpriced(con)
        missing = pricing.unpriced(con)
        assert {row["model"] for row in missing} == {"known-fast", None, "mystery", "no-write-rate"}
        assert {row["tokens"] for row in missing} == {10000000}
        assert pricing.coverage(con) == [
            {
                "harness": "claude",
                "sessions": len(subjects),
                "priced_sessions": len({"complete", "reported"}),
                "partial_sessions": len({"partial"}),
                "unpriced_sessions": len({"unknown", "empty", "unknown-write"}),
                "known_cost_usd": pytest.approx(145.9),
            }
        ]
        con.execute(runner._sql("rollups"))
        assert con.execute(
            "SELECT total_cost_usd, unpriced_sessions, cost_status FROM rollup_by_lane"
        ).fetchall() == [
            (pytest.approx(145.9), len({"partial", "unknown", "empty", "unknown-write"}), "partial")
        ]
        pricing.refresh(con, replace(config, pricing={"known": ModelPrice(1, 1, 1, 1)}))
        assert con.execute(
            "SELECT cost_usd FROM session_cost WHERE session_id='partial'"
        ).fetchone() == (10.0,)
        assert con.execute(
            "SELECT cost_usd FROM session_cost WHERE session_id='reported'"
        ).fetchone() == (7.5,)
        override = replace(config, pricing=config.pricing | {"known-fast": ModelPrice(1, 1, 1, 1)})
        current = status.collect(con, override, ["claude"], [])
        assert {row["model"] for row in current["unpriced_models"]} == {
            None,
            "mystery",
            "no-write-rate",
        }


def test_titles_custom_wins_latest_wins_and_invalid_titles_do_not_replace() -> None:
    with database() as con:
        for sid in ("claude", "codex", "untitled"):
            con.execute("INSERT INTO sessions (session_id, harness) VALUES (?, ?)", [sid, sid])
        for n, (sid, kind, raw) in enumerate(
            [
                ("claude", "ai-title", {"aiTitle": "first AI"}),
                ("claude", "custom-title", {"customTitle": "first custom"}),
                ("claude", "ai-title", {"aiTitle": "later AI"}),
                ("claude", "custom-title", {"customTitle": "latest custom"}),
                ("claude", "custom-title", {"customTitle": []}),
                ("codex", "event_msg:thread_name_updated", {"payload": {"thread_name": "first"}}),
                ("codex", "event_msg:thread_name_updated", {"payload": {"thread_name": "latest"}}),
            ]
        ):
            event(con, str(n), sid, n, kind, raw)
        engine.ingest(con, load_config(), [])
        engine.ingest(con, load_config(), [])
        assert dict(con.execute("SELECT session_id, title FROM sessions").fetchall()) == {
            "claude": "latest custom",
            "codex": "latest",
            "untitled": None,
        }


def test_explicit_pr_link_deduplicates_inferred_link_and_marks_source() -> None:
    with database() as con:
        con.execute("INSERT INTO sessions (session_id, harness) VALUES ('s', 'claude')")
        event(con, "direct", "s", 2, "pr-link", {"prUrl": "https://github.com/Owner/Repo/pull/42"})
        event(
            con, "duplicate", "s", 3, "pr-link", {"prUrl": "https://github.com/Owner/Repo/pull/42"}
        )
        event(con, "pasted", "s", 4, "user", {"prUrl": "https://github.com/Owner/Repo/pull/99"})
        event(con, "bad", "s", 5, "pr-link", {"prUrl": "https://evil.invalid/Owner/Repo/pull/99"})
        event(con, "use", "s", 0)
        event(con, "result", "s", 1)
        con.execute(
            "INSERT INTO tool_calls (session_id, call_id, use_event_id, result_event_id, "
            "tool_name, input, result_text) VALUES ('s','call','use','result','Bash', "
            "'{\"command\":\"gh pr create\"}', 'https://github.com/owner/repo/pull/42')"
        )
        linkage.link(con, "", batch_rows=2)
        assert con.execute(
            "SELECT session_id, repo, pr_number, method, source_event_id FROM session_links"
        ).fetchall() == [("s", "owner/repo", 42, "pr_link", "direct")]


@pytest.mark.parametrize(
    ("harness", "parent", "open_commands"),
    [
        ("claude", None, ["claude --resume s"]),
        (
            "claude",
            "parent-main",
            ["claude --resume parent-main", "less /transcripts/source.jsonl"],
        ),
        ("codex", "parent-main", ["codex resume s"]),
        ("droid", "parent-main", ["droid --resume s"]),
    ],
)
def test_finding_navigation_explanation_and_collapsed_escaped_evidence(
    harness: Harness, parent: str | None, open_commands: list[str]
) -> None:
    with database() as con:
        con.execute(
            "INSERT INTO sessions (session_id, harness, title, project, git_branch, "
            "started_at, ended_at, source_file_id, parent_session_id) "
            "VALUES ('s', ?, '<session-title>', 'project', 'feature', "
            "'2026-11-10 12:00:00Z', '2026-11-10 12:02:00Z', 1, ?)",
            [harness, parent],
        )
        con.execute("INSERT INTO session_cost VALUES ('s', 12.5, 'partial_price_table')")
        signal: Signal = {
            "signal_id": "signal",
            "kind": "rerun_loop",
            "session_id": "s",
            "harness": harness,
            "project": "project",
            "severity": 2.0,
            "evidence_event_ids": ["ev"],
            "details": {"command": "<cmd>", "runs": 6},
        }
        # The annotation verifies the typed JSON boundary too.
        finding = findings.enrich(con, signal)
        assert finding["open_commands"] == open_commands
        assert finding["parent_session_id"] == parent
        assert finding["evidence_commands"] == ["slopsniffer rehydrate ev"]
        assert finding["title"] == "<session-title>"
        assert finding["duration_seconds"] == 120
        assert finding["cost_status"] == "partial"
        assert "<cmd>" in str(finding["explanation"]) and "6 times" in str(finding["explanation"])
        con.execute(runner._sql("rollups"))
        con.execute(
            "INSERT INTO signals VALUES ('signal', 'rerun_loop', 's', 2, ['ev'], ?, now())",
            [json.dumps(signal["details"])],
        )
        con.execute(
            "INSERT INTO signals VALUES ('info', 'context_overflow', 's', 0, [], '{}', now())"
        )
        document = report.build(con, load_rubrics(), datetime(2026, 11, 12, tzinfo=UTC))
        [group] = document["sections"]["top_findings"]
        assert {k: group[k] for k in finding if k != "ranking_basis"} == {
            k: v for k, v in finding.items() if k != "ranking_basis"
        }
        assert group["worst_sessions"] == [finding]
        assert group["session_count"] == len({signal["session_id"]})
        assert group["affected_sessions_cost_usd"] == 12.5
        assert group["affected_sessions_unpriced"] == len({signal["session_id"]})
        page = html.render(document, {})
        assert "<details>" in page and "<details open" not in page
        assert "<session-title>" not in page and "&lt;session-title&gt;" in page
        assert "<cmd>" not in page and "&lt;cmd&gt;" in page
        assert "$12.50" in page
        if harness == "claude" and parent:
            assert f"parent: {parent}" in page
        else:
            assert "parent: " not in page


def test_repricing_history_ignores_large_bodies_under_a_small_memory_pool() -> None:
    with database() as con:
        con.execute("INSERT INTO sessions (session_id, harness) VALUES ('s', 'claude')")
        con.execute(
            "INSERT INTO events (event_id, session_id, seq, record_type, kind, "
            "source_file_id, byte_offset, byte_length, parsed) "
            "SELECT 'bulk-' || i, 's', i, 'assistant', 'message', 1, i * 5000, 5000, "
            "json_object('irrelevant', repeat('x', 4096)) FROM range(10000) t(i)"
        )
        con.execute("INSERT INTO usage SELECT event_id, 100, 50, 30, 20, 'gpt-5.6-sol' FROM events")
        pricing.refresh(con, load_config())
        assert con.execute("SELECT session_id, cost_usd, method FROM session_cost").fetchall() == [
            ("s", pytest.approx(15.12), "price_table")
        ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"output":"bad","metadata":{"exit_code":1}}', True),
        ('{"exit_code":-15}', True),
        ('{"output":"example exit_code: 1","metadata":{"exit_code":0}}', False),
        ('{"output":"exit_code: 1"}', False),
        ("Process exited with code -15", True),
        ("Process exited with code 0", False),
    ],
)
def test_structured_exit_status_is_authoritative(text: str, expected: bool) -> None:
    assert runner._failed_result(text, None) is expected


@pytest.mark.parametrize(
    ("phrase", "context", "feedback"),
    [
        ("that's wrong", "Earlier context " * 100, "That's wrong: fix the actual response."),
        (
            "that is wrong",
            'An "it is fine" that is wrong is worse than UNKNOWN. '
            + "Earlier context " * 100
            + "\n",
            "That is wrong: fix the actual response.",
        ),
    ],
)
def test_correction_excerpt_quotes_own_text_near_the_matched_phrase(
    phrase: str, context: str, feedback: str
) -> None:
    text = (
        "<system-reminder>"
        + "PRIVATE instruction " * 100
        + "that's wrong: injected example.</system-reminder>"
        + "<INSTRUCTIONS>PRIVATE that's wrong: quoted instructions.</INSTRUCTIONS>"
        + context
        + feedback
    )
    excerpt = findings.correction_excerpt(text, [phrase])
    assert feedback in excerpt
    assert "PRIVATE" not in excerpt


def test_stale_pickle_finding_has_metadata_an_honest_explanation_and_an_open_path() -> None:
    with database() as con:
        con.execute(
            "INSERT INTO pickle_runs (pickle_run_id, session_dir, active, step, "
            "current_ticket, start_time_epoch, working_dir, last_log_write_at, "
            "source_file_id) VALUES ('run', '/tmp/pickle-run', true, 'implementing', "
            "'fix-report', 1700000000, '/work/project', to_timestamp(1700000120), 1)"
        )
        signal: Signal = {
            "signal_id": "pickle-signal",
            "kind": "orphaned_session",
            "session_id": "run",
            "harness": "pickle",
            "project": "project",
            "severity": 1.0,
            "evidence_event_ids": [],
            "details": {"reason": "stale_pickle_run", "last_log_write_at": "yesterday"},
        }
        finding = findings.enrich(con, signal)
        assert finding["title"] == "Pickle fix-report · implementing"
        assert finding["started_at"] == "2023-11-14T22:13:20Z"
        assert finding["duration_seconds"] == 120
        assert finding["source_path"] == "/transcripts/source.jsonl"
        assert finding["open_commands"] == ["cat /transcripts/source.jsonl"]
        assert "still marked active" in str(finding["explanation"])
        assert "unanswered tool call" not in str(finding["explanation"])


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pytest", True),
        ("cd /repo && uv run pytest -q", True),
        ("timeout 30 pnpm test:unit", True),
        ("python3.14 -m unittest", True),
        ("cat missing/test.py", False),
        ("ls build", False),
        ("echo '; pytest -q'", False),
        ("cd 'missing && pytest -q'", False),
    ],
)
def test_verification_requires_a_command_position(command: str, expected: bool) -> None:
    with database() as con:
        assert con.execute(
            "SELECT regexp_matches(?, ?)", [command, runner.VERIFICATION_COMMAND]
        ).fetchone() == (expected,)


def test_displayed_open_command_resolves_a_quoted_home_path(isolated_home: Path) -> None:
    from slopsniffer.report.paths import display_text

    path = isolated_home / "evidence with $literal.txt"
    path.write_text("evidence", encoding="utf-8")
    command = display_text("cat " + findings._shell_path(str(path)))
    output = subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=True)
    assert output.stdout == "evidence"
