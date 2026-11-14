import json
import sqlite3
from pathlib import Path
from typing import Any

import duckdb
import pytest

from slopsniffer.db import open_write
from slopsniffer.ingest import crush, engine
from tests.ingest.crush_support import fixture_config, message, part


def test_crush_rows_cost_parent_source_and_incremental_replacement(tmp_path: Path) -> None:
    config, database = fixture_config(tmp_path)
    with open_write(config) as con:
        first = engine.ingest(con, config, ["crush"], workers=1)
        assert first["harnesses"]["crush"]["files_failed"] == []
        sessions = con.execute(
            "SELECT session_id,parent_session_id,title,project,model FROM sessions"
        ).fetchall()
        by_title = {row[2]: row for row in sessions}
        parent, child = by_title["Parent title"], by_title["Child title"]
        assert {row[2] for row in sessions} == {"Parent title", "Child title"}
        assert child[1] == parent[0]
        assert parent[3:] == (str(database.parent.parent), "crush-model")
        assert set(
            con.execute("SELECT session_id,cost_usd,method FROM session_cost").fetchall()
        ) == {
            (parent[0], 1.25, "cost_state"),
            (child[0], 0.3, "cost_state"),
        }
        assert con.execute(
            "SELECT call_id,tool_name,result_text,is_error FROM tool_calls"
        ).fetchall() == [("call", "bash", "failed output", True)]
        assert set(con.execute("SELECT role,text FROM messages").fetchall()) == {
            ("user", "hello"),
            ("assistant", "running"),
            ("tool", None),
            ("assistant", "a summary"),
        }
        assert set(con.execute("SELECT input_tokens,output_tokens FROM usage").fetchall()) == {
            (123, 45),
            (7, 3),
        }
        assert con.execute("SELECT session_id FROM events WHERE kind='compaction'").fetchall() == [
            (child[0],)
        ]
        file_event = con.execute("SELECT event_id FROM events WHERE record_type='files'").fetchone()
        assert file_event is not None
        location = engine.locate_event(con, file_event[0])
        assert location is not None
        raw = engine.read_event(location)
        assert json.loads(raw)["row"]["content"] == "original bytes"
        before = con.execute("SELECT COLUMNS(*)::VARCHAR FROM events ORDER BY event_id").fetchall()
        again = engine.ingest(con, config, ["crush"], workers=1)
        assert again["harnesses"]["crush"]["events_written"] == 0
        assert (
            con.execute("SELECT COLUMNS(*)::VARCHAR FROM events ORDER BY event_id").fetchall()
            == before
        )
        with sqlite3.connect(database) as db:
            db.execute("UPDATE sessions SET updated_at=121,cost=2.5 WHERE id='parent'")
            db.execute(
                "UPDATE messages SET parts=?,updated_at=121 WHERE id='a1'",
                [
                    json.dumps(
                        [
                            part("text", text="updated"),
                            part(
                                "tool_call",
                                id="call",
                                name="bash",
                                input='{"command":"uv run pytest"}',
                            ),
                        ]
                    )
                ],
            )
        changed = engine.ingest(con, config, ["crush"], workers=1)
        assert changed["harnesses"]["crush"]["files_failed"] == []
        assert con.execute("SELECT call_id,input::VARCHAR FROM tool_calls").fetchall() == [
            ("call", '{"command":"uv run pytest"}')
        ]
        assert con.execute(
            "SELECT cost_usd FROM session_cost WHERE session_id=?", parameters=[parent[0]]
        ).fetchall() == [(2.5,)]
        assert set(con.execute("SELECT role,text FROM messages").fetchall()) == {
            ("user", "hello"),
            ("assistant", "updated"),
            ("tool", None),
            ("assistant", "a summary"),
        }
        assert not con.execute("SELECT * FROM sessions WHERE harness<>'crush'").fetchall()
        assert (
            len({r[0] for r in con.execute("SELECT event_id FROM events").fetchall()})
            == con.execute("SELECT count(*) FROM events").fetchall()[0][0]
        )


