"""Harness-agnostic, lossless, resumable ingest.

Each source file moves through the ``source_files`` states: ``new`` when first seen;
``resumable`` once a batch commits; ``complete`` at the end of the size snapshot taken
when the pass started. A file whose hashed head prefix is intact and that did not shrink
resumes at ``committed_offset``; a changed head or a shrink deletes every row the file
owns and re-ingests it (``reingest``). Unreadable files are ``failed``; files that no
longer exist are ``missing`` and keep their rows.

Every source line becomes exactly one ``events`` row whose ``(byte_offset, byte_length)``
slice is the line, terminator included; a line that does not parse still gets its event
(kind ``malformed``) plus an ``ingest_errors`` row. An unterminated final line is left
uncommitted for the next run. Worker processes parse in parallel; this process is the only
writer, and each batch's rows commit in the transaction that advances ``committed_offset``.
"""

import logging
import multiprocessing
import os
import signal
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from itertools import groupby
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import cast

import duckdb

from slopsniffer.config import Config
from slopsniffer.contracts import Harness, HarnessIngest, IngestSummary
from slopsniffer.ingest.progress import CURRENT, Progress, tick
from slopsniffer.ingest.reader import Batch, BatchRequest, Parser, Row, head_sha256, serve

log = logging.getLogger(__name__)

KILL_ENV = "SLOPSNIFFER_TEST_KILL_AFTER_BATCH"
WORKER_JOIN_SECONDS = 10

_INSERT_EVENTS = """
INSERT INTO events (event_id, session_id, seq, record_type, kind, ts,
                    source_file_id, byte_offset, byte_length, parsed)
SELECT unnest($1::VARCHAR[]), unnest($2::VARCHAR[]), unnest($3::BIGINT[]),
       unnest($4::VARCHAR[]), unnest($5::VARCHAR[]), unnest($6::VARCHAR[])::TIMESTAMPTZ,
       $7, unnest($8::BIGINT[]), unnest($9::BIGINT[]), unnest($10::VARCHAR[])::JSON
"""
_INSERT_ERRORS = """
INSERT INTO ingest_errors (source_file_id, byte_offset, error)
SELECT $1, unnest($2::BIGINT[]), unnest($3::VARCHAR[])
"""

type Link = Connection[BatchRequest | None, Batch]
type Finalize = Callable[[duckdb.DuckDBPyConnection, Config], None]


class SourceError(Exception):
    """Source discovery failed; report it rather than silently dropping a harness."""


