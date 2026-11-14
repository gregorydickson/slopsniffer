"""Writer, source-byte, and real worker-process contracts at their owning boundaries."""

import hashlib
import signal
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from slopsniffer.contracts import Harness
from slopsniffer.db import apply_schema
from slopsniffer.ingest import engine
from slopsniffer.ingest.reader import Batch, Event, Row
from tests.ingest.parsers import ExplodingParser


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = duckdb.connect(config={"threads": 1})
    apply_schema(connection)
    yield connection
    connection.close()


def test_unavailable_database_summary_counts_each_requested_harness_once() -> None:
    harnesses: tuple[Harness, ...] = ("claude", "codex")
    result = engine.failed_summary(harnesses, "synthetic unavailable")
    assert set(result["harnesses"]) == set(harnesses)
    for harness in harnesses:
        counts = result["harnesses"][harness]
        assert counts["files_failed"] == [f"{harness}: database unavailable: synthetic unavailable"]
        assert counts["ingest_errors"] == len(counts["files_failed"])
        assert {k for k, v in counts.items() if v} == {"files_failed", "ingest_errors"}
    assert result["started_at"] <= result["finished_at"]
    for stamp in (result["started_at"], result["finished_at"]):
        assert datetime.fromisoformat(stamp).utcoffset() == timedelta(0)


def test_writer_commits_rows_offsets_errors_and_accumulates_failures(
    con: duckdb.DuckDBPyConnection,
) -> None:
    sources = {17: "/synthetic/first", 23: "/synthetic/second"}
    for fid, path in sources.items():
        con.execute(
            "INSERT INTO source_files (source_file_id, path, harness) VALUES (?, ?, 'claude')",
            [fid, path],
        )
    counts = engine._empty_counts()
    writer = engine._Writer(
        con, {fid: engine._Tracked(path, counts) for fid, path in sources.items()}
    )
    batch = Batch(
        17,
        29,
        events=[
            (
                "event-one",
                0,
                11,
                Event(
                    "user",
                    "message",
                    session_id="session",
                    seq=1,
                    ts="2026-11-09T10:00:00+00:00",
                    parsed='{"n":1}',
                ),
            ),
            ("event-two", 11, 18, Event("malformed", "malformed")),
        ],
        rows=[
            Row("messages", {"event_id": "event-one", "role": "user", "text": "hello"}),
            Row("blobs", {"content_hash": "digest", "content": "same"}),
            Row("blobs", {"content_hash": "digest", "content": "same"}),
        ],
        errors=[(11, "bad JSON")],
    )
    assert writer.commit(batch)
    assert con.execute(
        "SELECT event_id, session_id, seq, record_type, kind, epoch(ts), source_file_id, "
        "byte_offset, byte_length, parsed FROM events ORDER BY byte_offset"
    ).fetchall() == [
        ("event-one", "session", 1, "user", "message", 1794218400.0, 17, 0, 11, '{"n":1}'),
        ("event-two", None, None, "malformed", "malformed", None, 17, 11, 18, None),
    ]
    assert con.execute("SELECT * FROM messages").fetchall() == [("event-one", "user", "hello")]
    assert con.execute("SELECT * FROM blobs").fetchall() == [("digest", "same")]
    assert con.execute(
        "SELECT committed_offset, status, last_ingested_at IS NOT NULL FROM source_files "
        "WHERE source_file_id = 17"
    ).fetchall() == [(batch.end, "resumable", True)]
    assert con.execute(
        "SELECT source_file_id, byte_offset, error FROM ingest_errors"
    ).fetchall() == [(17, 11, "bad JSON")]
    malformed = b"not-json\n"
    second = Batch(
        17,
        batch.end + len(malformed),
        eof=True,
        events=[("event-three", batch.end, len(malformed), Event("malformed", "malformed"))],
        errors=[(batch.end, "another bad JSON")],
    )
    assert writer.commit(second)
    writer.fail(23, "first failure")
    writer.fail(17, "second failure")
    assert counts["files_failed"] == [sources[23], sources[17]]
    assert counts["events_written"] == sum(len(b.events) for b in (batch, second))
    failures = con.execute(
        "SELECT source_file_id, byte_offset, error FROM ingest_errors"
    ).fetchall()
    assert set(failures) == {
        (17, 11, "bad JSON"),
        (17, batch.end, "another bad JSON"),
        (23, None, "first failure"),
        (17, None, "second failure"),
    }
    assert counts["ingest_errors"] == len(failures)


