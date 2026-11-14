"""Droid session parser: ``~/.factory/sessions/<project-dir>/<session-id>.jsonl``.

The format is documented in ``docs/formats/droid.md``. Each line's record type is its
``type``; types outside :data:`KINDS` keep kind ``unknown`` and no rows. ``message`` lines
yield ``messages`` rows. :func:`finalize` derives, per pending session: ``sessions`` (parent
from ``session_start.callingSessionId``, else the fork's ``parent``), ``tool_calls``
(``tool_use`` ↔ ``tool_result`` blocks by id), and ``usage`` plus price-table cost from the
``<session-id>.settings.json`` sidecar, whose ``tokenUsage`` is the session's own total.

The sidecar has no per-message usage, so the session's usage row hangs off its
``session_start`` event. It is re-read whenever the session has new events; a missing or
unreadable sidecar leaves the session without usage and cost.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

import duckdb

from slopsniffer.config import Config
from slopsniffer.contracts import Harness
from slopsniffer.ingest import common, engine
from slopsniffer.ingest.reader import Event, LineContext, ParsedLine, Row

HARNESS: Harness = "droid"
UNKNOWN = "unknown"
SIDECAR_SUFFIX = ".settings.json"

KINDS = {
    "session_start": "meta",
    "message": "message",
    "todo_state": "todo",
    "agent_turn_outcome": "turn",
    "compaction_state": "compaction",
    "session_end": "meta",
}


@dataclass(frozen=True)
class DroidParser:
    def parse_line(self, line: bytes, ctx: LineContext) -> ParsedLine:
        text = line.decode("utf-8").rstrip("\r\n")
        value = json.loads(text)
        record: dict[str, Any] = value if isinstance(value, dict) else {}
        record_type = record.get("type")
        if not isinstance(record_type, str):
            record_type = UNKNOWN
        event = Event(
            record_type,
            KINDS.get(record_type, UNKNOWN),
            session_id=PurePath(ctx.source_path).stem,
            ts=common.timestamp(record.get("timestamp")),
            parsed=text,
        )
        message = record.get("message")
        if record_type != "message" or not isinstance(message, dict):
            return ParsedLine(event)
        texts = common.block_texts(message.get("content"), ("text",))
        role = common.as_str(message.get("role")) or "unknown"
        row = Row(
            "messages", {"event_id": ctx.event_id, "role": role, "text": "\n".join(texts) or None}
        )
        return ParsedLine(event, [row])


_SESSIONS = """
INSERT INTO sessions (session_id, harness, parent_session_id, project, cwd, git_branch, model,
                      started_at, ended_at, source_file_id)
WITH spans AS (
    SELECT session_id, min(ts) AS started_at, max(ts) AS ended_at,
           arg_min(source_file_id, seq) AS source_file_id,
           arg_min(meta, seq) FILTER (WHERE record_type = 'session_start') AS start,
           arg_min(meta[4], seq)
               FILTER (WHERE meta[4] <> '') AS model
    FROM (SELECT e.*, f.meta FROM lane_pending_events e
          LEFT JOIN droid_fields f USING (event_id)) GROUP BY session_id
), fields AS (
    SELECT *, start[1] AS cwd, coalesce(start[2], start[3]) AS parent
    FROM spans
)
SELECT session_id, 'droid', parent, NULLIF(regexp_extract(cwd, '([^/]+)/*$', 1), ''), cwd,
       NULL, model, started_at, ended_at, source_file_id
