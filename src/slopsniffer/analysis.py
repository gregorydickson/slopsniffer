"""Shared whole-session scopes for post-ingest transforms.

Only the queue contains history-wide state: ids and scalar row/byte totals, never
bodies. Wide expressions run after materializing one scope. A session is indivisible
because lag/lead and evidence lists must see all its turns.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import duckdb

from slopsniffer.ingest.reader import BATCH_BYTES, BATCH_ROWS

# Explicit resource policies; membership tests against the CLI registry force review
# when another command is introduced. Query is caller SQL, governed by DuckDB's pool.
COMMAND_POLICIES = {
    "ingest": "ingest writer batches",
    "signals": "whole-session scopes; inventory-only global pass",
    "outcomes": "whole-session linkage; bounded GitHub responses",
    "grade": "narrow ranking; render one selected session",
    "propose": "spooled verdicts; shared-budget prompts and evidence fragments",
    "report": "top-N wide rows; narrow rollup aggregates",
    "status": "scalar aggregates over narrow columns",
    "query": "caller-controlled SQL under the configured memory pool",
    "rehydrate": "one event",
    "run": "sequential ingest/signals/outcomes/optional grade",
}


@contextmanager
def session_batches(
    con: duckdb.DuckDBPyConnection, batch_rows: int = BATCH_ROWS
) -> Iterator[Iterator[list[str]]]:
    """Reuse ingest's row/byte budgets; an oversized session travels alone.

    The queue is a spillable narrow aggregate (one row per session). Keyset pages
    avoid retaining every session in Python and remain valid across batch commits.
    """
    con.execute(
        "CREATE TEMP TABLE analysis_queue AS "
        "SELECT session_id, count(*) AS rows, sum(byte_length) AS bytes FROM events "
        "WHERE session_id IS NOT NULL GROUP BY session_id "
        "UNION ALL SELECT session_id, 0, 0 FROM sessions s WHERE NOT EXISTS "
        "(SELECT 1 FROM events e WHERE e.session_id = s.session_id)"
    )
    try:
        yield _batches(con, batch_rows)
    finally:
        con.execute("DROP TABLE analysis_queue")


def _batches(con: duckdb.DuckDBPyConnection, cap: int) -> Iterator[list[str]]:
    last: str | None = None
    batch: list[str] = []
    rows = size = 0
    while page := con.execute(
        "SELECT session_id, rows, bytes FROM analysis_queue "
        "WHERE ($1::VARCHAR IS NULL OR session_id > $1) ORDER BY session_id LIMIT $2",
        [last, cap],
    ).fetchall():
        for sid, n, length in page:
            if batch and (rows + n > cap or size + length > BATCH_BYTES):
                yield batch
                batch, rows, size = [], 0, 0
            batch.append(sid)
            rows += n
            size += length
            last = sid
        # Do not fetch another page while a completed scope remains retained.
        if batch:
            yield batch
            batch, rows, size = [], 0, 0

    if last is None:
        yield []


@contextmanager
def scope(con: duckdb.DuckDBPyConnection, ids: list[str]) -> Iterator[None]:
    """Materialization fences keep wide transforms behind session selection."""
    con.execute(
        "CREATE TEMP TABLE analysis_ids AS SELECT unnest(?::VARCHAR[]) AS session_id", [ids]
    )
    try:
        # Resolve this scope once. Every session-local kind shares these narrow
        # event rows instead of repeating a history scan for each window/join.
        con.execute(
            "CREATE TEMP TABLE analysis_events AS "
            "SELECT e.event_id, e.session_id, e.seq, e.kind, e.source_file_id, "
            "e.byte_offset FROM main.events e SEMI JOIN analysis_ids USING (session_id)"
        )
        try:
            yield
        finally:
            con.execute("DROP TABLE analysis_events")
    finally:
        con.execute("DROP TABLE analysis_ids")


def scoped(sql: str) -> str:
    """Attach materialized, filtered inputs without changing a kind's semantics.

    All event-bearing sources are covered centrally, including messages (which
    have no session_id). CTE names shadow base tables only inside this statement.
    """
    sources = {
        "events": "SELECT * FROM temp.main.analysis_events",
        "sessions": "SELECT s.* FROM main.sessions s SEMI JOIN analysis_ids USING (session_id)",
        "tool_calls": "SELECT t.* FROM main.tool_calls t SEMI JOIN analysis_ids USING (session_id)",
        "skill_invocations": (
            "SELECT i.* FROM main.skill_invocations i SEMI JOIN analysis_ids USING (session_id)"
        ),
        "signal_turns": (
            "SELECT t.* FROM temp.main.signal_turns t SEMI JOIN analysis_ids USING (session_id)"
        ),
        "signal_compactions": (
            "SELECT c.* FROM temp.main.signal_compactions c "
            "SEMI JOIN analysis_ids USING (session_id)"
        ),
        "messages": "SELECT m.* FROM main.messages m SEMI JOIN events e USING (event_id)",
    }
    names = {name.casefold() for name in sources if re.search(rf"\b{name}\b", sql, re.IGNORECASE)}
    if "messages" in names:
        names.add("events")
    ctes = ", ".join(
        f"{name} AS MATERIALIZED ({body})"
        for name, body in sources.items()
        if name.casefold() in names
    )
    if not ctes:
        return sql
    return _with_inputs(sql, ctes)


def _with_inputs(sql: str, ctes: str) -> str:
    """Use DuckDB's lexer so comments, quoted text and keyword case cannot bypass scopes."""
    raw = sql.encode("utf-8")
    tokens = duckdb.tokenize(sql)
    for index, (position, kind) in enumerate(tokens):
        if kind != duckdb.token_type.keyword:
            continue
        tail = raw[position:]
        if tail[:6].upper() == b"SELECT":
            return (raw[:position] + b"WITH " + ctes.encode() + b" " + tail).decode()
        if tail[:4].upper() == b"WITH":
            end = position + 4
            recursive = False
            for next_position, next_kind in tokens[index + 1 :]:
                if next_kind == duckdb.token_type.comment:
                    continue
                if raw[next_position : next_position + 9].upper() == b"RECURSIVE":
                    recursive = True
                    end = next_position + 9
                break
            prefix = b"WITH RECURSIVE " if recursive else b"WITH "
            return (raw[:position] + prefix + ctes.encode() + b", " + raw[end:]).decode()
    raise ValueError("scoped SQL must contain a SELECT")


