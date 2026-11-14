"""Small database contracts for signals without ingesting the fleet corpus."""

from datetime import UTC, date, datetime
from importlib import resources
from importlib.resources.abc import Traversable

import pytest

from slopsniffer.config import load_config
from slopsniffer.contracts import SignalsReport
from slopsniffer.signals import runner
from tests.grading.support import memory_db

NOW = datetime(2026, 11, 11, tzinfo=UTC)


def test_empty_compute_keeps_zero_kinds_and_rolls_back_failed_replacements(
    monkeypatch: pytest.MonkeyPatch,
    non_utf8_text_defaults: None,
) -> None:
    files = resources.files

    class CaseSensitivePackage:
        def __init__(self, root: Traversable) -> None:
            self.root = root

        def joinpath(self, name: str) -> Traversable:
            # Model a case-sensitive filesystem, even when tests run on macOS.
            children = {child.name: child for child in self.root.iterdir()}
            if name not in children:
                raise FileNotFoundError(name)
            return children[name]

    def strict_files(anchor: str | None = None) -> CaseSensitivePackage:
        # resources.files(None) infers the calling module's package in production.
        return CaseSensitivePackage(files(anchor or "slopsniffer.signals"))

    config = load_config()
    with memory_db() as con:
        con.execute("SET threads = 1")
        monkeypatch.setattr(resources, "files", strict_files)
        summary = runner.compute(con, config, None, NOW)
        stored = con.execute("SELECT signal_id FROM signals").fetchall()
        assert stored == []
        assert summary == {
            "signals_written": len(stored),
            "by_kind": dict.fromkeys(runner.KINDS, len(stored)),
        }
        expected_empty: SignalsReport = {
            "window": {"since": None, "until": NOW.isoformat()},
            "signals": [],
            "rollups": {dimension: [] for dimension in ("lane", "skill", "project", "day", "week")},
        }
        assert runner.report(con, runner.Filters(), NOW) == expected_empty
        con.execute("INSERT INTO signals VALUES ('old', 'rerun_loop', 's', 2, [], NULL, ?)", [NOW])
        old = con.execute("SELECT signal_id, kind, severity FROM signals").fetchall()
        original = runner._sql

        def broken(name: str) -> str:
            if name == "prelude":
                raise ValueError("synthetic SQL resource failure")
            return original(name)

        monkeypatch.setattr(runner, "_sql", broken)
        with pytest.raises(ValueError, match="synthetic SQL resource failure"):
            runner.compute(con, config, None, NOW)
        assert con.execute("SELECT signal_id, kind, severity FROM signals").fetchall() == old
        con.execute("BEGIN TRANSACTION")
        con.execute("ROLLBACK")


def test_report_uses_utc_midnight_and_preserves_null_details_and_rollup_values() -> None:
    sessions = {"early": "2026-11-10T00:30:00Z", "older": "2026-11-09T23:30:00Z"}
    evidence = ["e-a", "e-b"]
    usage_tokens: list[int] = []
    with memory_db() as con:
        con.execute("SET threads = 1")
        con.execute("SET TimeZone = 'America/Chicago'")
        for sid, started in sessions.items():
            con.execute(
                "INSERT INTO sessions (session_id, harness, project, started_at, ended_at) "
                "VALUES (?, 'claude', 'tiny', CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ))",
                [sid, started, started],
            )
        runner.compute(con, load_config(), None, NOW)
        for signal_id, sid, details in [
            ("with-details", "early", '{"phrases":["wrong"]}'),
            ("without-details", "early", None),
            ("excluded", "older", '{"old":true}'),
        ]:
            con.execute(
                "INSERT INTO signals VALUES (?, 'user_correction', ?, 1.5, ?, ?, ?)",
                [signal_id, sid, evidence, details, NOW],
            )
        report = runner.report(con, runner.Filters(since=date(2026, 11, 10)), NOW)
        expected_signals = [
            {
                "signal_id": signal_id,
                "kind": "user_correction",
                "session_id": "early",
                "harness": "claude",
                "project": "tiny",
                "severity": 1.5,
                "evidence_event_ids": evidence,
                "details": details,
            }
            for signal_id, details in [
                ("with-details", {"phrases": ["wrong"]}),
                ("without-details", {}),
            ]
        ]
        assert report["signals"] == expected_signals
        assert report["window"] == {"since": "2026-11-10", "until": NOW.isoformat()}
        included = {s["session_id"] for s in expected_signals}
        assert report["rollups"] == {
            dim: [
                {
                    "key": key,
                    "sessions": len(included),
                    "top_level_sessions": len(included),
                    "sessions_including_subagents": len(included),
                    "worktree_count": 1,
                    "total_tokens": sum(usage_tokens),
                    "mean_tokens": sum(usage_tokens) / len(included),
                    "total_cost_usd": 0.0,
                    "cost_status": "partial",
                    "unpriced_sessions": len(included),
                    "wall_seconds": 0.0,
                    "signal_counts": {"user_correction": len(expected_signals)},
                }
            ]
            for dim, key in [
                ("lane", "claude"),
                ("project", "tiny"),
                ("day", "2026-11-10"),
                ("week", "2026-11-09"),
            ]
        } | {"skill": []}
        negative = runner.report(con, runner.Filters(project="missing"), NOW)
        assert negative["signals"] == []
        assert negative["rollups"] == {
            dim: [] for dim in ("lane", "skill", "project", "day", "week")
        }
