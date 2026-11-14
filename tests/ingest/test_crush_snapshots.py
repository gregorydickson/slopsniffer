"""A relocated export cache must still publish one logical SQLite session."""

import json
import os
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slopsniffer import pricing, report, status
from slopsniffer.config import load_config
from slopsniffer.db import open_write
from slopsniffer.grading.rubrics import load_rubrics
from slopsniffer.ingest import crush, engine
from slopsniffer.ingest.reader import event_id
from slopsniffer.signals import runner
from tests.ingest.crush_support import fixture_config


def test_moved_cache_keeps_session_cost_usage_and_rollup_identity(tmp_path: Path) -> None:
    config, database = fixture_config(tmp_path)
    config = replace(config, ingest=replace(config.ingest, memory_limit="32MB", threads=1))
    with open_write(config) as con:
        assert (
            engine.ingest(con, config, ["crush"], workers=1)["harnesses"]["crush"]["files_failed"]
            == []
        )
        ids = dict(con.execute("SELECT title,session_id FROM sessions").fetchall())
    moved = replace(config, data_dir=tmp_path / "moved-data")
    moved.data_dir.mkdir()
    shutil.copy2(config.db_path, moved.db_path)
    with sqlite3.connect(database) as db:
        # Equal second-resolution updated_at must not hide revised totals/title.
        db.execute(
            "UPDATE sessions SET title='Updated parent',cost=2.5,prompt_tokens=200 "
            "WHERE id='parent'"
        )
    with open_write(moved) as con:
        assert (
            engine.ingest(con, moved, ["crush"], workers=1)["harnesses"]["crush"]["files_failed"]
            == []
        )
        expected = sorted(
            [
                (ids["Parent title"], None, "Updated parent"),
                (ids["Child title"], ids["Parent title"], "Child title"),
            ]
        )
        assert (
            con.execute(
                "SELECT session_id,parent_session_id,title FROM sessions ORDER BY session_id"
            ).fetchall()
            == expected
        )
        assert con.execute(
            "SELECT session_id,cost_usd,method FROM session_cost ORDER BY session_id"
        ).fetchall() == sorted(
            [(ids["Parent title"], 2.5, "cost_state"), (ids["Child title"], 0.3, "cost_state")]
        )
        assert con.execute(
            "SELECT e.session_id,u.input_tokens,u.output_tokens FROM usage u "
            "JOIN events e USING(event_id) ORDER BY e.session_id"
        ).fetchall() == sorted([(ids["Parent title"], 200, 45), (ids["Child title"], 7, 3)])
        runner.compute(con, moved, None, datetime.now(UTC))
        [counts] = status.session_counts(con)
        assert counts["top_level_sessions"] == len({ids["Parent title"]})
        assert counts["sessions_including_subagents"] == len(set(ids.values()))
        [coverage] = pricing.coverage(con)
        assert coverage["sessions"] == len(set(ids.values()))
        document = report.build(con, load_rubrics(), datetime.now(UTC), moved)
        [lane] = document["sections"]["rollups"]["lane"]
        assert lane["top_level_sessions"] == counts["top_level_sessions"]
        assert lane["sessions_including_subagents"] == counts["sessions_including_subagents"]


@pytest.mark.parametrize("duplicate", ["sessions", "session_cost"])
def test_newest_snapshot_wins_and_complete_v4_duplicates_are_repaired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duplicate: str,
) -> None:
    sid = "crush:synthetic:session"
    paths = [tmp_path / "newer.jsonl", tmp_path / "older.jsonl"]
    for path, title, cost, tokens, stamp in [
        (paths[0], "Newer", 2.0, 20, 2000000000),
        (paths[1], "Older", 9.0, 90, 1000000000),
    ]:
        path.write_text(
            json.dumps(
                {
                    "type": "session",
                    "session_id": sid,
                    "project": "/synthetic/repo",
                    "database": "/synthetic/repo/.crush/crush.db",
                    "row": {
                        "id": "session",
                        "parent_session_id": None,
                        "title": title,
                        "cost": cost,
                        "prompt_tokens": tokens,
                        "completion_tokens": 2,
                        "created_at": 90,
                        "updated_at": 100,
                    },
                }
            )
            + "\n"
        )
        os.utime(path, ns=(stamp, stamp))
    source = engine.HarnessSource(crush.CrushParser(), lambda config: paths, crush.finalize)
    monkeypatch.setitem(engine.REGISTRY, "crush", source)
    config = load_config()
    config = replace(config, ingest=replace(config.ingest, memory_limit="32MB", threads=1))
    with open_write(config) as con:
        assert (
            engine.ingest(con, config, ["crush"], workers=1)["harnesses"]["crush"]["files_failed"]
            == []
        )
        newest_event = event_id(str(paths[0].resolve()), 0)
        newest_file = con.execute(
            "SELECT source_file_id FROM source_files WHERE path=?", [str(paths[0].resolve())]
        ).fetchone()
        assert newest_file is not None

        def canonical() -> None:
            assert con.execute(
                "SELECT session_id,title,source_file_id FROM sessions"
            ).fetchall() == [(sid, "Newer", newest_file[0])]
            assert con.execute("SELECT * FROM session_cost").fetchall() == [
                (sid, 2.0, "cost_state")
            ]
            assert con.execute(
                "SELECT event_id,input_tokens,output_tokens FROM usage"
            ).fetchall() == [(newest_event, 20, 2)]

        canonical()
        # Model a completed v4 publication from the old all-headers derivation.
        con.execute(f"INSERT INTO {duplicate} SELECT * FROM {duplicate}")
        before = con.execute(
            "SELECT event_id,parsed::VARCHAR FROM events ORDER BY event_id"
        ).fetchall()
        crush.finalize(con, config)
        canonical()
        assert (
            con.execute("SELECT event_id,parsed::VARCHAR FROM events ORDER BY event_id").fetchall()
            == before
        )

        # Repair is incremental: a clean replay does not need derivation again.
        def unexpected(*args: object) -> None:
            raise AssertionError("a clean session was re-derived")

        monkeypatch.setattr(crush, "_derive", unexpected)
        crush.finalize(con, config)
