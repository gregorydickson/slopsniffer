"""Pieces shared by harness parsers: value coercion, and the finalize plumbing of the lanes
whose cost comes from the config price table (Codex, Droid).

A lane's finalize runs inside :func:`pending`, which exposes the lane's sessions that have
unsequenced events or no ``sessions`` row as ``lane_pending`` and their events as
``lane_pending_events``; it re-derives those sessions' rows and nothing else.
"""

import json
import logging
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from typing import Any, BinaryIO

import duckdb

from slopsniffer.config import Config
from slopsniffer.contracts import Harness
from slopsniffer.ingest import engine, reader
from slopsniffer.ingest.progress import finalizing, tick
from slopsniffer.ingest.reader import Row


def timestamp(value: object) -> str | None:
    """The instant in canonical ISO form, which the TIMESTAMPTZ cast always accepts."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return None


def as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def block_texts(content: object, types: Iterable[str]) -> list[str]:
    """Texts of the content blocks whose ``type`` is one of ``types``, in order; a string
    content is its own text."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    wanted = set(types)
    return [
        text
        for block in content
        if isinstance(block, dict)
        and block.get("type") in wanted
        and isinstance(text := block.get("text"), str)
    ]


# Snapshot pending identities once; never scan the corpus once per session.
_QUEUE = """
CREATE OR REPLACE TEMP TABLE lane_queue AS
SELECT DISTINCT e.session_id FROM events e JOIN source_files f USING (source_file_id)
WHERE f.harness = $1 AND e.session_id IS NOT NULL
  AND (e.seq IS NULL OR NOT EXISTS (
      SELECT 1 FROM sessions s WHERE s.session_id = e.session_id
  ))
"""
_NEXT = "SELECT min(session_id) FROM lane_queue WHERE ($1::VARCHAR IS NULL OR session_id > $1)"

# One file per session in these lanes, so file order is event order.
_SEQ = """
UPDATE lane_pending_events SET seq = ordered.seq FROM (
    SELECT event_id, row_number() OVER (
        PARTITION BY session_id ORDER BY source_file_id, byte_offset
    ) AS seq
    FROM lane_pending_events
) AS ordered
WHERE lane_pending_events.event_id = ordered.event_id
"""


def pending(
    con: duckdb.DuckDBPyConnection,
    harness: Harness,
    derive: Callable[[], None],
    sequence: str = _SEQ,
) -> None:
    """Visit pending sessions once, isolating failures and publishing completion last.

    Body batches can commit before publication; an interrupted session has no sessions
    row and is replaced on retry. Only narrow identities participate in session windows.
    """
    con.execute(_QUEUE, [harness])
    failures: list[str] = []
    total = con.execute("SELECT count(*) FROM lane_queue").fetchall()[0][0]
    completed = 0
    finalizing(completed, total)
    previous: str | None = None
    while True:
        row = con.execute(_NEXT, [previous]).fetchone()
        if row is None or row[0] is None:
            break
        previous = row[0]
        tick(f"finalize session {previous}")
        _isolated_session(con, harness, previous, sequence, failures, derive)
        # Persist updates and derived append buffers before the next session. Otherwise
        # committed, uncheckpointed data can pin memory across session boundaries.
        con.execute("CHECKPOINT")
        completed += 1
        finalizing(completed, total)
    with engine.transaction(con):
        # Re-ingestion may remove a session's last event entirely.
        con.execute(
            "DELETE FROM tool_calls WHERE use_event_id NOT IN (SELECT event_id FROM events)"
        )
        con.execute(
            "DELETE FROM session_cost WHERE session_id NOT IN (SELECT session_id FROM sessions)"
        )

    con.execute("DROP TABLE lane_queue")
    if failures:
        raise engine.FinalizeFailures(failures)


def _isolated_session(
    con: duckdb.DuckDBPyConnection,
    harness: Harness,
    session_id: str,
    sequence: str,
    failures: list[str],
    derive: Callable[[], None],
) -> None:
    try:
        with _session(con, harness, session_id, sequence):
            derive()
    except duckdb.Error as exc:
        logging.getLogger(__name__).exception("%s session %s finalize failed", harness, session_id)
        files = con.execute(
            "SELECT DISTINCT f.source_file_id, f.path FROM source_files f "
            "JOIN events e USING (source_file_id) WHERE f.harness = ? AND e.session_id = ?",
            [harness, session_id],
        ).fetchall()
        with engine.transaction(con):
            for fid, path in files:
                con.execute(
                    "INSERT INTO ingest_errors (source_file_id, error) VALUES (?, ?)",
                    [fid, f"finalize session {session_id}: {type(exc).__name__}: {exc}"],
                )
                failures.append(path)