FROM fields
"""


def _fields(record: dict[str, Any], kind: str, location: dict[str, Any]) -> Iterator[Row]:
    message = record.get("message")
    message = message if isinstance(message, dict) else {}
    yield Row(
        "droid_fields",
        {
            "event_id": location["event_id"],
            "meta": [
                common.json_string(value)
                for value in (
                    record.get("cwd"),
                    record.get("callingSessionId"),
                    record.get("parent"),
                    message.get("modelId"),
                )
            ],
        },
    )
    content = message.get("content")
    if kind != "message" or not isinstance(content, list):
        return
    loc = {key: location[key] for key in ("event_id", "session_id", "seq")}
    for block in content:
        if not isinstance(block, dict):
            continue
        if message.get("role") == "assistant" and block.get("type") == "tool_use":
            call, name = common.json_string(block.get("id")), common.json_string(block.get("name"))
            if call is not None and name is not None:
                yield Row(
                    "lane_uses",
                    {
                        **loc,
                        "call_id": call,
                        "tool_name": name,
                        "input": json.dumps(
                            block["input"], ensure_ascii=False, separators=(",", ":")
                        )
                        if "input" in block
                        else None,
                    },
                )
        elif message.get("role") == "user" and block.get("type") == "tool_result":
            call = common.json_string(block.get("tool_use_id"))
            if call is not None:
                yield Row(
                    "lane_results",
                    {
                        **loc,
                        "call_id": call,
                        "result_text": common.result_text(block.get("content"), ("text",)),
                        "is_error": common.json_string(block.get("is_error")),
                    },
                )


_STARTS = """
SELECT e.event_id, f.path FROM lane_pending_events e JOIN source_files f USING (source_file_id)
WHERE e.record_type = 'session_start'
QUALIFY row_number() OVER (PARTITION BY e.session_id ORDER BY e.seq) = 1
"""

_INSERT_USAGE = """
INSERT INTO usage (event_id, input_tokens, output_tokens, cache_read, cache_write, model)
SELECT unnest($1::VARCHAR[]), unnest($2::BIGINT[]), unnest($3::BIGINT[]),
       unnest($4::BIGINT[]), unnest($5::BIGINT[]), unnest($6::VARCHAR[])
"""


def sidecar(source: Path) -> Path:
    return source.with_name(source.stem + SIDECAR_SUFFIX)


def _sidecar_usage(event_id: str, source: Path) -> tuple[object, ...] | None:
    try:
        settings = json.loads(sidecar(source).read_text("utf-8"))
    except OSError, ValueError:
        return None
    tokens = settings.get("tokenUsage") if isinstance(settings, dict) else None
    if not isinstance(tokens, dict):
        return None
    return (
        event_id,
        common.as_int(tokens.get("inputTokens")),
        common.as_int(tokens.get("outputTokens")),
        common.as_int(tokens.get("cacheReadTokens")),
        common.as_int(tokens.get("cacheCreationTokens")),
        common.as_str(settings.get("model")),
    )


def _insert_usage(con: duckdb.DuckDBPyConnection) -> None:
    starts = con.execute(_STARTS).fetchall()
    con.execute(
        "DELETE FROM usage WHERE event_id IN (SELECT unnest($1::VARCHAR[]))",
        [[eid for eid, _ in starts]],
    )
    rows = [u for eid, path in starts if (u := _sidecar_usage(eid, Path(path))) is not None]
    if rows:
        con.execute(_INSERT_USAGE, [list(column) for column in zip(*rows, strict=True)])


def finalize(con: duckdb.DuckDBPyConnection, config: Config) -> None:
    """Derive sessions, tool calls, sidecar usage and price-table cost of every pending Droid
    session; publishes completion after bounded body batches."""
    common.pending(con, HARNESS, lambda: _derive(con, config))


def _derive(con: duckdb.DuckDBPyConnection, config: Config) -> None:
    with common.tool_tables(con, config) as bodies:
        con.execute("CREATE TEMP TABLE droid_fields (event_id VARCHAR, meta VARCHAR[])")
        common.json_batches(con, config, _fields, bodies)
        common.insert_tools(con, config, bodies, error_default=False)
        _insert_usage(con)
        con.execute(_SESSIONS)
        common.insert_costs(con, config)
        con.execute("DROP TABLE droid_fields")


def source(root: Path | None = None) -> engine.HarnessSource:
    """Every ``*.jsonl`` under ``root``, or under the configured Droid sessions root."""

    def discover(config: Config) -> list[Path]:
        base = root if root is not None else config.ingest.sources.droid_sessions
        return sorted(base.rglob("*.jsonl")) if base.is_dir() else []

    return engine.HarnessSource(DroidParser(), discover, finalize)
