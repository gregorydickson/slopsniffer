"""Pickle-rick session parser: ``<root>/<run>/`` for each configured pickle sessions root.

A run directory is one that holds ``state.json``. Every ``*.log`` in it and in its ticket
subdirectories (``tmux_iteration_N.log``, ``*runner.log``, ``hooks.log``,
``<ticket>/worker_session_<pid>.log``) goes through the engine line by line. Those logs mix
stream-json NDJSON with plain text: a JSON object line keeps its text as ``parsed``; any other
line, undecodable bytes included, is a ``text`` event with no ``parsed``, not a
malformed one.

``state.json`` is multi-line JSON, so :func:`finalize` reads it directly and rebuilds every
pickle row: ``pickle_runs`` (one per run directory, first root wins on a repeated name),
``pickle_iterations`` (one per ``tmux_iteration_N.log``, keyed by N) and
``pickle_session_links`` (a ``session_id``/``sessionId``/``thread_id`` named in a log line
that is a known ``sessions`` row).
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

import duckdb

from slopsniffer.config import Config
from slopsniffer.contracts import Harness
from slopsniffer.ingest import common, engine
from slopsniffer.ingest.reader import Event, LineContext, ParsedLine

HARNESS: Harness = "pickle"
STATE = "state.json"
TEXT = "text"
ITERATION_LOG = re.compile(r"tmux_iteration_(\d+)\.log")


def kind(path: str) -> str:
    name = PurePath(path).name
    if ITERATION_LOG.fullmatch(name):
        return "iteration"
    if name.endswith("runner.log"):
        return "runner"
    if name == "hooks.log":
        return "hooks"
    if name.startswith("worker_session_"):
        return "worker"
    return "log"


@dataclass(frozen=True)
class PickleParser:
    def parse_line(self, line: bytes, ctx: LineContext) -> ParsedLine:
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        if not isinstance(value, dict):
            return ParsedLine(Event(TEXT, kind(ctx.source_path)))
        record_type = common.as_str(value.get("type")) or "json"
        ts = common.timestamp(value.get("timestamp"))
        return ParsedLine(Event(record_type, kind(ctx.source_path), ts=ts, parsed=text))


def run_dirs(roots: Sequence[Path]) -> dict[str, Path]:
    """Run directories by name; the first root holding a name wins."""
    runs: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if (child / STATE).is_file():
                runs.setdefault(child.name, child.resolve())
    return runs


def _logs(run: Path) -> list[Path]:
    return sorted([*run.glob("*.log"), *run.glob("*/*.log")])


def _state(run: Path) -> dict[str, Any]:
    try:
        value = json.loads((run / STATE).read_text("utf-8"))
    except OSError, ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _run_row(run_id: str, run: Path) -> tuple[object, ...]:
    state = _state(run)
    active = state.get("active")
    return (
        run_id,
        str(run),
        active if isinstance(active, bool) else None,
        common.as_str(state.get("step")),
        common.as_int(state.get("iteration")),
        common.as_int(state.get("max_iterations")),
        common.as_str(state.get("current_ticket")),
        common.as_int(state.get("start_time_epoch")),
        common.as_str(state.get("working_dir")),
    )


_INSERT_RUNS = """
INSERT INTO pickle_runs (pickle_run_id, session_dir, active, step, iteration, max_iterations,
                         current_ticket, start_time_epoch, working_dir)
SELECT unnest($1::VARCHAR[]), unnest($2::VARCHAR[]), unnest($3::BOOLEAN[]),
       unnest($4::VARCHAR[]), unnest($5::INTEGER[]), unnest($6::INTEGER[]),
       unnest($7::VARCHAR[]), unnest($8::BIGINT[]), unnest($9::VARCHAR[])
