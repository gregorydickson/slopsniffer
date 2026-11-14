"""Materialize the ``signals`` table and report signals and rollups.

Each signal kind is one SELECT in ``<kind>.sql`` yielding ``(session_id, severity,
evidence_event_ids, details)``; the subject in ``session_id`` is a session, except for
``never_fired_skill`` (the skill's inventory path) and a stale pickle run (its run id).
Thresholds reach the SQL only as bound ``$name`` parameters taken from the config.
"""

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from importlib import resources
from typing import TextIO

import duckdb

from slopsniffer import analysis
from slopsniffer.config import Config
from slopsniffer.contracts import (
    RollupDimension,
    Signal,
    SignalsReport,
    SignalsRunSummary,
)
from slopsniffer.ingest.reader import BATCH_ROWS
from slopsniffer.signals.text import (
    CLAUSE_PHRASES,
    CORRECTION_CLAUSE,
    OWN_TEXT_PATTERN,
    RECOVERY_RETRY,
)

KINDS = (
    "rerun_loop",
    "edit_churn",
    "unchanged_retry",
    "user_correction",
    "skill_abandoned",
    "never_fired_skill",
    "orphaned_session",
    "context_overflow",
)
# Global kinds read installed inventory / narrow identities only. Orphaned pickle
# runs have a separate global arm; session-local orphan detection stays scoped.
SESSION_KINDS = frozenset(
    {
        "rerun_loop",
        "edit_churn",
        "unchanged_retry",
        "user_correction",
        "skill_abandoned",
        "orphaned_session",
        "context_overflow",
    }
)
GLOBAL_KINDS = frozenset({"never_fired_skill"})
DIMENSIONS: tuple[RollupDimension, ...] = ("lane", "skill", "project", "day", "week")
_PARAMETER = re.compile(r"\$([a-z_]+)")

# Conservative verification executable/subcommand positions. Filenames and quoted
# mentions of tests do not establish that a command actually ran a verifier.
VERIFICATION_COMMAND = (
    r"""(?i)^\s*(?:cd\s+(?:"[^"]*"|'[^']*'|[^"';&|]+)\s*&&\s*)?"""
    r"(?:timeout\s+\S+\s+)?"
    r"(?:(?:uv|poetry)\s+run\s+|npx\s+|pnpm\s+exec\s+|bunx\s+)?"
    r"(?:(?:\S*/)?(?:pytest|vitest|jest|tsc|mypy|eslint)"
    r"|python[0-9.]*\s+-m\s+(?:pytest|unittest|compileall)"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?"
    r"(?:test|check|build|lint|typecheck|verify)(?::[\w:.-]+)?"
    r"|(?:go|cargo|make|dotnet)\s+(?:test|check|build|lint|clippy)"
    r"|ruff\s+check)(?:\s|$)"
)
# Pure, literal shell output/exit builtins cannot inspect changing external state.
NOOP_COMMAND = r"""(?i)(?:true|false|:|(?:echo|printf)(?: [a-z0-9_ ./:"'%=-]+)?)"""

_INSERT = """
INSERT INTO signals (signal_id, kind, session_id, severity, evidence_event_ids, details,
                     computed_at)
SELECT substr(sha256(concat_ws(chr(0), $kind, session_id,
                               array_to_string(list_sort(evidence_event_ids), ','),
                               CAST(details AS VARCHAR))), 1, 32),
       $kind, session_id, severity, evidence_event_ids, details, $computed_at
FROM ({select})
"""