def test_writer_rolls_back_rejected_rows_and_preserves_previous_offset(
    con: duckdb.DuckDBPyConnection,
) -> None:
    con.execute(
        "INSERT INTO source_files (source_file_id, path, harness, committed_offset) "
        "VALUES (17, '/synthetic/input', 'claude', 11)"
    )
    counts = engine._empty_counts()
    writer = engine._Writer(con, {17: engine._Tracked("/synthetic/input", counts)})
    batch = Batch(
        17,
        29,
        eof=True,
        events=[("rejected-event", 11, 18, Event("user", "message"))],
        rows=[Row("messages", {"no_such_column": "bad"})],
    )
    assert not writer.commit(batch)
    assert con.execute("SELECT * FROM events").fetchall() == []
    assert con.execute("SELECT * FROM messages").fetchall() == []
    assert con.execute("SELECT committed_offset, status FROM source_files").fetchall() == [
        (11, "failed")
    ]
    assert counts["files_failed"] == ["/synthetic/input"]
    error = con.execute("SELECT error FROM ingest_errors").fetchone()
    assert error is not None
    assert error[0].startswith(f"{duckdb.BinderException.__name__}: ")
    assert counts["events_written"] == len(con.execute("SELECT * FROM events").fetchall())
    assert counts["ingest_errors"] == len(
        con.execute("SELECT source_file_id, byte_offset, error FROM ingest_errors").fetchall()
    )


def test_writer_empty_eof_batch_marks_complete(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        "INSERT INTO source_files (source_file_id, path, harness) "
        "VALUES (17, '/synthetic/empty', 'claude')"
    )
    counts = engine._empty_counts()
    writer = engine._Writer(con, {17: engine._Tracked("/synthetic/empty", counts)})
    assert writer.commit(Batch(17, 0, eof=True))
    assert con.execute("SELECT committed_offset, status FROM source_files").fetchall() == [
        (0, "complete")
    ]
    assert counts["events_written"] == len(con.execute("SELECT * FROM events").fetchall())
    assert counts["ingest_errors"] == len(
        con.execute("SELECT source_file_id, byte_offset, error FROM ingest_errors").fetchall()
    )


@pytest.mark.parametrize("missing", ["size", "head"])
def test_read_event_refuses_incomplete_snapshot(tmp_path: Path, missing: str) -> None:
    path = tmp_path / "input"
    path.write_bytes(b"source")
    loc = engine.EventLocation(
        "probe", str(path), 0, 6, 6, hashlib.sha256(path.read_bytes()).hexdigest()
    )
    loc = replace(loc, size=None) if missing == "size" else replace(loc, head=None)
    with pytest.raises(engine.SourceUnavailable) as error:
        engine.read_event(loc)
    assert error.value.code == "source_changed"
    assert str(path) in str(error.value)


def test_read_event_reports_unreadable_source(tmp_path: Path) -> None:
    # Hashing a directory fails with OSError even when the tests run as root.
    loc = engine.EventLocation("probe", str(tmp_path), 0, 1, 1, "head")
    with pytest.raises(engine.SourceUnavailable) as error:
        engine.read_event(loc)
    assert error.value.code == "source_unreadable"
    assert str(tmp_path) in str(error.value)


def test_quoted_identifiers_can_write_literal_quotes(con: duckdb.DuckDBPyConnection) -> None:
    name = 'a"b'
    con.execute(f"CREATE TABLE {engine._ident(name)} (value INTEGER)")
    con.execute(f"INSERT INTO {engine._ident(name)} VALUES (7)")
    assert con.execute('SELECT value FROM "a""b"').fetchall() == [(7,)]