class FinalizeFailures(Exception):
    """Sessions rolled back and recorded while the remaining sessions committed."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__("session finalization failed")


@dataclass(frozen=True)
class HarnessSource:
    """A harness's parser and file discovery. ``finalize``, when set, runs once after every
    file has been read; the finalizer owns transactions for its bounded derivation units."""

    parser: Parser
    discover: Callable[[Config], Iterable[Path]]
    finalize: Finalize | None = None


# Harness parsers register their source here; ``ingest`` runs the requested ones.
REGISTRY: dict[Harness, HarnessSource] = {}

# Tables keyed by a hash of their content: a duplicate key is the same row, so it is skipped.
CONTENT_ADDRESSED = frozenset({"blobs"})


@dataclass(frozen=True)
class _Stored:
    source_file_id: int
    size: int | None
    head: str | None
    committed_offset: int


@dataclass(frozen=True)
class _Tracked:
    path: str
    counts: HarnessIngest


def ingest(
    con: duckdb.DuckDBPyConnection,
    config: Config,
    harnesses: Iterable[Harness],
    *,
    since: date | None = None,
    workers: int | None = None,
) -> IngestSummary:
    started_at = _now()
    by_harness: dict[Harness, HarnessIngest] = {}
    cutoff = datetime.combine(since, time.min).timestamp() if since else None
    for harness in harnesses:
        source = REGISTRY.get(harness)
        if source is None:
            log.info("no parser registered for harness %s yet; skipped", harness)
            continue
        counts = by_harness[harness] = _empty_counts()
        paths: list[str] = []
        progress = Progress(harness, counts)
        token = CURRENT.set(progress)
        progress.emit(force=True)
        try:
            _mark_missing(con, harness)
            tracked: dict[int, _Tracked] = {}
            requests: list[BatchRequest] = []
            for discovered in source.discover(config):
                path = discovered.resolve()
                stat = _stat(path)
                if (
                    cutoff is not None
                    and isinstance(stat, os.stat_result)
                    and stat.st_mtime < cutoff
                ):
                    continue
                paths.append(str(path))
                counts["files_seen"] += 1
                start = _prepare(con, harness, path, stat, counts)
                if start is None:
                    continue
                fid, offset, limit = start
                tracked[fid] = _Tracked(str(path), counts)
                requests.append(
                    BatchRequest(
                        str(path), fid, offset, limit, config.ingest.batch_rows, source.parser
                    )
                )
            progress.total = counts["files_seen"]
            progress.done = progress.total - len(requests)
            progress.phase = "read"
            writer = _Writer(con, tracked)

            def commit(
                batch: Batch, writer: _Writer = writer, progress: Progress = progress
            ) -> bool:
                accepted = writer.commit(batch)
                if batch.eof or not accepted:
                    progress.done += 1
                progress.emit()
                return accepted

            def fail(
                fid: int, error: str, writer: _Writer = writer, progress: Progress = progress
            ) -> None:
                writer.fail(fid, error)
                progress.done += 1
                progress.emit()

            _run_pool(requests, workers or config.ingest.workers, commit, fail)
            if source.finalize is not None:
                # Committed append buffers cannot spill until checkpointed. Release the
                # ingest phase's pinned buffers before running window/JSON derivations.
                con.execute("CHECKPOINT")
                progress.phase = "finalize"
                progress.emit(force=True)
                source.finalize(con, config)
                if harness == "skills":
                    inventory = con.execute("SELECT count(*) FROM skills").fetchone()
                    assert inventory is not None
                    counts["files_seen"] = inventory[0]
                    progress.done = progress.total = inventory[0]
        except FinalizeFailures as exc:
            counts["files_failed"] = sorted(set(counts["files_failed"] + exc.paths))
            counts["ingest_errors"] += len(exc.paths)
        except SourceError as exc:
            log.error("%s source discovery failed: %s", harness, exc)
            counts["files_failed"].append(str(exc))
            counts["ingest_errors"] += 1
        except duckdb.Error as exc:
            log.error("%s ingest/finalize failed: %s", harness, exc, exc_info=True)
            # Include pending files from earlier interrupted runs, even outside --since.
            with suppress(duckdb.Error):
                pending_paths = [
                    row[0]
                    for row in con.execute(
                        "SELECT DISTINCT f.path FROM source_files f "
                        "JOIN events e USING (source_file_id) "
                        "WHERE f.harness = ? AND (e.seq IS NULL OR NOT EXISTS "
                        "(SELECT 1 FROM sessions s WHERE s.session_id = e.session_id))",
                        [harness],
                    ).fetchall()
                ]
                if pending_paths:
                    paths = pending_paths
            counts["files_failed"] = sorted(set(counts["files_failed"] + paths)) or [
                f"{harness}: {type(exc).__name__}: {exc}"
            ]
            counts["ingest_errors"] += 1
        finally:
            progress.phase = "failed" if counts["files_failed"] else "complete"
            progress.emit(force=True)
            CURRENT.reset(token)
    from slopsniffer import pricing
    from slopsniffer.ingest import metadata

    if not any(counts["files_failed"] for counts in by_harness.values()):
        metadata.refresh(con)
        pricing.refresh(con, config)
    return {"started_at": started_at, "finished_at": _now(), "harnesses": by_harness}


def failed_summary(harnesses: Iterable[Harness], error: str) -> IngestSummary:
    """Failure before file discovery (for example, opening or configuring DuckDB)."""
    counts = {h: _empty_counts() for h in harnesses}
    for harness, value in counts.items():
        value["files_failed"] = [f"{harness}: database unavailable: {error}"]
        value["ingest_errors"] = 1
    return {"started_at": _now(), "finished_at": _now(), "harnesses": counts}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _empty_counts() -> HarnessIngest:
    return {
        "files_seen": 0,
        "files_skipped_unchanged": 0,
        "files_resumed": 0,
        "files_reingested": 0,
        "files_new": 0,
        "files_failed": [],
        "events_written": 0,
        "ingest_errors": 0,
    }


def _stat(path: Path) -> os.stat_result | OSError:
    try:
        return path.stat()
    except OSError as exc:
        return exc


def _mark_missing(con: duckdb.DuckDBPyConnection, harness: Harness) -> None:
    rows = con.execute(
        "SELECT source_file_id, path FROM source_files WHERE harness = ? AND status <> 'missing'",
        [harness],
    ).fetchall()
    gone = [fid for fid, path in rows if not os.path.lexists(path)]
    if gone:
        con.execute(
            "UPDATE source_files SET status = 'missing' "
            "WHERE source_file_id IN (SELECT unnest($1::BIGINT[]))",
            [gone],
        )


def _prepare(
    con: duckdb.DuckDBPyConnection,
    harness: Harness,
    path: Path,
    stat: os.stat_result | OSError,
    counts: HarnessIngest,
) -> tuple[int, int, int] | None:
    """Classify one file and record its new snapshot; returns (id, start, limit) to read."""
    row = con.execute(
        "SELECT source_file_id, size, mtime_ns, head_sha256, committed_offset, status "
        "FROM source_files WHERE path = ?",
        [str(path)],
    ).fetchone()
    if row is None:
        inserted = con.execute(
            "INSERT INTO source_files (path, harness) VALUES (?, ?) RETURNING source_file_id",
            [str(path), harness],
        ).fetchone()
        assert inserted is not None
        stored, mtime_ns, status = _Stored(inserted[0], None, None, 0), None, "new"
        counts["files_new"] += 1
    else:
        stored, mtime_ns, status = _Stored(row[0], row[1], row[3], row[4]), row[2], row[5]
    try:
        if isinstance(stat, OSError):
            raise stat
        if status == "complete" and (stored.size, mtime_ns) == (stat.st_size, stat.st_mtime_ns):
            counts["files_skipped_unchanged"] += 1
            return None
        head = head_sha256(path, stat.st_size)
        snapshot = [stat.st_size, stat.st_mtime_ns, head, stored.source_file_id]
        if row is None:
            _update_snapshot(con, snapshot, "new")
            return stored.source_file_id, 0, stat.st_size
        if _prefix_intact(path, stored, stat.st_size):
            _update_snapshot(con, snapshot, "resumable")
            counts["files_resumed"] += 1
            return stored.source_file_id, stored.committed_offset, stat.st_size
        _reingest(con, snapshot)
        counts["files_reingested"] += 1
        return stored.source_file_id, 0, stat.st_size
    except OSError as exc:
        _fail(con, stored.source_file_id, f"{type(exc).__name__}: {exc}")
        counts["files_failed"].append(str(path))
        counts["ingest_errors"] += 1
        return None


def _prefix_intact(path: Path, stored: _Stored, size: int) -> bool:
    """The bytes hashed last time are unchanged and nothing was cut off."""
    if stored.head is None or stored.size is None or size < stored.size:
        return False
    return head_sha256(path, stored.size) == stored.head


def _update_snapshot(con: duckdb.DuckDBPyConnection, snapshot: list[object], status: str) -> None:
    con.execute(
        "UPDATE source_files SET size = $1, mtime_ns = $2, head_sha256 = $3, status = $5 "
        "WHERE source_file_id = $4",
        [*snapshot, status],
    )


def _owned_tables(con: duckdb.DuckDBPyConnection) -> tuple[list[str], list[str]]:
    """Tables whose rows a source file owns: by its events' ids, and by its id."""
    columns = con.execute(
        "SELECT table_name, column_name FROM duckdb_columns() "
        "WHERE database_name = current_database() AND schema_name = 'main' "
        "AND column_name IN ('event_id', 'source_file_id') AND table_name <> 'source_files' "
        "ORDER BY table_name"
    ).fetchall()
    by_event = [t for t, c in columns if c == "event_id" and t != "events"]
    by_file = [t for t, c in columns if c == "source_file_id"]
    return by_event, by_file