# Subjects prefer a session id, then a Pickle run id, then a skill path.
_SUBJECTS = """
WITH subjects AS (
    SELECT session_id AS subject, harness, project, started_at, 1 AS rank FROM sessions
    UNION ALL
    SELECT pickle_run_id, 'pickle', NULLIF(regexp_extract(working_dir, '([^/]+)/*$', 1), ''),
           to_timestamp(start_time_epoch), 2
    FROM pickle_runs
    UNION ALL
    SELECT path, 'skills', NULLIF(regexp_extract(repo, '([^/]+)/*$', 1), ''), NULL, 3
    FROM skills
), owners AS (
    SELECT * FROM subjects
    QUALIFY row_number() OVER (PARTITION BY subject ORDER BY rank) = 1
)
"""
# The subject window contains narrow metadata only, never evidence or JSON.
_FILTERS = """
FROM signals s JOIN owners j ON j.subject = s.session_id
WHERE (len($kinds::VARCHAR[]) = 0 OR list_contains($kinds::VARCHAR[], s.kind))
  AND ($project::VARCHAR IS NULL OR j.project = $project::VARCHAR)
  AND ($lane::VARCHAR IS NULL OR j.harness = $lane::VARCHAR)
  AND ($since::TIMESTAMPTZ IS NULL OR j.harness = 'skills'
       OR j.started_at >= $since::TIMESTAMPTZ)
"""
_SIGNAL_COLUMNS = """
SELECT s.signal_id, s.kind, s.session_id, j.harness, j.project, s.severity,
       s.evidence_event_ids, CAST(s.details AS VARCHAR)
"""
_REPORT = _SUBJECTS + _SIGNAL_COLUMNS + _FILTERS
_REPORT_ORDER = "ORDER BY s.kind, s.session_id, s.signal_id"

_ROLLUP = "SELECT * FROM rollup($dim, $project::VARCHAR, $lane::VARCHAR, $since::TIMESTAMPTZ)"


@dataclass(frozen=True)
class Filters:
    kinds: tuple[str, ...] = ()
    project: str | None = None
    lane: str | None = None
    since: date | None = None


def _sql(name: str) -> str:
    return resources.files(__package__).joinpath(f"{name}.sql").read_text("utf-8")


def _execute(
    con: duckdb.DuckDBPyConnection, sql: str, values: Mapping[str, object]
) -> duckdb.DuckDBPyConnection:
    """Run ``sql`` with exactly the named parameters it uses; DuckDB rejects unused ones."""
    return con.execute(sql, {name: values[name] for name in set(_PARAMETER.findall(sql))})


def _start_of(day: date | None) -> datetime | None:
    return None if day is None else datetime.combine(day, time(), UTC)


def _first_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Match DuckDB JSON-path lookup: the first duplicate object key wins."""
    fields: dict[str, object] = {}
    for key, value in pairs:
        if key not in fields:
            fields[key] = value
    return fields


def _event_flags(con: duckdb.DuckDBPyConnection, start: int, end: int) -> None:
    """Decode one raw event at a time, retaining only the flags signal SQL uses.

    DuckDB's JSON extension can retain parsed trees for a vector even when the
    selected fields are tiny. Python decoding releases each tree before the next.
    """
    rows = []
    for eid, sid, seq, record_type, kind, raw in con.sql(
        "SELECT event_id, session_id, seq, record_type, kind, parsed::VARCHAR "
        "FROM main.events WHERE rowid >= $1 AND rowid < $2",
        params=[start, end],
    ).iter_rows(batch_size=1):
        parsed = json.loads(raw, object_pairs_hook=_first_fields) if raw is not None else {}
        flags = {
            key: None if isinstance(parsed[key], (dict, list)) else parsed[key]
            for key in ("isMeta", "isCompactSummary", "subtype")
            if isinstance(parsed, dict) and key in parsed
        }
        message = parsed.get("message") if isinstance(parsed, dict) else None
        if isinstance(message, dict) and message.get("visibility") == "user_only":
            flags["message"] = {"visibility": "user_only"}
        del message
        del parsed
        compact = json.dumps(flags, ensure_ascii=False)
        rows.append((eid, sid, seq, record_type, kind, compact))
    con.execute(
        "CREATE OR REPLACE TEMP TABLE signal_event_flags "
        "(event_id VARCHAR, session_id VARCHAR, seq BIGINT, record_type VARCHAR, "
        "kind VARCHAR, parsed JSON)"
    )
    if rows:
        con.execute(
            "INSERT INTO signal_event_flags SELECT unnest($1::VARCHAR[]), "
            "unnest($2::VARCHAR[]), unnest($3::BIGINT[]), unnest($4::VARCHAR[]), "
            "unnest($5::VARCHAR[]), unnest($6::VARCHAR[])::JSON",
            [list(column) for column in zip(*rows, strict=True)],
        )


_RESULT_DDL = (
    "CREATE TEMP TABLE signal_result_flags (session_id VARCHAR, call_id VARCHAR, "
    "result_seq BIGINT, result_hash VARCHAR, "
    "failed BOOLEAN, transient BOOLEAN)"
)
_EXIT_FAILURE = re.compile(
    r"""(?:exit(?:ed with)? code|exit_code)["']?[:= ]+["']?-?[1-9][0-9]*\b""", re.I
)
_TRANSIENT = re.compile(
    r"timeout|timed out|rate.limit|temporar|network|connection|unavailable|quota", re.I
)


