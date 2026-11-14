"""The writer's memory budget must not depend on persistent history indexes."""

import hashlib
from dataclasses import replace

import pytest

from slopsniffer.config import load_config
from slopsniffer.db import open_write
from slopsniffer.ingest import engine
from slopsniffer.ingest.reader import Batch, Event, Row


@pytest.mark.slow
def test_history_fits_default_writer_with_small_budget() -> None:
    config = load_config()
    config = replace(config, ingest=replace(config.ingest, memory_limit="64MB", threads=1))
    expected = 400_000
    with open_write(config) as con:
        con.execute("INSERT INTO source_files (path, harness) VALUES ('synthetic', 'claude')")
        writer = engine._Writer(con, {1: engine._Tracked("synthetic", engine._empty_counts())})
        for start in range(0, expected, config.ingest.batch_rows):
            batch = Batch(1, start + config.ingest.batch_rows)
            for n in range(start, batch.end):
                eid = hashlib.sha256(str(n).encode()).hexdigest()[:32]
                batch.events.append((eid, n, 1, Event("user", "message", session_id="session")))
                batch.rows.append(Row("messages", {"event_id": eid, "role": "user", "text": "hi"}))
                batch.rows.append(Row("usage", {"event_id": eid, "input_tokens": n}))
            assert writer.commit(batch), con.execute("SELECT error FROM ingest_errors").fetchall()
        assert con.execute("SELECT count(*), count(DISTINCT event_id) FROM events").fetchone() == (
            expected,
            expected,
        )
        for table in ("messages", "usage"):
            assert (
                con.execute(
                    f"SELECT event_id FROM {table} EXCEPT SELECT event_id FROM events"
                ).fetchall()
                == []
            )
            assert (
                con.execute(
                    f"SELECT event_id FROM events EXCEPT SELECT event_id FROM {table}"
                ).fetchall()
                == []
            )
        assert con.execute(
            "SELECT memory_usage_bytes FROM duckdb_memory() WHERE tag = 'ART_INDEX'"
        ).fetchone() == (0,)


def test_blob_deduplication_within_and_across_commits() -> None:
    config = load_config()
    with open_write(config) as con:
        con.execute("INSERT INTO source_files (path, harness) VALUES ('synthetic', 'claude')")
        writer = engine._Writer(con, {1: engine._Tracked("synthetic", engine._empty_counts())})
        for contents in (("shared", "shared", "first"), ("shared", "second", "second")):
            batch = Batch(1, 0)
            batch.rows = [
                Row("blobs", {"content_hash": hashlib.sha256(c.encode()).hexdigest(), "content": c})
                for c in contents
            ]
            assert writer.commit(batch)
        assert sorted(con.execute("SELECT content_hash, content FROM blobs").fetchall()) == sorted(
            (hashlib.sha256(c.encode()).hexdigest(), c) for c in ("shared", "first", "second")
        )