def test_spawn_pool_advances_batches_stops_rejected_files_and_isolates_parser_bugs(
    tmp_path: Path,
) -> None:
    from slopsniffer.ingest.reader import BatchRequest

    lines = {
        "good": [b'{"type":"one"}\n', b'{"type":"two"}\n'],
        "rejected": [b'{"type":"first"}\n', b'{"type":"uncommitted"}\n'],
        "broken": [b'{"type":"boom"}\n'],
    }
    requests = []
    names = {}
    for fid, (name, content) in enumerate(lines.items(), 17):
        path = tmp_path / f"{name}.jsonl"
        path.write_bytes(b"".join(content))
        requests.append(BatchRequest(str(path), fid, 0, path.stat().st_size, 1, ExplodingParser()))
        names[fid] = name
    committed: dict[str, list[Batch]] = {"good": [], "rejected": []}
    failed: dict[str, str] = {}

    def commit(batch: Batch) -> bool:
        name = names[batch.source_file_id]
        committed[name].append(batch)
        return name != "rejected"

    def fail(fid: int, reason: str) -> None:
        failed[names[fid]] = reason

    def expired(signum: int, frame: object) -> None:
        raise TimeoutError("finite pool requests did not complete")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 10)
    try:
        engine._run_pool(requests, 1, commit, fail)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert failed == {"broken": "RuntimeError: parser bug"}
    for name, expected in [("good", lines["good"]), ("rejected", lines["rejected"][:1])]:
        assert [event.parsed for batch in committed[name] for _, _, _, event in batch.events] == [
            line.decode().rstrip("\n") for line in expected
        ]
        assert committed[name][-1].end == sum(map(len, expected))
    assert committed["good"][-1].eof
    assert not committed["rejected"][-1].eof


def test_resume_requires_known_snapshot_and_accepts_equal_size(tmp_path: Path) -> None:
    from slopsniffer.ingest.reader import head_sha256

    path = tmp_path / "snapshot"
    data = b"original"
    path.write_bytes(data)
    stored = engine._Stored(17, len(data), head_sha256(path, len(data)), len(data))
    assert engine._prefix_intact(path, stored, len(data))
    for incomplete in (replace(stored, size=None), replace(stored, head=None)):
        assert not engine._prefix_intact(path, incomplete, len(data))
    assert not engine._prefix_intact(path, stored, len(data) - 1)
    path.write_bytes(data + b" appended")
    assert engine._prefix_intact(path, stored, path.stat().st_size)
    path.write_bytes(b"rewritten")
    assert not engine._prefix_intact(path, stored, path.stat().st_size)


def test_source_failures_keep_path_diagnostics_for_missing_and_truncated_files(
    tmp_path: Path,
) -> None:
    from slopsniffer.ingest.reader import HEAD_BYTES, head_sha256

    path = tmp_path / "source"
    data = b"a" * HEAD_BYTES + b"tail"
    path.write_bytes(data)
    loc = engine.EventLocation(
        "probe", str(path), HEAD_BYTES, len(b"tail"), len(data), head_sha256(path, len(data))
    )
    assert engine.read_event(loc) == b"tail"
    path.write_bytes(data[:HEAD_BYTES])
    with pytest.raises(engine.SourceUnavailable) as truncated:
        engine.read_event(loc)
    assert truncated.value.code == "source_changed"
    assert str(path) in str(truncated.value)
    path.unlink()
    with pytest.raises(engine.SourceUnavailable) as missing:
        engine.read_event(loc)
    assert missing.value.code == "source_missing"
    assert str(path) in str(missing.value)