def _failed_result(text: str | None, error: bool | None) -> bool:
    if error:
        return True
    body = text or ""
    try:
        parsed = json.loads(body)
    except ValueError:
        return bool(_EXIT_FAILURE.search(body))
    if not isinstance(parsed, dict):
        return False
    metadata = parsed.get("metadata")
    for source in (parsed, metadata):
        if isinstance(source, dict):
            code = source.get("exit_code")
            if isinstance(code, int) and not isinstance(code, bool):
                return code != 0
    return False


def _result_flags(
    con: duckdb.DuckDBPyConnection, start: int | None = None, end: int | None = None
) -> None:
    """Reduce one result body at a time, before session windows or JSON decoding."""
    source = "analysis_events" if start is None else "events"
    predicate = "" if start is None else " WHERE e.rowid >= ? AND e.rowid < ?"
    positions = con.sql(
        "SELECT t.rowid, t.session_id, t.call_id, t.is_error, r.seq, "
        "coalesce(r.byte_length, 16777216) "
        "FROM tool_calls t JOIN " + source + " e ON e.event_id = t.use_event_id "
        "LEFT JOIN main.events r ON r.event_id = t.result_event_id" + predicate,
        params=[] if start is None else [start, end],
    ).iter_rows(batch_size=1)
    items = ((row[:5], int(row[5])) for row in positions)
    for batch in analysis.sized_batches(items):
        identities = {position: (sid, cid, error, seq) for position, sid, cid, error, seq in batch}
        rows = []
        for position, text in con.sql(
            "SELECT rowid, result_text FROM tool_calls "
            "WHERE rowid IN (SELECT unnest($1::BIGINT[]))",
            params=[list(identities)],
        ).iter_rows(batch_size=1):
            sid, cid, error, result_seq = identities[position]
            rows.append(
                (
                    sid,
                    cid,
                    result_seq,
                    hashlib.sha256(text.encode()).hexdigest() if text is not None else None,
                    _failed_result(text, error),
                    bool(_TRANSIENT.search(text or "")),
                )
            )
        if rows:
            con.execute(
                "INSERT INTO signal_result_flags SELECT unnest($1::VARCHAR[]), "
                "unnest($2::VARCHAR[]), unnest($3::BIGINT[]), unnest($4::VARCHAR[]), "
                "unnest($5::BOOLEAN[]), unnest($6::BOOLEAN[])",
                [list(column) for column in zip(*rows, strict=True)],
            )