@contextmanager
def _session(
    con: duckdb.DuckDBPyConnection, harness: Harness, session_id: str, sequence: str
) -> Iterator[None]:
    try:
        with engine.transaction(con):
            con.execute(
                "CREATE OR REPLACE TEMP TABLE lane_pending AS SELECT ?::VARCHAR AS session_id",
                [session_id],
            )
            con.execute(
                "CREATE OR REPLACE TEMP TABLE lane_pending_events AS "
                "SELECT e.* EXCLUDE (parsed) FROM events e "
                "JOIN source_files f USING (source_file_id) "
                "JOIN lane_pending USING (session_id) WHERE f.harness = ?",
                [harness],
            )
            con.execute(
                "DELETE FROM session_relations WHERE event_id IN "
                "(SELECT event_id FROM lane_pending_events)"
            )
            bounds = con.execute(
                "SELECT min(source_file_id), max(source_file_id) FROM lane_pending_events"
            ).fetchall()[0]
            # Sequence in the narrow working table; publish the durable seq only once
            # every batch is stored. A crash between batch commits has no sessions row.
            con.execute(sequence)
            for table in ("sessions", "tool_calls", "session_cost"):
                con.execute(f"DELETE FROM {table} WHERE session_id = ?", [session_id])
            yield
            con.execute(
                "UPDATE events SET seq = p.seq FROM lane_pending_events p "
                "WHERE events.event_id = p.event_id AND events.source_file_id BETWEEN ? AND ?",
                list(bounds),
            )
            con.execute("DROP TABLE lane_pending_events; DROP TABLE lane_pending")
    except BaseException:
        # Tool bodies commit in writer-sized units. Remove any committed partial
        # derivation, keeping this session pending for replay, including after a
        # failure in the final publish/commit rather than in an individual statement.
        with suppress(duckdb.TransactionException):
            con.rollback()
        with engine.transaction(con):
            for table in ("sessions", "tool_calls", "session_cost"):
                con.execute(f"DELETE FROM {table} WHERE session_id = ?", [session_id])
            con.execute(
                "DELETE FROM session_relations WHERE event_id IN "
                "(SELECT event_id FROM events WHERE session_id = ?)",
                [session_id],
            )
            con.execute(
                "UPDATE events SET seq = NULL WHERE session_id = ? AND source_file_id IN "
                "(SELECT source_file_id FROM source_files WHERE harness = ?)",
                [session_id, harness],
            )
        _drop_working_tables(con)
        raise


def _drop_working_tables(con: duckdb.DuckDBPyConnection) -> None:
    for catalog, kind in (("duckdb_views()", "VIEW"), ("duckdb_tables()", "TABLE")):
        name = "view_name" if kind == "VIEW" else "table_name"
        rows = con.execute(
            f"SELECT {name} FROM {catalog} WHERE temporary AND "
            f"regexp_matches({name}, '^(lane_|codex_|droid_|claude_|crush_)') "
            f"AND {name} <> 'lane_queue'"
        ).fetchall()
        for (table,) in rows:
            con.execute(f"DROP {kind} {engine._ident(table)}")


def insert_costs(
    con: duckdb.DuckDBPyConnection, config: Config, *, usage_table: str = "usage"
) -> None:
    """``session_cost`` of each pending session with usage, priced from the config table."""
    from slopsniffer import pricing

    pricing.install(con, config, usage_table=usage_table)
    con.execute(
        "INSERT INTO session_cost (session_id, cost_usd, method) "
        + pricing.TOTALS.format(events="lane_pending_events")
    )


