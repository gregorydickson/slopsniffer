"""Real SQL regressions for byte-bounded Codex/Droid derivation."""

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest

from slopsniffer.config import load_config
from slopsniffer.db import apply_schema
from slopsniffer.ingest import codex, common, droid, engine, reader


@pytest.mark.parametrize("lane", ["codex", "droid"])
@pytest.mark.parametrize("bound", ["rows", "bytes", "scan"])
def test_large_lane_session_matches_generous_derivation(
    lane: str,
    bound: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if bound == "bytes":
        monkeypatch.setattr(reader, "BATCH_BYTES", 1024 * 1024)
    call_ids = {f"call-{i}" for i in range(1024 if bound == "scan" else 128)}
    parser = codex.CodexParser() if lane == "codex" else droid.DroidParser()
    finalize = codex.finalize if lane == "codex" else droid.finalize
    path = tmp_path / "large.jsonl"
    text = "x" * (16 * 1024)
    records: list[dict[str, Any]] = []
    if lane == "codex":
        records.append({"type": "session_meta", "payload": {"id": "large", "cwd": "/repo"}})
    else:
        records.append({"type": "session_start", "cwd": "/repo"})
    with path.open("w") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")
        for call in sorted(call_ids):
            if lane == "codex":
                pair = [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": call,
                            "name": "shell",
                            "arguments": '{"cmd":"pwd"}',
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": call,
                            "output": [
                                {
                                    "type": "ignored" if bound == "scan" else "output_text",
                                    "text": text,
                                }
                                for _ in range(8)
                            ],
                        },
                    },
                ]
            else:
                pair = [
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": call,
                                    "name": "shell",
                                    "input": {"cmd": "pwd"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "message",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": call,
                                    "content": [
                                        {
                                            "type": "ignored" if bound == "scan" else "text",
                                            "text": text,
                                        }
                                        for _ in range(8)
                                    ],
                                }
                            ],
                        },
                    },
                ]
            for record in pair:
                stream.write(json.dumps(record) + "\n")
    raw_events = common._json_events

    def bounded_events(
        connection: duckdb.DuckDBPyConnection,
        ids: list[str],
    ) -> Iterator[tuple[dict[str, Any], str, dict[str, Any]]]:
        sizes = [
            row[0]
            for row in connection.execute(
                "SELECT byte_length FROM lane_pending_events WHERE event_id IN "
                "(SELECT unnest($1::VARCHAR[]))",
                [ids],
            ).fetchall()
        ]
        assert len(sizes) <= config.ingest.batch_rows
        assert sum(sizes) <= reader.BATCH_BYTES + max(sizes)
        return raw_events(connection, ids)

    monkeypatch.setattr(common, "_json_events", bounded_events)
    snapshots = []
    for limit in ("512MB", "64MB"):
        config = load_config()
        config = replace(
            config,
            ingest=replace(
                config.ingest,
                memory_limit=limit,
                batch_rows=5000 if bound == "bytes" else 16,
                threads=1,
            ),
        )
        with duckdb.connect(
            str(tmp_path / f"{limit}.duckdb"),
            config={"memory_limit": "512MB", "threads": 1, "preserve_insertion_order": False},
        ) as con:
            apply_schema(con)
            con.execute(
                "INSERT INTO source_files (source_file_id, path, harness) VALUES (1, ?, ?)",
                [str(path), lane],
            )
            writer = engine._Writer(con, {1: engine._Tracked(str(path), engine._empty_counts())})
            offset = 0
            while offset < path.stat().st_size:
                batch = reader.read_batch(
                    reader.BatchRequest(str(path), 1, offset, path.stat().st_size, 16, parser)
                )
                assert writer.commit(batch)
                offset = batch.end
            con.execute("CHECKPOINT")
            con.execute(f"SET memory_limit = '{limit}'")
            finalize(con, config)
            assert set(con.execute("SELECT call_id FROM tool_calls").fetchall()) == {
                (call,) for call in call_ids
            }
            snapshots.append(
                {
                    table: set(con.execute(f"SELECT * FROM {table}").fetchall())
                    for table in ("sessions", "tool_calls", "usage", "session_cost")
                }
            )
    assert snapshots[0] == snapshots[1]