def compute(
    con: duckdb.DuckDBPyConnection, config: Config, since: date | None, now: datetime
) -> SignalsRunSummary:
    """Replace every signal with a fresh computation and (re)create the rollup views."""
    thresholds = config.signals
    values: dict[str, object] = {
        "rerun_min": thresholds.rerun_min,
        "churn_min": thresholds.churn_min,
        "verification_command": VERIFICATION_COMMAND,
        "noop_command": NOOP_COMMAND,
        "own_text_pattern": OWN_TEXT_PATTERN,
        "clause_phrases": list(CLAUSE_PHRASES),
        "correction_clause": CORRECTION_CLAUSE,
        "recovery_retry": RECOVERY_RETRY,
        "abandon_window": thresholds.abandon_window,
        "orphan_hours": thresholds.orphan_hours,
        "correction_lexicon": list(thresholds.correction_lexicon),
        "since": _start_of(since),
        "now": now,
        "computed_at": now,
    }
    # Staging commits release each batch while preserving the previous published
    # result if a later kind fails. No history-sized DELETE transaction is pinned.
    analysis.create_replacement(con, "signals", "analysis_signals")
    try:
        con.execute(_RESULT_DDL)
        con.execute(_sql("prelude"))
        con.execute(
            "CREATE TEMP TABLE signal_turns (session_id VARCHAR, seq BIGINT, "
            "event_id VARCHAR, role VARCHAR, has_text BOOLEAN, phrases VARCHAR[])"
        )
        con.execute(
            "CREATE TEMP TABLE signal_compactions "
            "(event_id VARCHAR, session_id VARCHAR, seq BIGINT)"
        )
        event_batches = analysis.event_batches(con, config.ingest.batch_rows)
        while True:
            with analysis.event_transaction(con, event_batches) as span:
                if span is None:
                    break
                start, end = span
                _event_flags(con, start, end)
                _execute(
                    con,
                    analysis.event_scoped(
                        con,
                        "INSERT INTO signal_turns " + _sql("turn_inputs"),
                        start,
                        end,
                        event_input="signal_event_flags",
                    ),
                    values,
                )
                con.execute(
                    analysis.event_scoped(
                        con,
                        "INSERT INTO signal_compactions " + _sql("compaction_inputs"),
                        start,
                        end,
                        event_input="signal_event_flags",
                    )
                )
        with analysis.session_batches(con, config.ingest.batch_rows) as batches:
            for ids in batches:
                with analysis.scope(con, ids):
                    con.execute("DELETE FROM signal_result_flags")
                    _result_flags(con)
                    con.execute("DROP VIEW IF EXISTS signal_calls")
                    con.execute(analysis.scoped(_sql("prelude").replace("TEMP VIEW", "TEMP TABLE")))
                    try:
                        con.begin()
                        try:
                            for kind in KINDS:
                                if kind in SESSION_KINDS:
                                    select = _sql(kind)
                                    statement = _INSERT.replace(
                                        "INSERT INTO signals", "INSERT INTO analysis_signals"
                                    )
                                    _execute(
                                        con,
                                        analysis.scoped(statement.format(select=select)),
                                        {**values, "kind": kind},
                                    )
                            con.commit()
                        except BaseException:
                            con.rollback()
                            raise
                    finally:
                        con.execute("DROP TABLE signal_calls")
        con.execute(_sql("prelude"))
        for kind in KINDS:
            if kind in GLOBAL_KINDS:
                _execute(
                    con,
                    _INSERT.replace("INSERT INTO signals", "INSERT INTO analysis_signals").format(
                        select=_sql(kind)
                    ),
                    {**values, "kind": kind},
                )
        # The pickle arm aggregates narrow event ids/file metadata, no JSON/bodies.
        _execute(
            con,
            _INSERT.replace("INSERT INTO signals", "INSERT INTO analysis_signals").format(
                select=_sql("stale_pickle")
            ),
            {**values, "kind": "orphaned_session"},
        )
        con.begin()
        try:
            con.execute("DROP TABLE signals")
            con.execute("ALTER TABLE analysis_signals RENAME TO signals")
            con.execute(_sql("rollups"))
            con.commit()
        except BaseException:
            con.rollback()
            raise
    finally:
        con.execute("DROP TABLE IF EXISTS analysis_signals")
        con.execute(
            "DROP TABLE IF EXISTS signal_turns; DROP TABLE IF EXISTS signal_compactions; "
            "DROP TABLE IF EXISTS signal_event_flags; DROP TABLE IF EXISTS signal_result_flags"
        )
    counts = dict(con.execute("SELECT kind, count(*) FROM signals GROUP BY kind").fetchall())
    by_kind = {kind: int(counts.get(kind, 0)) for kind in KINDS}
    return {"signals_written": sum(by_kind.values()), "by_kind": by_kind}


