"""Claude Code transcript parser: ``~/.claude/projects/**/*.jsonl``, subagent files included.

Each line yields its ``events`` row plus the rows that line alone determines: ``messages``,
``usage``, ``skill_invocations``, ``context_blocks`` and their content-addressed ``blobs``.
Rows that span lines or files (``sessions``, event ``seq``, ``tool_calls``,
``session_cost``) are derived by :func:`finalize` after every file has been read, for the
sessions that have unsequenced events — so a run killed before finalizing is finished by
the next one. No text is shortened anywhere.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

import duckdb

from slopsniffer.config import Config
from slopsniffer.contracts import Harness
from slopsniffer.ingest import common, engine
from slopsniffer.ingest.common import as_int, as_str, block_texts, timestamp
from slopsniffer.ingest.reader import Event, LineContext, ParsedLine, Row

HARNESS: Harness = "claude"
UNKNOWN = "unknown"

KINDS = {
    "user": "message",
    "assistant": "message",
    "attachment": "context",
    "system": "system",
    "api-request": "request",
    "api-request-blob": "request",
    "api-request-shape": "request",
    "cost-state": "cost",
    "last-prompt": "meta",
    "queue-operation": "meta",
    "tools-used": "meta",
    "atis-latch": "meta",
    "ai-title": "meta",
    "custom-title": "meta",
    "pr-link": "meta",
    "file-history-snapshot": "file_history",
    "file-history-delta": "file_history",
    "worktree-state": "context",
    "relocated": "context",
    "mode": "context",
    "permission-mode": "context",
    "fork-context-ref": "subagent",
    "started": "subagent",
    "result": "subagent",
    "failed": "error",
    "launched": "subagent",
}

_WORKFLOW_FILE = re.compile(r"/([^/]+)/subagents/workflows/[^/]+/journal\.jsonl$")
_SUBAGENT_FILE = re.compile(r"/subagents/agent-([^/]+)\.jsonl$")
_REMINDER = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.S)
_CONTENTS_HEADER = re.compile(r"^Contents of (\S[^\n]*?) \([^\n]*\):[ \t]*$", re.M)
_SKILL_LISTING = "The following skills are available for use with the Skill tool"
_HOOK_OUTPUT = re.compile(r"\s*[A-Za-z]+(?::\S+)? hook\b")
_COMMAND_MARKER = re.compile(r"\s*<command-(?:name|message)>")
_COMMAND_NAME = re.compile(r"<command-name>\s*/?(.*?)\s*</command-name>", re.S)
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


@dataclass(frozen=True)
class ClaudeParser:
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
            session_id=_session_id(record, ctx.source_path),
            ts=timestamp(record.get("timestamp")),
            parsed=text,
        )
        rows: list[Row] = []
        if event.session_id is not None and event.kind != UNKNOWN:
            rows = list(_rows(record, record_type, event.session_id, ctx.event_id))
        return ParsedLine(event, rows)


def _session_id(record: dict[str, Any], source_path: str) -> str | None:
    """A subagent file is its own session (its lines name the parent's ``sessionId``)."""
    if workflow := _WORKFLOW_FILE.search(source_path):
        return workflow.group(1)
    if match := _SUBAGENT_FILE.search(source_path):
        return match.group(1)
    for key in ("sessionId", "session_id"):
        if isinstance(session := record.get(key), str) and session:
            return session
    return PurePath(source_path).stem or None


def _rows(record: dict[str, Any], record_type: str, session: str, eid: str) -> Iterator[Row]:
    message = record.get("message")
    message = message if isinstance(message, dict) else {}
    if record_type in ("user", "assistant"):
        role = message.get("role")
        texts = _texts(message.get("content"))
        yield Row(
            "messages",
            {
                "event_id": eid,
                "role": role if isinstance(role, str) else record_type,
                "text": "\n".join(texts) if texts else None,
            },
        )
        if record_type == "user":
            yield from _slash_commands(texts, session, eid)
            yield from _context_rows(eid, _reminder_blocks(texts))
        else:
            yield from _usage(message, eid)
            yield from _skill_calls(message.get("content"), session, eid)
    elif record_type == "api-request-blob":
        yield from _context_rows(eid, _reminder_blocks(_texts(message.get("content"))))
    elif record_type == "attachment":
        attachment = record.get("attachment")
        if isinstance(attachment, dict):
            yield from _context_rows(eid, _attachment_blocks(attachment))


def _texts(content: object) -> list[str]:
    """The message's own text: a string body, or its ``text`` blocks in order."""
    return block_texts(content, ("text",))


def _usage(message: dict[str, Any], eid: str) -> Iterator[Row]:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    yield Row(
        "usage",
        {
            "event_id": eid,
            "input_tokens": as_int(usage.get("input_tokens")),
            "output_tokens": as_int(usage.get("output_tokens")),
            "cache_read": as_int(usage.get("cache_read_input_tokens")),
            "cache_write": as_int(usage.get("cache_creation_input_tokens")),
            "model": as_str(message.get("model")),
        },
    )


def _skill_calls(content: object, session: str, eid: str) -> Iterator[Row]:
    seen: set[str] = set()
    for block in content if isinstance(content, list) else ():
        if not (isinstance(block, dict) and block.get("type") == "tool_use"):
            continue
        tool_input = block.get("input")
        if block.get("name") != "Skill" or not isinstance(tool_input, dict):
            continue
        name = tool_input.get("skill")
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)
        args = tool_input.get("args")
        yield _skill_row(eid, session, name, "skill_tool", _text_or_json(args))