def test_backup_includes_wal_and_does_not_hold_writer_during_export(tmp_path: Path) -> None:
    config, database = fixture_config(tmp_path)
    with sqlite3.connect(database, timeout=0) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE sessions SET title='in WAL' WHERE id='parent'")
        writer.commit()
        with crush.snapshot(database) as snapshot:
            writer.execute("UPDATE sessions SET title='after snapshot' WHERE id='parent'")
            writer.commit()
            assert (
                snapshot.execute("SELECT title FROM sessions WHERE id='parent'").fetchone()[0]
                == "in WAL"
            )
        assert (
            writer.execute("SELECT title FROM sessions WHERE id='parent'").fetchone()[0]
            == "after snapshot"
        )
    assert list(crush.discover(config))


def test_same_second_message_updates_and_failed_snapshot_are_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, database = fixture_config(tmp_path)
    with open_write(config) as con:
        engine.ingest(con, config, ["crush"], workers=1)
        with sqlite3.connect(database) as db:
            message(
                db,
                "parent",
                "late",
                "assistant",
                [part("text", text="late same-second message")],
                when=102,
            )
        engine.ingest(con, config, ["crush"], workers=1)
        assert ("late same-second message",) in con.execute("SELECT text FROM messages").fetchall()

        def fail(path: Path) -> object:
            raise OSError("denied snapshot")

        monkeypatch.setattr(crush, "snapshot", fail)
        summary = engine.ingest(con, config, ["crush"], workers=1)
        assert any(
            "denied snapshot" in path for path in summary["harnesses"]["crush"]["files_failed"]
        )
        assert ("late same-second message",) in con.execute("SELECT text FROM messages").fetchall()


def test_absent_crush_index_is_noop(tmp_path: Path) -> None:
    config, _ = fixture_config(tmp_path)
    config.ingest.sources.crush_projects.unlink()
    assert list(crush.discover(config)) == []


@pytest.mark.parametrize("rows", [3, 16])
def test_crush_uses_writer_budgets_at_small_memory_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: int
) -> None:
    from dataclasses import replace

    from slopsniffer.ingest import common, reader

    config, database = fixture_config(tmp_path)
    with sqlite3.connect(database) as db:
        for i in range(128):
            message(
                db,
                "parent",
                f"wide-{i}",
                "assistant",
                [
                    part("text", text=f"body-{i}:" + "x" * 16000),
                    part(
                        "tool_call",
                        id=f"call-{i}",
                        name="edit",
                        input=json.dumps({"file_path": f"file-{i}.py"}),
                    ),
                    part(
                        "tool_result",
                        tool_call_id=f"call-{i}",
                        content=f"result-{i}",
                        is_error=False,
                    ),
                ],
                when=200 + i,
            )
        db.execute("UPDATE sessions SET updated_at=500 WHERE id='parent'")
    config = replace(config, ingest=replace(config.ingest, memory_limit="64MB", batch_rows=rows))
    monkeypatch.setattr(reader, "BATCH_BYTES", 40000)
    requested: list[tuple[int, int, int]] = []
    original = common._json_events

    def observe(con: duckdb.DuckDBPyConnection, ids: list[str]) -> Any:
        size, largest = con.execute(
            "SELECT sum(byte_length), max(byte_length) FROM lane_pending_events "
            "WHERE event_id IN (SELECT unnest(?::VARCHAR[]))",
            [ids],
        ).fetchall()[0]
        requested.append((len(ids), size, largest))
        return original(con, ids)

    monkeypatch.setattr(common, "_json_events", observe)
    with open_write(config) as con:
        result = engine.ingest(con, config, ["crush"], workers=1)
        assert result["harnesses"]["crush"]["files_failed"] == []
        assert set(
            con.execute(
                "SELECT call_id,result_text FROM tool_calls WHERE call_id<>'call'"
            ).fetchall()
        ) == {(f"call-{i}", f"result-{i}") for i in range(128)}
        assert requested
        assert all(
            n <= config.ingest.batch_rows and size <= reader.BATCH_BYTES + largest
            for n, size, largest in requested
        )