def report(
    con: duckdb.DuckDBPyConnection, filters: Filters, until: datetime, *, top: int | None = None
) -> SignalsReport:
    """The stored signals and the rollups, both narrowed by ``filters``."""
    values: dict[str, object] = {
        "kinds": list(filters.kinds),
        "project": filters.project,
        "lane": filters.lane,
        "since": _start_of(filters.since),
    }
    statement = _REPORT
    if top is not None:
        # Rank filtered narrow identities before fetching evidence/details.
        statement += (
            " AND s.severity > 0 AND s.signal_id IN (SELECT s.signal_id "
            + _FILTERS
            + (" ORDER BY s.severity DESC, s.signal_id LIMIT $top) ")
        )
        values["top"] = top
    statement += _REPORT_ORDER
    signals = list(_signal_rows(con, statement, values))
    return {
        "window": {
            "since": None if filters.since is None else filters.since.isoformat(),
            "until": until.isoformat(),
        },
        "signals": signals,
        "rollups": {dim: _rollup(con, dim, values) for dim in DIMENSIONS},
    }


def _signal_rows(
    con: duckdb.DuckDBPyConnection, statement: str, values: Mapping[str, object]
) -> Iterator[Signal]:
    # Sort/page only identities and scalar metadata. Fetch each wide output row
    # by physical position, so full signals output never sorts or pins all bodies.
    prefix = statement.removesuffix(_REPORT_ORDER).replace(
        _SIGNAL_COLUMNS,
        "SELECT s.rowid, s.signal_id, s.kind, s.session_id, j.harness, j.project, s.severity ",
    )
    last: tuple[str, str, str] | None = None
    while True:
        page_values = dict(values) | {"page_size": BATCH_ROWS}
        after = ""
        if last is not None:
            after = (
                " AND (s.kind, s.session_id, s.signal_id) > ($last_kind, $last_session, $last_id) "
            )
            page_values.update(zip(("last_kind", "last_session", "last_id"), last, strict=True))
        page = _execute(
            con, prefix + after + _REPORT_ORDER + " LIMIT $page_size", page_values
        ).fetchall()
        if not page:
            return
        for position, signal_id, kind, session_id, harness, project, severity in page:
            evidence, details = con.execute(
                "SELECT evidence_event_ids, details::VARCHAR FROM signals WHERE rowid = ?",
                [position],
            ).fetchall()[0]
            yield {
                "signal_id": signal_id,
                "kind": kind,
                "session_id": session_id,
                "harness": harness,
                "project": project,
                "severity": float(severity),
                "evidence_event_ids": list(evidence),
                "details": json.loads(details) if details is not None else {},
            }
            last = kind, session_id, signal_id


def write_report(
    con: duckdb.DuckDBPyConnection, filters: Filters, until: datetime, output: TextIO
) -> None:
    """Stream the full signals document without retaining history-sized bodies."""
    values: dict[str, object] = {
        "kinds": list(filters.kinds),
        "project": filters.project,
        "lane": filters.lane,
        "since": _start_of(filters.since),
    }
    window = {
        "since": None if filters.since is None else filters.since.isoformat(),
        "until": until.isoformat(),
    }
    rollups = {dim: _rollup(con, dim, values) for dim in DIMENSIONS}
    output.write('{"window":' + json.dumps(window) + ',"signals":[')
    separator = ""
    for signal in _signal_rows(con, _REPORT + _REPORT_ORDER, values):
        output.write(separator + json.dumps(signal))
        separator = ","
    output.write('],"rollups":' + json.dumps(rollups) + "}\n")


def _rollup(
    con: duckdb.DuckDBPyConnection, dim: RollupDimension, values: Mapping[str, object]
) -> list[dict[str, object]]:
    cursor = _execute(con, _ROLLUP, {**values, "dim": dim})
    columns = [column[0] for column in cursor.description or ()]
    rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    for row in rows:
        row["signal_counts"] = json.loads(str(row["signal_counts"]))
    return rows