def _slash_commands(texts: list[str], session: str, eid: str) -> Iterator[Row]:
    """A slash command is a user text that *opens* with the command markup; the same tags
    quoted inside an ordinary prompt are not an invocation."""
    seen: set[str] = set()
    for text in texts:
        if not _COMMAND_MARKER.match(text) or not (name := _COMMAND_NAME.search(text)):
            continue
        skill = name.group(1)
        if not skill or skill in seen:
            continue
        seen.add(skill)
        args = _COMMAND_ARGS.search(text)
        yield _skill_row(eid, session, skill, "slash_command", args.group(1) if args else None)


def _skill_row(eid: str, session: str, name: str, via: str, args: str | None) -> Row:
    values = {"event_id": eid, "session_id": session, "skill_name": name, "via": via}
    return Row("skill_invocations", {**values, "args": args})


def _text_or_json(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value)


# A context block: (kind, source_path, content).
type Block = tuple[str, str | None, str]


def _reminder_blocks(texts: Iterable[str]) -> Iterator[Block]:
    for text in texts:
        for match in _REMINDER.finditer(text):
            yield from _split_reminder(match.group(1))


def _split_reminder(body: str) -> Iterator[Block]:
    """One block per ``Contents of <path> (...):`` section, plus any text before the first;
    the blocks concatenate back to ``body``."""
    headers = list(_CONTENTS_HEADER.finditer(body))
    if not headers:
        yield _classify(body), None, body
        return
    if body[: headers[0].start()].strip():
        yield _classify(body[: headers[0].start()]), None, body[: headers[0].start()]
    for header, following in zip(headers, [*headers[1:], None], strict=True):
        section = body[header.start() : following.start() if following else len(body)]
        path = header.group(1)
        yield _file_kind(path), path, section


def _classify(text: str) -> str:
    if _SKILL_LISTING in text:
        return "skill_listing"
    if _HOOK_OUTPUT.match(text):
        return "hook_output"
    return "other"


def _file_kind(path: str | None) -> str:
    return "memory_index" if path and PurePath(path).name == "MEMORY.md" else "claude_md"


def _attachment_blocks(attachment: dict[str, Any]) -> Iterator[Block]:
    kind = attachment.get("type")
    if kind == "skill_listing" and isinstance(content := attachment.get("content"), str):
        yield "skill_listing", None, content
    elif kind == "instructions":
        for file in attachment.get("files") or ():
            if isinstance(file, dict) and isinstance(content := file.get("content"), str):
                path = as_str(file.get("path"))
                yield _file_kind(path), path, content
    elif kind == "nested_memory":
        inner = attachment.get("content")
        if isinstance(inner, dict) and isinstance(content := inner.get("content"), str):
            path = as_str(attachment.get("path"))
            yield _file_kind(path), path, content
    elif isinstance(kind, str) and kind.startswith("hook_"):
        parts = [attachment.get(k) for k in ("content", "stdout", "stderr")]
        output = "\n".join(p for p in parts if isinstance(p, str) and p)
        if output:
            yield "hook_output", None, output