def test_two_project_databases_with_same_ids_do_not_collide(tmp_path: Path) -> None:
    from tests.ingest.crush_support import fixture_db

    config, first = fixture_config(tmp_path)
    second = tmp_path / "second/.crush/crush.db"
    fixture_db(second)
    index = config.ingest.sources.crush_projects
    entries = json.loads(index.read_text())
    entries["projects"].append({"path": str(second.parent.parent), "data_dir": str(second.parent)})
    index.write_text(json.dumps(entries))
    with open_write(config) as con:
        result = engine.ingest(con, config, ["crush"], workers=1)
        assert result["harnesses"]["crush"]["files_failed"] == []
        assert set(con.execute("SELECT project,title FROM sessions").fetchall()) == {
            (str(db.parent.parent), title)
            for db in (first, second)
            for title in ("Parent title", "Child title")
        }
        assert set(
            con.execute(
                "SELECT s.project,p.project FROM sessions s JOIN sessions p "
                "ON s.parent_session_id=p.session_id"
            ).fetchall()
        ) == {(str(db.parent.parent), str(db.parent.parent)) for db in (first, second)}


def test_export_failure_keeps_the_last_complete_rows_and_names_the_database(tmp_path: Path) -> None:
    config, database = fixture_config(tmp_path)
    with open_write(config) as con:
        engine.ingest(con, config, ["crush"], workers=1)
        exports = {p: p.read_bytes() for p in (config.data_dir / "sources/crush").rglob("*.jsonl")}
        before = con.execute(
            "SELECT event_id,parsed::VARCHAR FROM events ORDER BY event_id"
        ).fetchall()
        with sqlite3.connect(database) as db:
            db.execute("UPDATE sessions SET updated_at=999 WHERE id='parent'")
            db.execute("UPDATE files SET content=? WHERE id='f'", [b"unsupported blob"])
        result = engine.ingest(con, config, ["crush"], workers=1)
        assert any(
            str(database) in failure for failure in result["harnesses"]["crush"]["files_failed"]
        )
        assert (
            con.execute("SELECT event_id,parsed::VARCHAR FROM events ORDER BY event_id").fetchall()
            == before
        )
        assert {p: p.read_bytes() for p in exports} == exports
        assert not list((config.data_dir / "sources/crush").rglob("*.tmp"))