def _reingest(con: duckdb.DuckDBPyConnection, snapshot: list[object]) -> None:
    fid = snapshot[-1]
    by_event, by_file = _owned_tables(con)
    with transaction(con):
        for table in by_event:
            con.execute(
                f"DELETE FROM {_ident(table)} WHERE event_id IN "
                "(SELECT event_id FROM events WHERE source_file_id = ?)",
                [fid],
            )
        for table in by_file:
            con.execute(f"DELETE FROM {_ident(table)} WHERE source_file_id = ?", [fid])
        con.execute("UPDATE source_files SET committed_offset = 0 WHERE source_file_id = ?", [fid])
        _update_snapshot(con, snapshot, "reingest")


def _fail(con: duckdb.DuckDBPyConnection, fid: int, error: str) -> None:
    with transaction(con):
        con.execute("UPDATE source_files SET status = 'failed' WHERE source_file_id = ?", [fid])
        con.execute(
            "INSERT INTO ingest_errors (source_file_id, byte_offset, error) VALUES (?, NULL, ?)",
            [fid, error],
        )


@contextmanager
def transaction(con: duckdb.DuckDBPyConnection) -> Iterator[None]:
    con.begin()
    try:
        yield
    except BaseException:
        con.rollback()
        raise
    con.commit()


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class _Writer:
    """The single writer: each batch and its offset advance commit together."""

    def __init__(self, con: duckdb.DuckDBPyConnection, tracked: dict[int, _Tracked]) -> None:
        self.con = con
        self.tracked = tracked
        self.kill_after_commit = os.environ.get(KILL_ENV) == "1"

    def commit(self, batch: Batch) -> bool:
        """Commit ``batch``; a batch the database rejects fails its file. True if committed."""
        counts = self.tracked[batch.source_file_id].counts
        try:
            self._insert(batch)
        except duckdb.Error as exc:
            self.fail(batch.source_file_id, f"{type(exc).__name__}: {exc}")
            return False
        counts["events_written"] += len(batch.events)
        counts["ingest_errors"] += len(batch.errors)
        if self.kill_after_commit:
            os.kill(os.getpid(), signal.SIGKILL)
        return True

    def _insert(self, batch: Batch) -> None:
        con = self.con
        with transaction(con):
            if batch.events:
                con.execute(_INSERT_EVENTS, _event_columns(batch))
            _insert_rows(con, batch.rows)
            if batch.errors:
                offsets, messages = zip(*batch.errors, strict=True)
                con.execute(_INSERT_ERRORS, [batch.source_file_id, list(offsets), list(messages)])
            con.execute(
                "UPDATE source_files SET committed_offset = ?, status = ?, "
                "last_ingested_at = now() WHERE source_file_id = ?",
                [batch.end, "complete" if batch.eof else "resumable", batch.source_file_id],
            )

    def fail(self, source_file_id: int, error: str) -> None:
        tracked = self.tracked[source_file_id]
        _fail(self.con, source_file_id, error)
        tracked.counts["files_failed"].append(tracked.path)
        tracked.counts["ingest_errors"] += 1