def _context_rows(eid: str, blocks: Iterator[Block]) -> Iterator[Row]:
    for ordinal, (kind, path, content) in enumerate(blocks):
        digest = hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()
        yield Row("blobs", {"content_hash": digest, "content": content})
        yield Row(
            "context_blocks",
            {
                "event_id": eid,
                "ordinal": ordinal,
                "kind": kind,
                "source_path": path,
                "content_hash": digest,
            },
        )


# Records without a timestamp take their neighbour's, so they stay where the file put them.
_SEQ = """
UPDATE lane_pending_events SET seq = ordered.seq FROM (
    SELECT event_id, row_number() OVER (
        PARTITION BY session_id ORDER BY eff_ts NULLS FIRST, source_file_id, byte_offset
    ) AS seq
    FROM (
        SELECT event_id, session_id, source_file_id, byte_offset,
               coalesce(last_value(ts IGNORE NULLS) OVER before,
                        first_value(ts IGNORE NULLS) OVER after) AS eff_ts
        FROM lane_pending_events
        WINDOW before AS (PARTITION BY session_id, source_file_id ORDER BY byte_offset
                          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
               after AS (PARTITION BY session_id, source_file_id ORDER BY byte_offset
                         ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING)
    )
) AS ordered
WHERE lane_pending_events.event_id = ordered.event_id
"""

_SESSIONS = """
INSERT INTO sessions (session_id, harness, parent_session_id, project, cwd, git_branch, model,
                      started_at, ended_at, source_file_id)
WITH fields AS (
    SELECT e.session_id, e.seq, e.ts, e.source_file_id, e.record_type,
           coalesce(NULLIF(regexp_extract(f.path, '/([^/]+)/subagents/[^/]+$', 1), ''),
                    m.meta[5]) AS parent,
           m.meta[1] AS cwd,
           m.meta[2] AS git_branch,
           CASE WHEN e.record_type = 'assistant'
                THEN NULLIF(m.meta[3], '<synthetic>')
           END AS model
    FROM lane_pending_events e LEFT JOIN claude_metadata m USING (event_id)
    JOIN source_files f USING (source_file_id)
), per_session AS (
    SELECT session_id, max(parent) AS parent,
           coalesce(arg_max(cwd, seq) FILTER (WHERE record_type IN ('worktree-state','relocated')),
                    arg_min(cwd, seq) FILTER (WHERE cwd IS NOT NULL)) AS cwd,
           coalesce(arg_max(git_branch, seq) FILTER (WHERE record_type = 'worktree-state'),
                    arg_min(git_branch, seq) FILTER (WHERE git_branch IS NOT NULL)) AS git_branch,
           arg_min(model, seq) FILTER (WHERE model <> '') AS model,
           min(ts) AS started_at, max(ts) AS ended_at,
           arg_min(source_file_id, seq) AS source_file_id
    FROM fields GROUP BY session_id
)
SELECT session_id, 'claude', parent, NULLIF(regexp_extract(cwd, '([^/]+)/*$', 1), ''), cwd,
       git_branch, model, started_at, ended_at, source_file_id
FROM per_session
"""