def test_snapshot_deadline_fails_visibly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    config, _ = fixture_config(tmp_path)
    times = iter([0.0, 100.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(times))
    with pytest.raises(engine.SourceError, match="snapshot timed out"):
        list(crush.discover(config))


def test_crush_totals_publish_after_body_batches_and_replay_after_process_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import subprocess
    import sys
    from dataclasses import replace

    config, _ = fixture_config(tmp_path)
    monkeypatch.setitem(engine.REGISTRY, "crush", replace(crush.source(), finalize=None))
    with open_write(config) as con:
        result = engine.ingest(con, config, ["crush"], workers=1)
        assert result["harnesses"]["crush"]["files_failed"] == []
    path = tmp_path / "config.toml"
    path.write_text("[ingest]\nbatch_rows=1\nthreads=1\n")
    script = """
import os,sys,duckdb
from slopsniffer.config import load_config
from slopsniffer.ingest import crush
config=load_config(sys.argv[1])
con=duckdb.connect(str(config.db_path),config={'memory_limit':'512MB','threads':1})
class Crash:
    def __getattr__(self,name): return getattr(con,name)
    def commit(self):
        con.commit()
        if con.execute('SELECT use_event_id FROM tool_calls LIMIT 1').fetchone():
            os._exit(73)
crush.finalize(Crash(),config)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        timeout=30,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
    )
    assert child.returncode == 73, child.stderr
    with open_write(config) as con:
        assert (
            con.execute(
                "SELECT session_id FROM sessions WHERE session_id LIKE '%:parent'"
            ).fetchall()
            == []
        )
        assert (
            con.execute(
                "SELECT u.event_id FROM usage u JOIN events e USING(event_id) "
                "WHERE e.session_id LIKE '%:parent'"
            ).fetchall()
            == []
        )
        assert con.execute("SELECT call_id FROM tool_calls").fetchall() == [("call",)]
        crush.finalize(con, config)
        assert set(con.execute("SELECT title FROM sessions").fetchall()) == {
            ("Parent title",),
            ("Child title",),
        }
        assert set(con.execute("SELECT input_tokens,output_tokens FROM usage").fetchall()) == {
            (123, 45),
            (7, 3),
        }
        assert con.execute("SELECT call_id,result_text,is_error FROM tool_calls").fetchall() == [
            ("call", "failed output", True)
        ]
        before = con.execute("SELECT * FROM usage ORDER BY event_id").fetchall()
        con.execute("UPDATE events SET seq=NULL WHERE session_id LIKE '%:parent'")
        crush.finalize(con, config)
        assert con.execute("SELECT * FROM usage ORDER BY event_id").fetchall() == before


def test_session_only_updates_preserve_authoritative_cost_and_mixed_models(tmp_path: Path) -> None:
    config, database = fixture_config(tmp_path)
    with open_write(config) as con:
        engine.ingest(con, config, ["crush"], workers=1)
        with sqlite3.connect(database) as db:
            db.execute(
                "UPDATE sessions SET title='Renamed',cost=9.75,updated_at=1000 WHERE id='parent'"
            )
        engine.ingest(con, config, ["crush"], workers=1)
        assert con.execute(
            "SELECT s.title,c.cost_usd FROM sessions s JOIN session_cost c USING(session_id) "
            "WHERE s.session_id LIKE '%:parent'"
        ).fetchall() == [("Renamed", 9.75)]
        assert con.execute(
            "SELECT epoch(started_at)::BIGINT,epoch(ended_at)::BIGINT "
            "FROM sessions WHERE title='Renamed'"
        ).fetchall() == [(90, 1000)]
        with sqlite3.connect(database) as db:
            message(
                db,
                "parent",
                "new-model",
                "assistant",
                [part("text", text="different model")],
                when=1001,
            )
            db.execute("UPDATE messages SET model='other-model' WHERE id='new-model'")
        engine.ingest(con, config, ["crush"], workers=1)
        assert con.execute(
            "SELECT u.model FROM usage u JOIN events e USING(event_id) "
            "WHERE e.session_id LIKE '%:parent'"
        ).fetchall() == [(None,)]
        reads = con.execute(
            "SELECT parsed::VARCHAR FROM events WHERE record_type='read_files'"
        ).fetchall()
        assert {json.loads(raw)["row"]["path"] for (raw,) in reads} == {"y.py"}


@pytest.mark.parametrize("table", ["messages", "files", "read_files"])
def test_orphan_rows_fail_visibly_instead_of_disappearing(tmp_path: Path, table: str) -> None:
    config, database = fixture_config(tmp_path)
    with sqlite3.connect(database) as db:
        if table == "messages":
            message(
                db, "missing-session", "orphan", "user", [part("text", text="must not disappear")]
            )
        elif table == "files":
            db.execute("INSERT INTO files VALUES ('orphan','missing-session','x','body',1,0,0)")
        else:
            db.execute("INSERT INTO read_files VALUES ('missing-session','x',0)")
    with pytest.raises(engine.SourceError, match=f"orphaned {table} row"):
        list(crush.discover(config))


def test_unindexed_home_database_is_included_when_the_index_exists(
    tmp_path: Path, isolated_home: Path
) -> None:
    from tests.ingest.crush_support import fixture_db

    config, _ = fixture_config(tmp_path)
    fixture_db(isolated_home / ".crush/crush.db")
    with open_write(config) as con:
        result = engine.ingest(con, config, ["crush"], workers=1)
        assert result["harnesses"]["crush"]["files_failed"] == []
        assert (str(isolated_home), "Parent title") in con.execute(
            "SELECT project,title FROM sessions"
        ).fetchall()


def test_default_ingest_inventories_skills_from_new_crush_projects(tmp_path: Path) -> None:
    from typing import get_args

    from slopsniffer.contracts import Harness

    config, database = fixture_config(tmp_path)
    project = database.parent.parent
    (project / ".git").mkdir()
    skill = project / ".claude/skills/fresh/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: fresh\n---\n")
    with open_write(config) as con:
        summary = engine.ingest(con, config, get_args(Harness), workers=1)
        assert all(not counts["files_failed"] for counts in summary["harnesses"].values())
        found = set(con.execute("SELECT name,scope,repo FROM skills").fetchall())
        assert found == {("fresh", "project", str(project))}
        assert summary["harnesses"]["skills"]["files_seen"] == len(found)