def json_batches(
    con: duckdb.DuckDBPyConnection,
    config: Config,
    extract: Callable[[dict[str, Any], str, dict[str, Any]], Iterable[Row]],
    bodies: BinaryIO | None = None,
) -> None:
    """Decode JSON one event at a time, with SQL results capped by writer row/byte limits.

    Only identities participate in windows. Raw trees and expanded blocks never enter
    a session-wide SQL operator. As in the writer, one oversized event is indivisible.
    """
    con.execute(
        "CREATE TEMP TABLE lane_json_batches AS SELECT event_id, "
        "(row_number() OVER w - 1) // $1 AS row_group, "
        "(sum(byte_length) OVER w - byte_length) // $2 AS byte_group "
        "FROM lane_pending_events WINDOW w AS (ORDER BY source_file_id, byte_offset "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
        [config.ingest.batch_rows, reader.BATCH_BYTES],
    )
    previous = (-1, -1)
    while groups := con.execute(
        "SELECT row_group, byte_group FROM lane_json_batches "
        "WHERE (row_group, byte_group) > (?, ?) ORDER BY row_group, byte_group LIMIT 1",
        list(previous),
    ).fetchall():
        previous = groups[0][0], groups[0][1]
        ids = [
            row[0]
            for row in con.execute(
                "SELECT event_id FROM lane_json_batches WHERE row_group = $1 AND byte_group = $2",
                list(previous),
            ).fetchall()
        ]
        values: list[Row] = []
        for record, kind, location in _json_events(con, ids):
            for row in extract(record, kind, location):
                if row.table in ("lane_uses", "lane_results"):
                    if bodies is None:
                        raise ValueError("tool rows require a body spool")
                    raw = json.dumps(
                        dict(row.values), ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    offset = bodies.tell()
                    bodies.write(raw)
                    row = Row(
                        row.table,
                        {
                            key: row.values[key]
                            for key in ("event_id", "session_id", "seq", "call_id")
                        }
                        | {"body_offset": offset, "body_length": len(raw)},
                    )
                values.append(row)
        engine._insert_rows(con, values)
    con.execute("DROP TABLE lane_json_batches")


def json_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def result_text(value: object, types: Iterable[str]) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return None
    parts = [
        text
        for item in value
        if isinstance(item, dict)
        and item.get("type") in set(types)
        and (text := json_string(item.get("text"))) is not None
    ]
    return "\n".join(parts) if parts else None


@contextmanager
def tool_tables(con: duckdb.DuckDBPyConnection, config: Config) -> Iterator[BinaryIO]:
    """Keep large bodies on disk; SQL windows and joins see only ids and file offsets."""
    directory = config.data_dir / "tmp"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=directory) as bodies:
        con.execute(
            "CREATE TEMP TABLE lane_uses (event_id VARCHAR, session_id VARCHAR, seq BIGINT, "
            "call_id VARCHAR, body_offset BIGINT, body_length BIGINT); "
            "CREATE TEMP TABLE lane_results (event_id VARCHAR, session_id VARCHAR, seq BIGINT, "
            "call_id VARCHAR, body_offset BIGINT, body_length BIGINT)"
        )
        yield bodies


def _json_events(
    con: duckdb.DuckDBPyConnection,
    ids: list[str],
) -> Iterator[tuple[dict[str, Any], str, dict[str, Any]]]:
    # Resolve physical positions afresh for this batch. Row-id ranges fetch only
    # those rows; an id semi-join can materialize an entire raw JSON vector first.
    # Never carry rowids across a commit, where a checkpoint may compact storage.
    bounds = con.execute(
        "SELECT min(source_file_id), max(source_file_id) FROM lane_pending_events"
    ).fetchall()[0]
    locations = con.execute(
        "SELECT e.rowid, p.event_id, p.session_id, p.source_file_id, p.byte_offset, "
        "p.byte_length, p.seq, p.record_type FROM events e "
        "JOIN lane_pending_events p USING (event_id) "
        "WHERE p.event_id IN (SELECT unnest($1::VARCHAR[])) "
        "AND e.source_file_id BETWEEN $2 AND $3 ORDER BY e.rowid",
        [ids, *bounds],
    ).fetchall()
    metadata = {row[1]: row[2:] for row in locations}
    spans: list[tuple[int, int]] = []
    for rowid, *_ in locations:
        if spans and spans[-1][1] == rowid:
            spans[-1] = (spans[-1][0], rowid + 1)
        else:
            spans.append((rowid, rowid + 1))
    for start, end in spans:
        rows = con.sql(
            "SELECT event_id, parsed::VARCHAR FROM events WHERE rowid >= $1 AND rowid < $2",
            params=[start, end],
        ).iter_rows(batch_size=1)
        for eid, raw in rows:
            sid, source, offset, length, seq, kind = metadata[eid]
            if raw is None:
                continue
            value = json.loads(raw)
            yield (
                value if isinstance(value, dict) else {},
                kind,
                {
                    "event_id": eid,
                    "session_id": sid,
                    "source_file_id": source,
                    "byte_offset": offset,
                    "byte_length": length,
                    "seq": seq,
                },
            )