def test_prepare_records_snapshots_and_accumulates_each_file_transition(
    con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    import os

    from tests.helpers import REPO_ROOT

    data = (
        (REPO_ROOT / "tests/fixtures/ingest/lossless.jsonl")
        .read_bytes()
        .splitlines(keepends=True)[0]
    )
    paths = [tmp_path / f"{name}.jsonl" for name in ("first", "second")]
    for path in paths:
        path.write_bytes(data)
    counts = engine._empty_counts()
    prepared = {path: engine._prepare(con, "claude", path, path.stat(), counts) for path in paths}
    ids = dict(con.execute("SELECT path, source_file_id FROM source_files").fetchall())
    for path in paths:
        assert prepared[path] == (ids[str(path)], 0, len(data))
    assert counts["files_new"] == len(paths)

    def snapshot(status: str, offset: int, content: bytes) -> None:
        assert set(
            con.execute(
                "SELECT path, harness, size, mtime_ns, head_sha256, committed_offset, status "
                "FROM source_files"
            ).fetchall()
        ) == {
            (
                str(path),
                "claude",
                len(content),
                path.stat().st_mtime_ns,
                hashlib.sha256(content).hexdigest(),
                offset,
                status,
            )
            for path in paths
        }

    snapshot("new", 0, data)
    con.execute("UPDATE source_files SET status = 'complete', committed_offset = size")
    for path in paths:
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        assert engine._prepare(con, "claude", path, path.stat(), counts) == (
            ids[str(path)],
            len(data),
            len(data),
        )
    assert counts["files_resumed"] == len(paths)
    snapshot("resumable", len(data), data)
    rewritten = data.upper()
    for path in paths:
        path.write_bytes(rewritten)
        assert engine._prepare(con, "claude", path, path.stat(), counts) == (
            ids[str(path)],
            0,
            len(rewritten),
        )
    assert counts["files_reingested"] == len(paths)
    snapshot("reingest", 0, rewritten)
    missing = [tmp_path / f"missing-{path.name}" for path in paths]
    for path in missing:
        assert engine._prepare(con, "claude", path, engine._stat(path), counts) is None
    assert counts["files_failed"] == [str(path) for path in missing]
    assert all(
        error.startswith(f"{FileNotFoundError.__name__}: ")
        for (error,) in con.execute("SELECT error FROM ingest_errors").fetchall()
    )
    assert counts["ingest_errors"] == len(missing)
    assert counts["files_new"] == len(paths) + len(missing)
    assert set(
        con.execute(
            "SELECT path, status, committed_offset FROM source_files WHERE status = 'failed'"
        ).fetchall()
    ) == {(str(path), "failed", 0) for path in missing}


def test_unregistered_harness_is_reported_and_does_not_skip_registered_work(
    con: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from slopsniffer.config import load_config
    from slopsniffer.ingest.passthrough import PassthroughParser

    path = tmp_path / "registered.jsonl"
    path.write_bytes(b'{"type":"registered"}\n')
    monkeypatch.delitem(engine.REGISTRY, "skills", raising=False)
    monkeypatch.setitem(
        engine.REGISTRY, "claude", engine.HarnessSource(PassthroughParser(), lambda config: [path])
    )
    caplog.set_level(logging.INFO, logger=engine.__name__)
    result = engine.ingest(con, load_config(), ["skills", "claude"], workers=1)
    assert set(result["harnesses"]) == {"claude"}
    assert con.execute("SELECT record_type FROM events").fetchall() == [("registered",)]
    assert "skills" in caplog.text


@pytest.mark.parametrize("has_missing_file", [False, True])
@pytest.mark.parametrize("finalize_fails", [False, True])
def test_ingest_accumulates_discovery_and_finalization_failures_without_losing_context(
    con: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    has_missing_file: bool,
    finalize_fails: bool,
) -> None:
    from slopsniffer.config import Config, load_config
    from slopsniffer.ingest.passthrough import PassthroughParser

    paths = [tmp_path / "vanished.jsonl"] if has_missing_file else []
    marker = "injected finalization failure"

    def finalize(connection: duckdb.DuckDBPyConnection, config: Config) -> None:
        if finalize_fails:
            raise duckdb.OutOfMemoryException(marker)

    monkeypatch.setitem(
        engine.REGISTRY,
        "claude",
        engine.HarnessSource(PassthroughParser(), lambda config: paths, finalize),
    )
    result = engine.ingest(con, load_config(), ["claude"])
    counts = result["harnesses"]["claude"]
    assert counts["ingest_errors"] == len(paths) + int(finalize_fails)
    if paths:
        assert counts["files_failed"] == [str(path) for path in paths]
    elif finalize_fails:
        assert counts["files_failed"] == [
            f"claude: {duckdb.OutOfMemoryException.__name__}: {marker}"
        ]
    else:
        assert counts["files_failed"] == []
    records = [record for record in caplog.records if record.name == engine.__name__]
    if finalize_fails:
        diagnostics = []
        for record in records:
            assert marker in record.getMessage() and "claude" in record.getMessage()
            assert record.exc_info is not None
            diagnostics.append(
                (record.exc_info[0], str(record.exc_info[1]), record.exc_info[2] is not None)
            )
        assert diagnostics == [(duckdb.OutOfMemoryException, marker, True)]
    else:
        assert records == []


@pytest.mark.parametrize("flag", [None, "0", "1", "true"])
def test_crash_hook_targets_this_process_after_a_successful_commit(
    con: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    flag: str | None,
) -> None:
    import os

    if flag is None:
        monkeypatch.delenv(engine.KILL_ENV, raising=False)
    else:
        monkeypatch.setenv(engine.KILL_ENV, flag)
    con.execute(
        "INSERT INTO source_files (source_file_id, path, harness) "
        "VALUES (17, '/synthetic/hook', 'claude')"
    )
    observed: list[tuple[int, int]] = []

    def kill(pid: int, sig: int) -> None:
        assert con.execute(
            "SELECT committed_offset, status FROM source_files WHERE source_file_id = 17"
        ).fetchall() == [(len(line), "complete")]
        observed.append((pid, sig))

    monkeypatch.setattr(os, "kill", kill)
    counts = engine._empty_counts()
    writer = engine._Writer(con, {17: engine._Tracked("/synthetic/hook", counts)})
    line = b'{"type":"hook"}\n'
    batch = Batch(
        17,
        len(line),
        eof=True,
        events=[
            ("hook-event", 0, len(line), Event("hook", "message", parsed=line.decode().strip()))
        ],
    )
    assert writer.commit(batch)
    assert observed == ([(os.getpid(), signal.SIGKILL)] if flag == "1" else [])


def test_since_includes_midnight_and_unreadable_files_but_excludes_older_files(
    con: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from slopsniffer.config import load_config
    from slopsniffer.ingest.passthrough import PassthroughParser

    midnight = datetime(2026, 11, 1)
    paths = {name: tmp_path / f"{name}.jsonl" for name in ("old", "boundary", "new", "gone")}
    for name, delta in (("old", -1), ("boundary", 0), ("new", 1)):
        path = paths[name]
        path.write_text(f'{{"type":"{name}"}}\n')
        stamp = midnight.timestamp() + delta
        os.utime(path, (stamp, stamp))
    monkeypatch.setitem(
        engine.REGISTRY,
        "claude",
        engine.HarnessSource(PassthroughParser(), lambda config: paths.values()),
    )
    result = engine.ingest(con, load_config(), ["claude"], since=midnight.date(), workers=1)
    assert set(con.execute("SELECT record_type FROM events").fetchall()) == {
        ("boundary",),
        ("new",),
    }
    expected = {str(path) for name, path in paths.items() if name != "old"}
    assert {p for (p,) in con.execute("SELECT path FROM source_files").fetchall()} == expected
    counts = result["harnesses"]["claude"]
    assert counts["files_seen"] == len(expected)
    assert counts["files_failed"] == [str(paths["gone"])]


@pytest.mark.parametrize("override", [None, 1])
def test_ingest_uses_requested_spawn_workers_with_parent_owned_writes(
    con: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: int | None,
) -> None:
    import json
    import multiprocessing
    import os
    from multiprocessing.process import BaseProcess
    from types import SimpleNamespace
    from typing import Any

    from slopsniffer.config import load_config
    from tests.ingest.parsers import ProcessContextParser

    valid = [tmp_path / f"{name}.jsonl" for name in ("first", "second")]
    for path in valid:
        path.write_bytes(b'{"type":"valid"}\n')
    invalid = tmp_path / "invalid.jsonl"
    invalid.write_bytes(b"not-json\n")
    config = load_config()
    config = replace(config, ingest=replace(config.ingest, workers=len(valid)))
    monkeypatch.setitem(
        engine.REGISTRY,
        "claude",
        engine.HarnessSource(ProcessContextParser(), lambda config: [*valid, invalid]),
    )
    original = multiprocessing.get_context
    processes: list[BaseProcess] = []

    def context(method: str | None = None) -> Any:
        # A caller relying on the platform default would use fork in this test.
        ctx: Any = original(method or "fork")

        def process(*args: Any, **kwargs: Any) -> BaseProcess:
            child: BaseProcess = ctx.Process(*args, **kwargs)
            processes.append(child)
            return child

        return SimpleNamespace(Pipe=ctx.Pipe, Process=process)

    monkeypatch.setattr(multiprocessing, "get_context", context)
    result = engine.ingest(con, config, ["claude"], workers=override)
    metadata = [json.loads(text) for (text,) in con.execute("SELECT text FROM messages").fetchall()]
    assert {m["pid"] for m in metadata} == {p.pid for p in processes}
    assert len(processes) == (override or config.ingest.workers)
    assert {(m["parent"], m["daemon"], m["method"]) for m in metadata} == {
        (os.getpid(), True, "spawn")
    }
    assert {p.exitcode for p in processes} == {0}
    assert set(con.execute("SELECT record_type, kind FROM events").fetchall()) == {
        ("valid", "passthrough"),
        ("malformed", "malformed"),
    }
    counts = result["harnesses"]["claude"]
    assert counts["events_written"] == len(valid) + len([invalid])
    assert counts["ingest_errors"] == len([invalid])
    assert counts["files_failed"] == []


def test_failed_worker_does_not_dispatch_a_new_file_ahead_of_ready_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiprocessing.connection import Connection
    from multiprocessing.connection import wait as original_wait
    from typing import Any

    from slopsniffer.ingest.reader import BatchRequest
    from tests.ingest.deadlines import deadline
    from tests.ingest.parsers import ExitParser

    data = {
        "broken": [b'{"type":"boom"}\n'],
        "exited": [b'{"type":"exit-worker"}\n'],
        "good": [b'{"type":"first"}\n', b'{"type":"second"}\n'],
        "later": [b'{"type":"later"}\n'],
    }
    requests = []
    for fid, (name, lines) in enumerate(data.items(), 17):
        path = tmp_path / name
        path.write_bytes(b"".join(lines))
        requests.append(BatchRequest(str(path), fid, 0, path.stat().st_size, 1, ExitParser()))
    sent: list[tuple[str, int]] = []
    original_send = Connection.send

    def send(connection: Any, request: Any) -> None:
        if isinstance(request, BatchRequest):
            sent.append((Path(request.path).name, request.start))
        original_send(connection, request)

    monkeypatch.setattr(Connection, "send", send)
    first = True

    def wait(connections: Any, timeout: float | None = None) -> Any:
        nonlocal first
        if first:
            first = False
            # All real children have replied; preserve their request insertion order.
            for connection in connections:
                assert connection.poll(5), "spawned worker did not reply"
            return list(connections)
        return original_wait(connections, timeout)

    monkeypatch.setattr(engine, "wait", wait)
    committed: list[tuple[str, int]] = []
    failures: dict[int, str] = {}
    names = {request.source_file_id: Path(request.path).name for request in requests}

    def commit(batch: Batch) -> bool:
        committed.extend((names[batch.source_file_id], offset) for _, offset, _, _ in batch.events)
        return True

    with deadline(10, "continuation-priority pool"):
        engine._run_pool(
            requests,
            len(data) - len(["later"]),
            commit,
            lambda fid, reason: failures.__setitem__(fid, reason),
        )
    expected = [("broken", 0), ("exited", 0)]
    for name in ("good", "later"):
        offset = 0
        for line in data[name]:
            expected.append((name, offset))
            offset += len(line)
        expected.append((name, offset))  # Empty EOF probe after a full batch.
    assert sorted(sent) == sorted(expected)
    assert sent.index(("good", len(data["good"][0]))) < sent.index(("later", 0))
    assert set(committed) == {
        ("good", 0),
        ("good", len(data["good"][0])),
        ("later", 0),
    }
    assert failures == {
        requests[0].source_file_id: "RuntimeError: parser bug",
        requests[1].source_file_id: "worker process exited unexpectedly",
    }


def test_pool_reports_lost_worker_and_fails_pending_files_without_committing(
    tmp_path: Path,
) -> None:
    from slopsniffer.ingest.reader import BatchRequest
    from tests.ingest.deadlines import deadline
    from tests.ingest.parsers import ExitParser

    data = {"exited": b'{"type":"exit-worker"}\n', "pending": b'{"type":"valid"}\n'}
    requests = []
    for fid, (name, content) in enumerate(data.items(), 17):
        path = tmp_path / name
        path.write_bytes(content)
        requests.append(BatchRequest(str(path), fid, 0, len(content), 1, ExitParser()))
    failures: dict[int, str] = {}
    committed: list[Batch] = []

    def commit(batch: Batch) -> bool:
        committed.append(batch)
        return True

    with deadline(5, "lost-worker pool"):
        engine._run_pool(requests, 1, commit, lambda fid, reason: failures.__setitem__(fid, reason))
    assert committed == []
    assert failures == {
        requests[0].source_file_id: "worker process exited unexpectedly",
        requests[1].source_file_id: "no worker processes left",
    }


def test_pool_terminates_busy_workers_when_the_writer_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import multiprocessing

    from slopsniffer.ingest.passthrough import PassthroughParser
    from slopsniffer.ingest.reader import BatchRequest
    from tests.ingest.deadlines import deadline
    from tests.ingest.parsers import DelayedServe

    paths = [tmp_path / name for name in ("slow", "fast")]
    requests = []
    for fid, path in enumerate(paths, 17):
        path.write_bytes(b'{"type":"valid"}\n')
        requests.append(
            BatchRequest(str(path), fid, 0, path.stat().st_size, 1, PassthroughParser())
        )
    marker = tmp_path / "busy"
    monkeypatch.setattr(engine, "serve", DelayedServe(str(marker), pause=20))
    monkeypatch.setattr(engine, "WORKER_JOIN_SECONDS", 0.05)
    before = {p.pid for p in multiprocessing.active_children()}

    def commit(batch: Batch) -> bool:
        assert batch.source_file_id == requests[-1].source_file_id
        assert marker.exists()
        raise RuntimeError("synthetic writer stopped")

    def fail(fid: int, reason: str) -> None:
        pytest.fail(f"unexpected worker failure {fid}: {reason}")

    try:
        with (
            pytest.raises(RuntimeError, match="synthetic writer stopped"),
            deadline(5, "worker cleanup"),
        ):
            engine._run_pool(requests, len(paths), commit, fail)
        assert {p.pid for p in multiprocessing.active_children()} == before
    finally:
        for child in multiprocessing.active_children():
            if child.pid not in before:
                child.terminate()
                child.join()


@pytest.mark.parametrize("query_unavailable", [False, True])
def test_finalize_failure_reports_interrupted_files_outside_since_and_survives_query_errors(
    con: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query_unavailable: bool,
) -> None:
    import os

    from slopsniffer.config import Config, load_config
    from slopsniffer.ingest.passthrough import PassthroughParser

    cutoff = datetime(2026, 11, 1)
    cases = {
        "unsequenced": ("claude", "known", None),
        "missing-session": ("claude", "not-derived", 1),
        "derived": ("claude", "known", 1),
        "other-harness": ("codex", "foreign", None),
    }
    paths = {}
    con.execute("INSERT INTO sessions (session_id, harness) VALUES ('known', 'claude')")
    for fid, (name, (harness, sid, seq)) in enumerate(cases.items(), 17):
        path = tmp_path / name
        path.write_bytes(b'{"type":"old"}\n')
        stamp = cutoff.timestamp() - 1
        os.utime(path, (stamp, stamp))
        paths[name] = path
        con.execute(
            "INSERT INTO source_files (source_file_id, path, harness) VALUES (?, ?, ?)",
            [fid, str(path), harness],
        )
        con.execute(
            "INSERT INTO events (event_id, session_id, seq, record_type, kind, "
            "source_file_id, byte_offset, byte_length) VALUES (?, ?, ?, 'old', 'test', ?, 0, ?)",
            [name, sid, seq, fid, path.stat().st_size],
        )
    marker = "synthetic interrupted finalization"

    def finalize(connection: duckdb.DuckDBPyConnection, config: Config) -> None:
        if query_unavailable:
            connection.execute("DROP TABLE sessions")
        raise duckdb.OutOfMemoryException(marker)

    monkeypatch.setitem(
        engine.REGISTRY,
        "claude",
        engine.HarnessSource(PassthroughParser(), lambda config: paths.values(), finalize),
    )
    result = engine.ingest(con, load_config(), ["claude"], since=cutoff.date())
    counts = result["harnesses"]["claude"]
    eligible = [path for path in paths.values() if path.stat().st_mtime >= cutoff.timestamp()]
    assert counts["files_seen"] == len(eligible)
    assert {eid for (eid,) in con.execute("SELECT event_id FROM events").fetchall()} == set(cases)
    if query_unavailable:
        expected = [f"claude: {duckdb.OutOfMemoryException.__name__}: {marker}"]
    else:
        expected = sorted(str(paths[name]) for name in ("unsequenced", "missing-session"))
    assert counts["files_failed"] == expected
    assert counts["ingest_errors"] == len([marker])