def _insert_rows(con: duckdb.DuckDBPyConnection, rows: Sequence[Row]) -> None:
    for table, columns, grouped in _group_rows(rows):
        names = ", ".join(_ident(c) for c in columns)
        values = ", ".join(f"unnest(${i})" for i in range(1, len(columns) + 1))
        select = f"SELECT {values}"
        if table in CONTENT_ADDRESSED:
            # DISTINCT bounds duplicate candidates by this batch; the anti-join
            # scans stored hashes without a persistent, history-sized ART index.
            select = (
                f"SELECT DISTINCT {names} FROM ({select}) AS batch ({names}) "
                f"ANTI JOIN {_ident(table)} AS stored USING (content_hash)"
            )
        con.execute(
            f"INSERT INTO {_ident(table)} ({names}) {select}",
            [[row.values[c] for row in grouped] for c in columns],
        )


def _event_columns(batch: Batch) -> list[object]:
    ids, offsets, lengths, events = zip(*batch.events, strict=True)
    return [
        list(ids),
        [e.session_id for e in events],
        [e.seq for e in events],
        [e.record_type for e in events],
        [e.kind for e in events],
        [e.ts for e in events],
        batch.source_file_id,
        list(offsets),
        list(lengths),
        [e.parsed for e in events],
    ]