_TOOL_CALLS = """
INSERT INTO tool_calls (call_id, session_id, use_event_id, result_event_id, tool_name, input,
                        result_text, is_error, duration_ms)
WITH uses AS (
    SELECT * FROM claude_uses
    QUALIFY row_number() OVER (PARTITION BY session_id, call_id
                               ORDER BY source_file_id, byte_offset) = 1
), results AS (
    SELECT * FROM claude_results
    QUALIFY row_number() OVER (PARTITION BY session_id, call_id
                               ORDER BY source_file_id, byte_offset) = 1
)
SELECT u.call_id, u.session_id, u.event_id, r.event_id, u.tool_name, u.input,
       r.result_text,
       CASE WHEN r.event_id IS NOT NULL
            THEN coalesce(TRY_CAST(r.is_error AS BOOLEAN), false) END,
       CASE WHEN r.results_in_line = 1 THEN TRY_CAST(r.duration AS BIGINT) END
FROM uses u LEFT JOIN results r USING (session_id, call_id)
"""

# cost-state totals are cumulative, so the session's last one is its cost.
_SESSION_COST = """
INSERT INTO session_cost (session_id, cost_usd, method)
SELECT session_id, arg_max(cost, seq), 'cost_state'
FROM (
    SELECT session_id, seq,
           TRY_CAST(m.meta[4] AS DOUBLE) AS cost
    FROM lane_pending_events e JOIN claude_metadata m USING (event_id)
    WHERE record_type = 'cost-state'
)
WHERE cost IS NOT NULL
GROUP BY session_id
"""