def event_batches(
    con: duckdb.DuckDBPyConnection, batch_rows: int = BATCH_ROWS
) -> Iterator[tuple[int, int]]:
    """Physical event spans bounded by the ingest row/byte caps.

    Resolve rowids afresh after every yield/commit, like common._json_events.
    Reading by physical spans prevents a join from first decoding a history-wide
    JSON/text vector. Only narrow locations are retained in Python.
    """
    last: str | None = None
    while page := con.execute(
        "SELECT rowid, event_id, byte_length FROM main.events WHERE rowid > coalesce("
        "(SELECT rowid FROM main.events WHERE event_id = $1), -1) ORDER BY rowid LIMIT $2",
        [last, batch_rows],
    ).fetchall():
        start = page[0][0]
        size = 0
        end = start
        anchor = page[0][1]
        for position, event_id, length in page:
            if size and size + length > BATCH_BYTES:
                break
            size += length
            end = position + 1
            anchor = event_id
        yield start, end
        last = anchor


def _body_predicate(
    con: duckdb.DuckDBPyConnection, table: str, key: str, start: int, end: int
) -> str:
    positions = con.execute(
        f"SELECT m.rowid FROM main.{table} m JOIN main.events e ON m.{key} = e.event_id "
        "WHERE e.rowid >= ? AND e.rowid < ? ORDER BY m.rowid",
        [start, end],
    ).fetchall()
    spans: list[tuple[int, int]] = []
    for (position,) in positions:
        if spans and spans[-1][1] == position:
            spans[-1] = spans[-1][0], position + 1
        else:
            spans.append((position, position + 1))
    return " OR ".join(f"(rowid >= {a} AND rowid < {b})" for a, b in spans) or "false"


def event_scoped(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    start: int,
    end: int,
    *,
    sessions_all: bool = False,
    event_input: str | None = None,
) -> str:
    """Fence event-local transforms before decoding bodies; spans are engine integers."""
    events = (
        f"SELECT * FROM main.events WHERE rowid >= {start} AND rowid < {end}"
        if event_input is None
        else f"SELECT * FROM {event_input}"
    )
    sources = [f"events AS MATERIALIZED ({events})"]
    for table, key in (("messages", "event_id"), ("tool_calls", "result_event_id")):
        if re.search(rf"\b{table}\b", sql, re.IGNORECASE):
            predicate = _body_predicate(con, table, key, start, end)
            sources.append(
                f"{table} AS MATERIALIZED (SELECT * FROM main.{table} WHERE {predicate})"
            )
    if re.search(r"\bsessions\b", sql, re.IGNORECASE):
        where = "" if sessions_all else " WHERE session_id IN (SELECT session_id FROM events)"
        sources.append("sessions AS MATERIALIZED (SELECT * FROM main.sessions" + where + ")")
    return _with_inputs(sql, ", ".join(sources))


def create_replacement(con: duckdb.DuckDBPyConnection, table: str, staging: str) -> None:
    """Create an empty durable staging table with the published table's constraints.

    Catalog lookup is one DDL string. CTAS would silently remove NOT NULL/CHECK
    constraints when the staging table replaces the published table.
    """
    row = con.execute(
        "SELECT sql FROM duckdb_tables() WHERE database_name = current_database() "
        "AND schema_name = 'main' AND table_name = ? AND NOT temporary",
        [table],
    ).fetchone()
    if row is None:
        raise ValueError(f"missing replacement target: {table}")
    con.execute(f"CREATE OR REPLACE TABLE {staging} (" + row[0].partition("(")[2])


@contextmanager
def event_transaction(
    con: duckdb.DuckDBPyConnection, batches: Iterator[tuple[int, int]]
) -> Iterator[tuple[int, int] | None]:
    """Select positions and consume their bodies in one transaction snapshot.

    Only the stable last event identity crosses commits; checkpoints cannot
    invalidate physical spans halfway through a batch.
    """
    con.begin()
    try:
        yield next(batches, None)
        con.commit()
    except BaseException:
        con.rollback()
        raise


def sized_batches[T](
    items: Iterator[tuple[T, int]], batch_rows: int = BATCH_ROWS
) -> Iterator[list[T]]:
    """Greedily fill the shared inclusive row/byte budgets, preserving encounter order."""
    batch: list[T] = []
    size = 0
    for item, length in items:
        if batch and (len(batch) >= batch_rows or size + length > BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += length
    if batch:
        yield batch


def physical_row(
    con: duckdb.DuckDBPyConnection, table: str, columns: str, predicate: str, values: list[object]
) -> tuple[Any, ...]:
    """Resolve a narrow identity before reading its wide columns, with no commit between."""
    position = con.execute(f"SELECT rowid FROM {table} WHERE {predicate}", values).fetchall()[0][0]
    return con.execute(f"SELECT {columns} FROM {table} WHERE rowid = ?", [position]).fetchall()[0]