def _group_rows(rows: Sequence[Row]) -> Iterable[tuple[str, tuple[str, ...], list[Row]]]:
    def key(row: Row) -> tuple[str, tuple[str, ...]]:
        return row.table, tuple(sorted(row.values))

    for (table, columns), group in groupby(sorted(rows, key=key), key=key):
        yield table, columns, list(group)


def _run_pool(
    requests: list[BatchRequest],
    workers: int,
    commit: Callable[[Batch], bool],
    fail: Callable[[int, str], None],
) -> None:
    """Parse ``requests`` on up to ``workers`` processes, one outstanding batch per worker.

    Files are read in order batch by batch; a finished batch's continuation goes to the
    front of the queue so each file completes before many others are opened.
    """
    if not requests:
        return
    ctx = multiprocessing.get_context("spawn")
    links: list[Link] = []
    procs = []
    for _ in range(min(workers, len(requests))):
        parent, child = ctx.Pipe()
        proc = ctx.Process(target=serve, args=(child,), daemon=True)
        proc.start()
        child.close()
        links.append(parent)
        procs.append(proc)
    pending = deque(requests)
    idle = list(links)
    busy: dict[Link, BatchRequest] = {}
    try:
        while pending or busy:
            if not (idle or busy):
                for req in pending:
                    fail(req.source_file_id, "no worker processes left")
                break
            while idle and pending:
                link = idle.pop()
                busy[link] = pending.popleft()
                link.send(busy[link])
            tick()
            for ready in wait(list(busy), timeout=1):
                link = cast(Link, ready)
                req = busy.pop(link)
                try:
                    batch = link.recv()
                except EOFError:
                    fail(req.source_file_id, "worker process exited unexpectedly")
                    continue
                idle.append(link)
                if batch.failure is not None:
                    fail(req.source_file_id, batch.failure)
                    continue
                if commit(batch) and not batch.eof:
                    pending.appendleft(replace(req, start=batch.end))
    finally:
        for link in links:
            with suppress(OSError):
                link.send(None)
            link.close()
        for proc in procs:
            proc.join(WORKER_JOIN_SECONDS)
            if proc.is_alive():
                proc.terminate()
                proc.join()


class SourceUnavailable(Exception):
    """The raw bytes of an event cannot be served from its source file."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EventLocation:
    event_id: str
    source_path: str
    byte_offset: int
    byte_length: int
    size: int | None
    head: str | None


def locate_event(con: duckdb.DuckDBPyConnection, event_id: str) -> EventLocation | None:
    row = con.execute(
        "SELECT e.event_id, f.path, e.byte_offset, e.byte_length, f.size, f.head_sha256 "
        "FROM events e JOIN source_files f USING (source_file_id) WHERE e.event_id = ?",
        [event_id],
    ).fetchone()
    return EventLocation(*row) if row else None


def read_event(loc: EventLocation) -> bytes:
    """The event's source bytes, refusing a file whose hashed head no longer matches."""
    path = Path(loc.source_path)
    try:
        if loc.size is None or loc.head is None or head_sha256(path, loc.size) != loc.head:
            raise SourceUnavailable("source_changed", f"source file {path} changed since ingest")
        with path.open("rb") as f:
            f.seek(loc.byte_offset)
            data = f.read(loc.byte_length)
    except FileNotFoundError:
        raise SourceUnavailable("source_missing", f"source file {path} no longer exists") from None
    except OSError as exc:
        raise SourceUnavailable("source_unreadable", f"source file {path}: {exc}") from None
    if len(data) != loc.byte_length:
        raise SourceUnavailable("source_changed", f"source file {path} is shorter than at ingest")
    return data
