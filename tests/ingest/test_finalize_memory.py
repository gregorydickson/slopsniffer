"""Finalization is session-bounded and interrupted derivation is replayable."""

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import pytest

from slopsniffer.cli import main
from slopsniffer.config import Config, load_config
from slopsniffer.contracts import Harness
from slopsniffer.ingest import claude, codex, common, droid, engine
from tests.cli.support import validate
from tests.ingest.support import jsonl, query


def corpus(root: Path, harness: Harness, *, large: bool) -> tuple[set[str], set[tuple[str, str]]]:
    root.mkdir()
    sessions, calls = set(), set()
    for n in range(32 if large else 3):
        sid = f"session-{n:03}"
        sessions.add(sid)
        records: list[dict[str, Any]] = []
        if harness == "codex":
            records.extend(
                [
                    {"type": "session_meta", "payload": {"id": sid, "cwd": "/work/project"}},
                    {"type": "turn_context", "payload": {"model": "gpt-5"}},
                ]
            )
        for i in range(128 if large else 2):
            call = f"call-{i}"
            calls.add((sid, call))
            payload = f"{sid}/{call}:" + "x" * (4096 if large else 8)
            if harness in ("claude", "droid"):
                records.extend(
                    [
                        {
                            "type": "assistant",
                            "sessionId": sid,
                            "message": {
                                "model": "claude-sonnet-4-5",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": call,
                                        "name": "shell",
                                        "input": {"cmd": payload},
                                    },
                                    {"type": "text", "text": payload},
                                ],
                            },
                        },
                        {
                            "type": "user",
                            "sessionId": sid,
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": call,
                                        "content": payload,
                                    },
                                ]
                            },
                        },
                    ]
                )
            else:
                records.extend(
                    [
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "function_call",
                                "call_id": call,
                                "name": "shell",
                                "arguments": json.dumps({"cmd": payload}),
                            },
                        },
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": call,
                                "output": payload,
                            },
                        },
                    ]
                )
        # A result without a use must never become a call.
        if harness in ("claude", "droid"):
            records.append(
                {
                    "type": "user",
                    "sessionId": sid,
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "orphan",
                                "content": "unmatched",
                            },
                        ]
                    },
                }
            )
        else:
            records.append(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "orphan",
                        "output": "unmatched",
                    },
                }
            )
        if harness == "droid":
            records = [{"type": "session_start", "cwd": "/work/project"}] + [
                {
                    **record,
                    "type": "message",
                    "message": {
                        **record["message"],
                        "role": "assistant" if record["type"] == "assistant" else "user",
                    },
                }
                for record in records
            ]
        (root / f"{sid}.jsonl").write_bytes(jsonl(*records))
    return sessions, calls


def run(capsys: pytest.CaptureFixture[str], config: Path, harness: Harness) -> tuple[int, Any]:
    code = main(["ingest", "--harness", harness, "--config", str(config), "--json"])
    doc = json.loads(capsys.readouterr().out)
    validate("ingest", doc)
    return code, doc


def snapshot(data: Path) -> dict[str, set[tuple[Any, ...]]]:
    return {
        table: set(query(f"SELECT * FROM {table}", data_dir=data))
        for table in ("sessions", "tool_calls", "usage", "session_cost")
    }