def insert_tools(
    con: duckdb.DuckDBPyConnection,
    config: Config,
    bodies: BinaryIO,
    error_default: bool | None = None,
) -> None:
    """Pair narrow identities, then read/write one row/byte-bounded body batch.

    Spooling during the first JSON pass avoids re-scanning/decompressing raw vectors
    for every scattered tool result. A result may precede its use or be in another batch.
    """
    con.execute(
        "CREATE TEMP TABLE lane_pairs AS WITH uses AS ("
        "SELECT rowid AS use_id, * FROM lane_uses "
        "QUALIFY row_number() OVER (PARTITION BY session_id, call_id ORDER BY seq, rowid) = 1"
        "), results AS (SELECT * FROM lane_results "
        "QUALIFY row_number() OVER (PARTITION BY session_id, call_id ORDER BY seq, rowid) = 1) "
        "SELECT u.event_id, u.call_id, u.body_offset, u.body_length, r.event_id AS result_id, "
        "r.body_offset AS result_offset, r.body_length AS result_length, "
        "(row_number() OVER w - 1) // $1 AS row_group, "
        "(sum(u.body_length + coalesce(r.body_length, 0)) OVER w - u.body_length "
        "- coalesce(r.body_length, 0)) // $2 AS byte_group "
        "FROM uses u LEFT JOIN results r USING (session_id, call_id) "
        "WINDOW w AS (ORDER BY u.seq, u.use_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
        [config.ingest.batch_rows, reader.BATCH_BYTES],
    )
    previous = (-1, -1)
    while groups := con.execute(
        "SELECT row_group, byte_group FROM lane_pairs WHERE (row_group, byte_group) > (?, ?) "
        "ORDER BY row_group, byte_group LIMIT 1",
        list(previous),
    ).fetchall():
        previous = groups[0][0], groups[0][1]
        pairs = con.execute(
            "SELECT event_id, call_id, body_offset, body_length, result_id, result_offset, "
            "result_length FROM lane_pairs WHERE row_group = $1 AND byte_group = $2",
            list(previous),
        ).fetchall()
        values = []
        for eid, call, offset, length, rid, roffset, rlength in pairs:
            bodies.seek(offset)
            use = json.loads(bodies.read(length))
            result = {}
            if rid is not None:
                bodies.seek(roffset)
                result = json.loads(bodies.read(rlength))
            values.append(
                Row(
                    "tool_calls",
                    {
                        "call_id": call,
                        "session_id": use["session_id"],
                        "use_event_id": eid,
                        "result_event_id": rid,
                        "tool_name": use["tool_name"],
                        "input": use["input"],
                        "result_text": result.get("result_text"),
                        "is_error": result.get("is_error"),
                    },
                )
            )
        columns = (
            "call_id",
            "session_id",
            "use_event_id",
            "result_event_id",
            "tool_name",
            "input",
            "result_text",
            "is_error",
        )
        con.execute(
            "INSERT INTO tool_calls (call_id, session_id, use_event_id, result_event_id, "
            "tool_name, input, result_text, is_error) "
            "SELECT unnest($1::VARCHAR[]), unnest($2::VARCHAR[]), unnest($3::VARCHAR[]), "
            "unnest($4::VARCHAR[]), unnest($5::VARCHAR[]), unnest($6::VARCHAR[])::JSON, "
            "unnest($7::VARCHAR[]), CASE WHEN unnest($4::VARCHAR[]) IS NOT NULL THEN "
            "coalesce(TRY_CAST(unnest($8::VARCHAR[]) AS BOOLEAN), $9::BOOLEAN) END",
            [[row.values[column] for row in values] for column in columns] + [error_default],
        )
        # Bound transaction buffers as well as decoding memory. Completion is published last.
        con.commit()
        con.begin()
    con.execute("DROP TABLE lane_pairs; DROP TABLE lane_uses; DROP TABLE lane_results")


def reclassify(
    con: duckdb.DuckDBPyConnection,
    harness: Harness,
    kinds: dict[str, str],
    *,
    rederive: set[str],
) -> None:
    """Backfill newly understood records in existing stores without rereading sources.

    Classifications and invalidation commit together; a crash leaves pending sessions
    for the normal bounded finalizer. No source bytes or bodies enter this migration.
    """
    with engine.transaction(con):
        con.execute(
            "CREATE TEMP TABLE lane_reclassified AS SELECT e.event_id, e.record_type "
            "FROM events e JOIN source_files f USING(source_file_id) "
            "WHERE f.harness=? AND e.kind='unknown' "
            "AND e.record_type IN (SELECT unnest(?::VARCHAR[]))",
            [harness, list(kinds)],
        )
        con.execute(
            "UPDATE events SET seq=NULL WHERE event_id IN ("
            "SELECT event_id FROM lane_reclassified WHERE record_type IN "
            "(SELECT unnest(?::VARCHAR[])))",
            [sorted(rederive)],
        )
        con.execute(
            "UPDATE events SET kind=k.kind FROM "
            "(SELECT unnest(?::VARCHAR[]) AS record_type, unnest(?::VARCHAR[]) AS kind) k "
            "WHERE events.record_type=k.record_type AND events.event_id IN "
            "(SELECT event_id FROM lane_reclassified)",
            [list(kinds), list(kinds.values())],
        )
        con.execute("DROP TABLE lane_reclassified")