"""

_FILES = """
CREATE OR REPLACE TEMP TABLE pickle_files AS
WITH logs AS (
    SELECT unnest($1::VARCHAR[]) AS path, unnest($2::VARCHAR[]) AS pickle_run_id,
           unnest($3::INTEGER[]) AS iteration
)
SELECT f.source_file_id, f.mtime_ns, l.pickle_run_id, l.iteration
FROM logs l JOIN source_files f USING (path)
WHERE f.harness = 'pickle'
"""

_LAST_WRITE = """
UPDATE pickle_runs SET last_log_write_at = latest.at FROM (
    SELECT pickle_run_id, make_timestamptz(max(mtime_ns) // 1000) AS at
    FROM pickle_files GROUP BY pickle_run_id
) AS latest
WHERE pickle_runs.pickle_run_id = latest.pickle_run_id
"""

_ITERATIONS = """
INSERT INTO pickle_iterations (pickle_run_id, iteration, source_file_id, started_at, ended_at)
SELECT p.pickle_run_id, p.iteration, p.source_file_id, min(e.ts), max(e.ts)
FROM pickle_files p LEFT JOIN events e USING (source_file_id)
WHERE p.iteration IS NOT NULL
GROUP BY p.pickle_run_id, p.iteration, p.source_file_id
"""

_LINKS = """
INSERT INTO pickle_session_links (pickle_run_id, iteration, session_id, source_event_id)
WITH named AS (
    SELECT p.pickle_run_id, p.iteration, e.event_id, e.source_file_id, e.byte_offset,
           coalesce(json_extract_string(e.parsed, '$.session_id'),
                    json_extract_string(e.parsed, '$.sessionId'),
                    json_extract_string(e.parsed, '$.thread_id')) AS session_id
    FROM events e JOIN pickle_files p USING (source_file_id)
    WHERE e.parsed IS NOT NULL
)
SELECT pickle_run_id, iteration, session_id, arg_min(event_id, (source_file_id, byte_offset))
FROM named
WHERE session_id IN (SELECT session_id FROM sessions)
GROUP BY pickle_run_id, iteration, session_id
"""


def _iteration(log: Path, run: Path) -> int | None:
    match = ITERATION_LOG.fullmatch(log.name)
    return int(match.group(1)) if match and log.parent == run else None


def source(roots: Sequence[Path] | None = None) -> engine.HarnessSource:
    """Run-directory logs under ``roots``, or under the configured pickle sessions roots."""

    def runs(config: Config) -> dict[str, Path]:
        return run_dirs(roots if roots is not None else config.ingest.sources.pickle_sessions)

    def discover(config: Config) -> list[Path]:
        return [log for run in runs(config).values() for log in _logs(run)]

    def finalize(con: duckdb.DuckDBPyConnection, config: Config) -> None:
        """Rebuild every pickle row from the run directories and the ingested log events."""
        found = runs(config)
        # Remove vanished runs, then rebuild each surviving run independently. A crash
        # leaves complete runs or their previous committed versions; the next pass rebuilds all.
        with engine.transaction(con):
            for table in ("pickle_session_links", "pickle_iterations", "pickle_runs"):
                con.execute(
                    f"DELETE FROM {table} WHERE pickle_run_id NOT IN (SELECT unnest(?::VARCHAR[]))",
                    [list(found)],
                )
        for run_id, run in found.items():
            with engine.transaction(con):
                for table in ("pickle_session_links", "pickle_iterations", "pickle_runs"):
                    con.execute(f"DELETE FROM {table} WHERE pickle_run_id = ?", [run_id])
                con.execute(_INSERT_RUNS, [[value] for value in _run_row(run_id, run)])
                logs = [(str(log.resolve()), run_id, _iteration(log, run)) for log in _logs(run)]
                con.execute(
                    _FILES, [list(column) for column in zip(*logs, strict=True)] or [[], [], []]
                )
                con.execute(_LAST_WRITE)
                con.execute(_ITERATIONS)
                con.execute(_LINKS)
                con.execute("DROP TABLE pickle_files")

    return engine.HarnessSource(PickleParser(), discover, finalize)
