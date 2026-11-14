"""Ingest state and database summary, and the ``command_runs`` log ``status`` reads."""

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import duckdb

from slopsniffer import pricing
from slopsniffer.config import Config
from slopsniffer.contracts import Harness, HarnessStatus, Status

# All status scans project scalar identities only. Their spillable aggregates
# retain one row per harness/status, record type or command; no event bodies.
_FILES = "SELECT harness, status, count(*) FROM source_files GROUP BY ALL"
_UNKNOWN = """
SELECT f.harness, e.record_type, count(*)
FROM events e JOIN source_files f ON f.source_file_id = e.source_file_id
WHERE e.kind = 'unknown'
GROUP BY ALL
"""
_LAST_RUNS = """
SELECT command, strftime(timezone('UTC', max(finished_at)), '%Y-%m-%dT%H:%M:%S.%fZ')
FROM command_runs WHERE finished_at IS NOT NULL GROUP BY command
"""


def record_run(
    con: duckdb.DuckDBPyConnection,
    command: str,
    started_at: datetime,
    finished_at: datetime,
    exit_code: int,
) -> None:
    con.execute(
        "INSERT INTO command_runs (command, started_at, finished_at, exit_code) "
        "VALUES (?, ?, ?, ?)",
        [command, started_at, finished_at, exit_code],
    )


def collect(
    con: duckdb.DuckDBPyConnection,
    config: Config,
    harnesses: Iterable[Harness],
    commands: Iterable[str],
) -> Status:
    pricing.install(con, config)
    by_harness: dict[Harness, HarnessStatus] = {
        h: (
            {"unknown_record_types": {}}
            if h == "skills"
            else {"files_by_status": {}, "unknown_record_types": {}}
        )
        for h in harnesses
    }
    for harness, state, n in con.execute(_FILES).fetchall():
        if harness in by_harness and harness != "skills":
            by_harness[harness]["files_by_status"][state] = int(n)
    for harness, record_type, n in con.execute(_UNKNOWN).fetchall():
        if harness in by_harness:
            by_harness[harness]["unknown_record_types"][record_type] = int(n)
    finished = dict(con.execute(_LAST_RUNS).fetchall())
    version = con.execute("SELECT max(version) FROM schema_version").fetchone()
    return {
        "session_counts": session_counts(con),
        "pricing_coverage": pricing.coverage(con),
        "unpriced_models": pricing.unpriced(con),
        "db_path": str(config.db_path),
        "db_size_bytes": _size(config.db_path) + _size(Path(f"{config.db_path}.wal")),
        "schema_version": int(version[0]) if version and version[0] is not None else 0,
        "harnesses": by_harness,
        "last_runs": {command: finished.get(command) for command in commands},
    }


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def session_counts(con: duckdb.DuckDBPyConnection) -> list[dict[str, object]]:
    """Attribute descendants to their root lane, exactly as the lane rollup does."""
    rows = con.execute("""
WITH RECURSIVE tree AS (
    SELECT session_id AS root, session_id, harness AS lane FROM sessions
    WHERE parent_session_id IS NULL OR parent_session_id NOT IN (SELECT session_id FROM sessions)
    UNION ALL
    SELECT t.root, s.session_id, t.lane FROM tree t
    JOIN sessions s ON s.parent_session_id=t.session_id
)
SELECT lane, count(DISTINCT root), count(*) FROM tree GROUP BY lane ORDER BY lane
""").fetchall()
    return [
        {"lane": lane, "top_level_sessions": roots, "sessions_including_subagents": total}
        for lane, roots, total in rows
    ]