def _json_string(value: object) -> str | None:
    """The scalar/text representation used by DuckDB's json_extract_string."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _metadata(record: dict[str, Any]) -> list[str | None]:
    message = record.get("message")
    model = message.get("model") if isinstance(message, dict) else None
    worktree = record.get("worktreeSession")
    worktree = worktree if isinstance(worktree, dict) else {}
    return [
        _json_string(field)
        for field in (
            worktree.get("worktreePath") or record.get("relocatedCwd") or record.get("cwd"),
            worktree.get("worktreeBranch") or record.get("gitBranch"),
            model,
            record.get("totalCostUSD"),
            record.get("parentSessionId") if record.get("type") == "fork-context-ref" else None,
        )
    ]


def _call_rows(record: dict[str, Any], record_type: str, location: dict[str, Any]) -> Iterator[Row]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if record_type not in ("assistant", "user") or not isinstance(content, list):
        return
    blocks = [b for b in content if isinstance(b, dict)]
    results_in_line = sum(b.get("type") == "tool_result" for b in blocks)
    tool_result = record.get("toolUseResult")
    duration = tool_result.get("durationMs") if isinstance(tool_result, dict) else None
    for block in blocks:
        if record_type == "assistant" and block.get("type") == "tool_use":
            call_id, name = _json_string(block.get("id")), _json_string(block.get("name"))
            if call_id is not None and name is not None:
                yield Row(
                    "claude_uses",
                    {
                        **location,
                        "call_id": call_id,
                        "tool_name": name,
                        "input": json.dumps(
                            block["input"], ensure_ascii=False, separators=(",", ":")
                        )
                        if "input" in block
                        else None,
                    },
                )
        elif record_type == "user" and block.get("type") == "tool_result":
            call_id = _json_string(block.get("tool_use_id"))
            if call_id is None:
                continue
            body = block.get("content")
            text = body if isinstance(body, str) else None
            if isinstance(body, list):
                parts = [
                    part
                    for item in body
                    if isinstance(item, dict)
                    and item.get("type") == "text"
                    and (part := _json_string(item.get("text"))) is not None
                ]
                text = "\n".join(parts) if parts else None
            yield Row(
                "claude_results",
                {
                    **location,
                    "call_id": call_id,
                    "result_text": text,
                    "is_error": _json_string(block.get("is_error")),
                    "duration": _json_string(duration),
                    "results_in_line": results_in_line,
                },
            )


def _session_fields(con: duckdb.DuckDBPyConnection, config: Config) -> None:
    """Bound JSON results by the same row/byte caps as the ingest reader.

    DuckDB's native JSON functions retain parsed trees for an entire vector. Large
    transcript blobs can exhaust the pool even for a small session. Keep only ids
    in the batch map, fetch bounded raw JSON, and free each Python tree immediately.
    """
    con.execute(
        "CREATE TEMP TABLE claude_metadata (event_id VARCHAR, meta VARCHAR[]); "
        "CREATE TEMP TABLE claude_uses (event_id VARCHAR, session_id VARCHAR, "
        "source_file_id BIGINT, byte_offset BIGINT, call_id VARCHAR, "
        "tool_name VARCHAR, input JSON); "
        "CREATE TEMP TABLE claude_results (event_id VARCHAR, session_id VARCHAR, "
        "source_file_id BIGINT, byte_offset BIGINT, call_id VARCHAR, result_text VARCHAR, "
        "is_error VARCHAR, duration VARCHAR, results_in_line BIGINT)"
    )
    common.json_batches(con, config, _fields)


def _fields(record: dict[str, Any], kind: str, location: dict[str, Any]) -> Iterator[Row]:
    if kind in ("fork-context-ref", "started", "result", "failed") and isinstance(
        record.get("agentId"), str
    ):
        parent = record.get("parentSessionId") or location["session_id"]
        if parent is not None and parent != record["agentId"]:
            yield Row(
                "session_relations",
                {
                    "event_id": location["event_id"],
                    "session_id": parent,
                    "child_session_id": record["agentId"],
                    "kind": kind,
                },
            )
    yield Row("claude_metadata", {"event_id": location["event_id"], "meta": _metadata(record)})
    yield from _call_rows(
        record,
        kind,
        {key: value for key, value in location.items() if key not in ("seq", "byte_length")},
    )


def finalize(con: duckdb.DuckDBPyConnection, config: Config) -> None:
    """Derive Claude rows atomically per session; cost comes from its cost-state."""
    _repair_workflow_sessions(con)
    common.reclassify(
        con,
        HARNESS,
        KINDS,
        rederive={"worktree-state", "relocated", "fork-context-ref", "started", "result", "failed"},
    )
    common.pending(con, HARNESS, lambda: _derive(con, config), _SEQ)


def _repair_workflow_sessions(con: duckdb.DuckDBPyConnection) -> None:
    """Workflow journals belong to their enclosing session, never a shared 'journal'."""
    with engine.transaction(con):
        con.execute(
            "CREATE TEMP TABLE claude_workflow_files AS SELECT source_file_id, "
            "regexp_extract(path, '/([^/]+)/subagents/workflows/[^/]+/journal\\.jsonl$', 1) "
            "AS owner FROM source_files WHERE harness='claude' AND "
            "regexp_matches(path, '/[^/]+/subagents/workflows/[^/]+/journal\\.jsonl$')"
        )
        con.execute(
            "CREATE TEMP TABLE claude_old_workflows AS SELECT DISTINCT e.session_id "
            "FROM events e JOIN claude_workflow_files f USING(source_file_id) "
            "WHERE e.session_id IS DISTINCT FROM f.owner"
        )
        con.execute(
            "UPDATE events SET session_id=f.owner, seq=NULL FROM claude_workflow_files f "
            "WHERE events.source_file_id=f.source_file_id "
            "AND events.session_id IS DISTINCT FROM f.owner"
        )
        for table in ("sessions", "session_cost", "tool_calls", "session_relations"):
            con.execute(
                f"DELETE FROM {table} WHERE session_id IN "
                "(SELECT session_id FROM claude_old_workflows) AND session_id NOT IN "
                "(SELECT session_id FROM events WHERE session_id IS NOT NULL)"
            )
        con.execute("DROP TABLE claude_workflow_files; DROP TABLE claude_old_workflows")


def _derive(con: duckdb.DuckDBPyConnection, config: Config) -> None:
    _session_fields(con, config)
    con.execute(_SESSIONS)
    con.execute(_TOOL_CALLS)
    con.execute(_SESSION_COST)
    con.execute("DROP TABLE claude_metadata; DROP TABLE claude_uses; DROP TABLE claude_results")


def source(root: Path | None = None) -> engine.HarnessSource:
    """Every ``*.jsonl`` under ``root``, or under the configured Claude projects root."""

    def discover(config: Config) -> list[Path]:
        base = root if root is not None else config.ingest.sources.claude_projects
        return sorted(base.rglob("*.jsonl")) if base.is_dir() else []

    return engine.HarnessSource(ClaudeParser(), discover, finalize)