@pytest.mark.parametrize("harness", ["claude", "codex", "droid"])
def test_many_sessions_fit_low_memory_and_match_generous_derivation(
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "corpus"
    sessions, calls = corpus(root, harness, large=True)
    source = {"claude": claude.source, "codex": codex.source, "droid": droid.source}[harness](root)
    monkeypatch.setitem(engine.REGISTRY, harness, source)
    results = []
    config = tmp_path / "config.toml"
    for limit in ("512MB", "64MB"):
        data = tmp_path / limit
        monkeypatch.setenv("SLOPSNIFFER_HOME", str(data))
        config.write_text(f'[ingest]\nmemory_limit = "{limit}"\nworkers = 1\nbatch_rows = 128\n')
        code, doc = run(capsys, config, harness)
        assert (code, doc["harnesses"][harness]["files_failed"]) == (0, [])
        assert set(query("SELECT session_id FROM sessions", data_dir=data)) == {
            (sid,) for sid in sessions
        }
        assert set(query("SELECT session_id, call_id FROM tool_calls", data_dir=data)) == calls
        results.append(snapshot(data))
    assert results[0] == results[1]


@pytest.mark.parametrize("harness", ["claude", "codex", "droid"])
@pytest.mark.parametrize("phase", ["entry", "exit"])
def test_finalize_oom_reports_files_and_resumes_identically(
    phase: str,
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "corpus"
    sessions, calls = corpus(root, harness, large=False)
    # A separate parse error must remain counted when finalize adds its own failure.
    parse_error_path = root / "session-000.jsonl"
    with parse_error_path.open("ab") as stream:
        stream.write(b"{malformed\n")
    monkeypatch.setitem(
        engine.REGISTRY,
        harness,
        {"claude": claude.source, "codex": codex.source, "droid": droid.source}[harness](root),
    )
    config = tmp_path / "config.toml"
    config.write_text("[ingest]\nworkers = 1\n")
    original = common._session

    @contextmanager
    def interrupted(
        con: duckdb.DuckDBPyConnection, lane: Harness, sid: str, sequence: str
    ) -> Iterator[None]:
        if phase == "entry" and sid == "session-001":
            raise duckdb.OutOfMemoryException("injected finalize OOM")
        with original(con, lane, sid, sequence):
            yield
            if phase == "exit" and sid == "session-001":
                raise duckdb.OutOfMemoryException("injected finalize OOM")

    with monkeypatch.context() as patch:
        patch.setattr(common, "_session", interrupted)
        code, doc = run(capsys, config, harness)
    assert code == 3
    assert set(doc["harnesses"][harness]["files_failed"]) == {
        str(p.resolve()) for p in root.glob("*.jsonl") if p.stem == "session-001"
    }
    errors = dict(
        query(
            "SELECT f.path, i.error FROM ingest_errors i JOIN source_files f USING (source_file_id)"
        )
    )
    failed_path = str((root / "session-001.jsonl").resolve())
    assert set(errors) == {failed_path, str(parse_error_path.resolve())}
    assert errors[failed_path].startswith("finalize session session-001: OutOfMemoryException:")
    assert "injected finalize OOM" in errors[failed_path]
    assert any(
        harness in record.getMessage() and "session-001" in record.getMessage()
        for record in caplog.records
        if record.exc_info is not None
    )
    assert doc["harnesses"][harness]["ingest_errors"] == len(errors)
    assert "injected finalize OOM" in caplog.text
    assert set(query("SELECT session_id FROM sessions")) == {
        (sid,) for sid in sessions - {"session-001"}
    }
    assert set(
        query("SELECT DISTINCT session_id FROM events WHERE session_id IS NOT NULL AND seq IS NULL")
    ) == {("session-001",)}
    assert set(query("SELECT session_id, call_id FROM tool_calls")) == {
        call for call in calls if call[0] != "session-001"
    }
    assert set(query("SELECT path, committed_offset FROM source_files")) == {
        (str(p.resolve()), p.stat().st_size) for p in root.glob("*.jsonl")
    }
    code, doc = run(capsys, config, harness)
    assert (code, doc["harnesses"][harness]["files_failed"]) == (0, [])
    assert set(query("SELECT session_id, call_id FROM tool_calls")) == calls
    resumed = snapshot(load_config().data_dir)
    clean = tmp_path / "clean"
    monkeypatch.setenv("SLOPSNIFFER_HOME", str(clean))
    code, doc = run(capsys, config, harness)
    assert (code, doc["harnesses"][harness]["files_failed"]) == (0, [])
    assert resumed == snapshot(clean)


@pytest.mark.parametrize(
    "command",
    ["signals", "outcomes", "grade", "propose", "report", "query", "rehydrate", "status", "run"],
)
def test_database_errors_are_partial_json(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from slopsniffer import cli

    def fail(args: Any, ctx: Any) -> int:
        raise duckdb.OutOfMemoryException("injected command OOM")

    from dataclasses import replace

    monkeypatch.setitem(cli.COMMANDS, command, replace(cli.COMMANDS[command], handler=fail))
    extra = {"grade": ["--worst", "1"], "query": ["SELECT 1"], "rehydrate": ["event"]}.get(
        command, []
    )
    assert main([command, *extra, "--json"]) == 3
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    validate("error", doc)
    assert (doc["error"], doc["command"], doc["message"]) == (
        "database_error",
        command,
        "injected command OOM",
    )
    assert "injected command OOM" in captured.err


def test_database_open_error_still_emits_ingest_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from slopsniffer import cli

    @contextmanager
    def fail(config: Config) -> Iterator[duckdb.DuckDBPyConnection]:
        raise duckdb.IOException("injected open failure")
        yield  # pragma: no cover

    monkeypatch.setattr(cli, "open_write", fail)
    assert main(["ingest", "--harness", "claude,codex", "--json"]) == 3
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    validate("ingest", doc)
    assert {h: row["files_failed"] for h, row in doc["harnesses"].items()} == {
        h: [f"{h}: database unavailable: injected open failure"] for h in ("claude", "codex")
    }
    assert "injected open failure" in captured.err


@pytest.mark.parametrize("readonly", [False, True])
def test_connection_resource_settings_come_from_config(
    readonly: bool,
    tmp_path: Path,
) -> None:
    from slopsniffer.db import open_read, open_write

    path = tmp_path / "settings.toml"
    path.write_text('[ingest]\nmemory_limit = "128MiB"\nthreads = 1\n')
    config = load_config(str(path))
    with open_read(config) if readonly else open_write(config) as con:
        settings = dict(
            con.execute(
                "SELECT name, value FROM duckdb_settings() WHERE name IN "
                "('memory_limit', 'threads', 'preserve_insertion_order', 'temp_directory')"
            ).fetchall()
        )
    assert settings == {
        "memory_limit": "128.0 MiB",
        "threads": "1",
        "preserve_insertion_order": "false",
        "temp_directory": str(config.data_dir / "tmp"),
    }


def test_query_runtime_oom_is_not_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from typing import cast

    from slopsniffer import cli

    class Connection:
        def execute(self, sql: str) -> None:
            pass

        def sql(self, sql: str) -> None:
            raise duckdb.OutOfMemoryException("injected query OOM")

    @contextmanager
    def read(config: Config) -> Iterator[duckdb.DuckDBPyConnection]:
        yield cast(duckdb.DuckDBPyConnection, Connection())

    monkeypatch.setattr(cli, "open_read", read)
    assert main(["query", "SELECT 1", "--json"]) == 3
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    validate("error", doc)
    assert (doc["error"], doc["command"], doc["message"]) == (
        "database_error",
        "query",
        "injected query OOM",
    )
    assert "injected query OOM" in captured.err


@pytest.mark.parametrize("bound", ["rows", "bytes"])
def test_large_json_session_derives_in_bounded_batches(
    bound: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from slopsniffer.ingest import reader

    root = tmp_path / "corpus"
    root.mkdir()
    record = {
        "type": "api-request-blob",
        "sessionId": "large",
        "cwd": "/work/project",
        "gitBranch": "topic",
        "message": {"content": "x" * (64 * 1024)},
    }
    path = root / "large.jsonl"
    with path.open("wb") as stream:
        for _ in range(512):
            stream.write(jsonl(record))
        stream.write(
            jsonl(
                {
                    "type": "assistant",
                    "sessionId": "large",
                    "message": {
                        "model": "model-large",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call",
                                "name": "Read",
                                "input": {"path": "x"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "sessionId": "large",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "call", "content": "complete"}
                        ]
                    },
                },
            )
        )
    from dataclasses import replace

    source = claude.source(root)
    monkeypatch.setitem(engine.REGISTRY, "claude", replace(source, finalize=None))
    config = tmp_path / "bounded.toml"
    config.write_text(
        '[ingest]\nmemory_limit = "128MB"\nthreads = 1\nworkers = 1\nbatch_rows = 16\n'
    )
    code, doc = run(capsys, config, "claude")
    assert (code, doc["harnesses"]["claude"]["files_failed"]) == (0, [])
    monkeypatch.setitem(engine.REGISTRY, "claude", source)
    if bound == "bytes":
        monkeypatch.setattr(reader, "BATCH_BYTES", 1024 * 1024)
        config.write_text(config.read_text().replace("batch_rows = 16", "batch_rows = 5000"))
    code, doc = run(capsys, config, "claude")
    assert (code, doc["harnesses"]["claude"]["files_failed"]) == (0, [])
    assert query("SELECT session_id, cwd, project, git_branch, model FROM sessions") == [
        ("large", "/work/project", "project", "topic", "model-large")
    ]
    assert query("SELECT call_id, tool_name, input, result_text, is_error FROM tool_calls") == [
        ("call", "Read", '{"path":"x"}', "complete", False)
    ]


@pytest.mark.parametrize("harness", ["codex", "droid"])
def test_crash_after_body_batch_replays_unpublished_session(
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dataclasses import replace

    root = tmp_path / "corpus"
    sessions, calls = corpus(root, harness, large=False)
    if harness == "codex":
        for path in root.glob("*.jsonl"):
            with path.open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": {
                                        "input_tokens": 11,
                                        "output_tokens": 7,
                                        "cached_input_tokens": 3,
                                    }
                                },
                            },
                        }
                    )
                    + "\n"
                )
    source = {"codex": codex.source, "droid": droid.source}[harness](root)
    monkeypatch.setitem(engine.REGISTRY, harness, replace(source, finalize=None))
    config = tmp_path / "config.toml"
    config.write_text("[ingest]\nworkers = 1\nthreads = 1\nbatch_rows = 1\n")
    code, doc = run(capsys, config, harness)
    assert (code, doc["harnesses"][harness]["files_failed"]) == (0, [])
    usage_query = (
        "SELECT e.session_id,u.event_id,u.input_tokens,u.output_tokens,u.cache_read "
        "FROM usage u JOIN events e USING(event_id) ORDER BY 1,2"
    )
    expected_usage = query(usage_query)
    if harness == "codex":
        assert {row[0] for row in expected_usage} == sessions
        assert {tuple(row[2:]) for row in expected_usage} == {(8, 7, 3)}
    script = """
import os, sys, duckdb
from slopsniffer.config import load_config
from slopsniffer.ingest import codex, droid
config = load_config(sys.argv[1])
con = duckdb.connect(
    str(config.data_dir / 'telemetry.duckdb'),
    config={'memory_limit': '512MB', 'threads': 1},
)
class Crash:
    def __getattr__(self, name):
        return getattr(con, name)
    def commit(self):
        con.commit()
        if con.execute('SELECT use_event_id FROM tool_calls LIMIT 1').fetchone():
            os._exit(73)
{'codex': codex, 'droid': droid}[sys.argv[2]].finalize(Crash(), config)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(config), harness],
        timeout=30,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert child.returncode == 73
    if harness == "codex":
        assert query(usage_query) == []
    assert query("SELECT session_id FROM sessions") == []
    assert set(query("SELECT session_id, call_id FROM tool_calls")) == {min(calls)}
    assert set(query("SELECT DISTINCT session_id FROM events WHERE seq IS NULL")) == {
        (sid,) for sid in sessions
    }
    monkeypatch.setitem(engine.REGISTRY, harness, source)
    code, doc = run(capsys, config, harness)
    assert (code, doc["harnesses"][harness]["files_failed"]) == (0, [])
    assert set(query("SELECT session_id, call_id FROM tool_calls")) == calls
    assert set(query("SELECT session_id FROM sessions")) == {(sid,) for sid in sessions}
    if harness == "codex":
        assert query(usage_query) == expected_usage
