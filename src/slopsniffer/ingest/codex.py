"""Codex rollout parser: ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``.

A rollout line is ``{timestamp, ordinal?, type, payload, metadata?}``. ``response_item`` and
``event_msg`` lines are typed by their payload too, so their record type is
``"<type>:<payload.type>"`` (``response_item:function_call``); every other line's record
type is its ``type``. Record types outside :data:`KINDS` keep kind ``unknown`` and no rows.

Each line yields its ``events`` row plus ``messages`` (``response_item:message``) and
``usage`` (``event_msg:token_count``). :func:`finalize` derives the rest per pending session:

* A subagent rollout opens with its own ``session_meta`` and then replays the parent's
  history; lines whose ``ordinal`` is below that meta's ``subagent_history_start_ordinal``
  are the parent's, so they yield no messages, usage, tool calls or session fields here.
* ``token_count`` repeats with an unchanged cumulative ``total_token_usage`` describe no new
  response, so only the first of such a run keeps its usage row. ``token_usage_record``
  reports the same responses again and is kept only as an event.
* Usage ``input_tokens`` is uncached input (Codex counts cached tokens inside it), so the
  columns mean what Claude's do; its model is the latest preceding ``turn_context`` or
  ``thread_settings_applied`` model.
* ``function_call``/``custom_tool_call`` pair with their ``*_output`` by ``call_id``; the
  outputs carry no error flag, so ``is_error`` is NULL.
"""

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

import duckdb

from slopsniffer import analysis
from slopsniffer.config import Config
from slopsniffer.contracts import Harness
from slopsniffer.ingest import common, engine
from slopsniffer.ingest.progress import tick
from slopsniffer.ingest.reader import Event, LineContext, ParsedLine, Row

HARNESS: Harness = "codex"
UNKNOWN = "unknown"
# Line types whose payload ``type`` names the record.
SUBTYPED = ("response_item", "event_msg")

KINDS = {
    "session_meta": "meta",
    "turn_context": "context",
    "world_state": "context",
    "token_usage_record": "usage_record",
    "compacted": "compaction",
    "inter_agent_communication_metadata": "meta",
    "response_item:message": "message",
    "response_item:agent_message": "message",
    "response_item:reasoning": "reasoning",
    "response_item:function_call": "tool_call",
    "response_item:custom_tool_call": "tool_call",
    "response_item:web_search_call": "tool_call",
    "response_item:function_call_output": "tool_result",
    "response_item:custom_tool_call_output": "tool_result",
    "response_item:additional_tools": "context",
    "response_item:configuration_update": "meta",
    "event_msg:task_started": "turn",
    "event_msg:task_complete": "turn",
    "event_msg:turn_aborted": "turn",
    "event_msg:token_count": "usage",
    "event_msg:item_completed": "item",
    "event_msg:thread_settings_applied": "meta",
    "event_msg:thread_name_updated": "meta",
    "event_msg:thread_goal_updated": "meta",
    "event_msg:agent_message": "event",
    "event_msg:user_message": "event",
    "event_msg:exec_command_end": "event",
    "event_msg:patch_apply_end": "event",
    "event_msg:collab_agent_spawn_end": "event",
    "event_msg:collab_close_end": "event",
    "event_msg:collab_waiting_end": "event",
    "event_msg:error": "error",
    "event_msg:mcp_tool_call_end": "tool_result",
    "event_msg:web_search_end": "tool_result",
    "response_item:tool_search_call": "tool_call",
    "response_item:tool_search_output": "tool_result",
    "event_msg:sub_agent_activity": "subagent",
    "event_msg:context_compacted": "compaction",
    "event_msg:thread_rolled_back": "rollback",
    "event_msg:entered_review_mode": "context",
    "event_msg:exited_review_mode": "context",
    "event_msg:collab_agent_interaction_end": "subagent",
}

_ROLLOUT_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
_MESSAGE_TEXT = ("input_text", "output_text")


@dataclass(frozen=True)
class CodexParser:
    def parse_line(self, line: bytes, ctx: LineContext) -> ParsedLine:
        text = line.decode("utf-8").rstrip("\r\n")
        value = json.loads(text)
        record: dict[str, Any] = value if isinstance(value, dict) else {}
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        record_type = _record_type(record, payload)
        match = _ROLLOUT_ID.search(ctx.source_path)
        session = match.group(1) if match else PurePath(ctx.source_path).stem
        event = Event(
            record_type,
            KINDS.get(record_type, UNKNOWN),
            session_id=session,
            ts=common.timestamp(record.get("timestamp")),
            parsed=text,
        )
        rows = [] if event.kind == UNKNOWN else list(_rows(record_type, payload, ctx.event_id))
        return ParsedLine(event, rows)


def _record_type(record: dict[str, Any], payload: dict[str, Any]) -> str:
    kind = record.get("type")
    if not isinstance(kind, str):
        return UNKNOWN
    if kind in SUBTYPED and isinstance(sub := payload.get("type"), str):
        return f"{kind}:{sub}"
    return kind


def _rows(record_type: str, payload: dict[str, Any], eid: str) -> Iterator[Row]:
    if record_type == "response_item:message":
        texts = common.block_texts(payload.get("content"), _MESSAGE_TEXT)
        role = common.as_str(payload.get("role")) or "unknown"
        yield Row("messages", {"event_id": eid, "role": role, "text": "\n".join(texts) or None})
    elif record_type == "event_msg:token_count":
        info = payload.get("info")
        last = info.get("last_token_usage") if isinstance(info, dict) else None
        if isinstance(last, dict):
            yield Row("usage", _usage(last, eid))


def _usage(last: dict[str, Any], eid: str) -> dict[str, object]:
    total_input = common.as_int(last.get("input_tokens"))
    cached = common.as_int(last.get("cached_input_tokens"))
    uncached = total_input - (cached or 0) if total_input is not None else None
    return {
        "event_id": eid,
        "input_tokens": uncached,
        "output_tokens": common.as_int(last.get("output_tokens")),
        "cache_read": cached,
        "cache_write": common.as_int(last.get("cache_write_input_tokens")),
        "model": None,
    }


# The session's own meta: the first whose payload id is the session (later ones are replayed).
_META = """
CREATE OR REPLACE TEMP TABLE codex_meta AS
SELECT e.session_id, f.history_start, f.parent, f.cwd, f.git_branch
FROM lane_pending_events e JOIN codex_fields f USING (event_id)
WHERE e.record_type = 'session_meta'
QUALIFY row_number() OVER (
    PARTITION BY e.session_id
    ORDER BY f.meta_id IS DISTINCT FROM e.session_id, e.seq
) = 1
"""

_INHERITED = """
CREATE OR REPLACE TEMP TABLE codex_inherited AS
SELECT e.event_id FROM lane_pending_events e JOIN codex_fields f USING (event_id)
JOIN codex_meta m USING (session_id)
WHERE TRY_CAST(f.ordinal AS BIGINT) < TRY_CAST(m.history_start AS BIGINT)
"""

_OWN = """
CREATE OR REPLACE TEMP VIEW codex_own AS
SELECT e.*, f.model AS usage_model, f.total AS usage_total
FROM lane_pending_events e LEFT JOIN codex_fields f USING (event_id)
WHERE event_id NOT IN (SELECT event_id FROM codex_inherited)
"""

_MODELS = """
CREATE OR REPLACE TEMP TABLE codex_models AS
SELECT session_id, seq, usage_model AS model FROM codex_own
WHERE record_type IN ('turn_context', 'event_msg:thread_settings_applied')
  AND usage_model IS NOT NULL AND usage_model <> ''
"""

_REPEATED_USAGE = """
DELETE FROM codex_usage WHERE event_id IN (
    SELECT event_id FROM (
        SELECT event_id, total, lag(total) OVER (PARTITION BY session_id ORDER BY seq) AS previous
        FROM (
            SELECT event_id, session_id, seq, usage_total AS total
            FROM codex_own WHERE record_type = 'event_msg:token_count'
        )
        WHERE total IS NOT NULL
    )
    WHERE total = previous
)
"""

_USAGE_MODEL = """
UPDATE codex_usage SET model = latest.model FROM (
    SELECT t.event_id, m.model
    FROM (SELECT event_id, session_id, seq FROM codex_own
          WHERE record_type = 'event_msg:token_count') t
    ASOF LEFT JOIN codex_models m ON t.session_id = m.session_id AND t.seq >= m.seq
) AS latest
WHERE codex_usage.event_id = latest.event_id
"""

_SESSIONS = """
INSERT INTO sessions (session_id, harness, parent_session_id, project, cwd, git_branch, model,
                      started_at, ended_at, source_file_id)
WITH spans AS (
    SELECT session_id, min(ts) AS started_at, max(ts) AS ended_at,
           arg_min(source_file_id, seq) AS source_file_id
    FROM codex_own GROUP BY session_id
), first_model AS (
    SELECT session_id, arg_min(model, seq) AS model FROM codex_models GROUP BY session_id
), fields AS (
    SELECT s.*, f.model,
           m.parent, m.cwd, m.git_branch
    FROM spans s
    LEFT JOIN codex_meta m USING (session_id)
    LEFT JOIN first_model f USING (session_id)
)
SELECT session_id, 'codex', parent, NULLIF(regexp_extract(cwd, '([^/]+)/*$', 1), ''), cwd,
       git_branch, model, started_at, ended_at, source_file_id
FROM fields
"""


def _fields(record: dict[str, Any], kind: str, location: dict[str, Any]) -> Iterator[Row]:
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if kind == "event_msg:token_count":
        info = payload.get("info")
        last = info.get("last_token_usage") if isinstance(info, dict) else None
        if isinstance(last, dict):
            yield Row("codex_usage", _usage(last, location["event_id"]))
    settings = payload.get("thread_settings")
    model = common.json_string(payload.get("model"))
    if model is None and isinstance(settings, dict):
        model = common.json_string(settings.get("model"))
    parent = common.json_string(payload.get("parent_thread_id"))
    source = payload.get("source")
    if parent is None and isinstance(source, dict):
        subagent = source.get("subagent")
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        if isinstance(spawn, dict):
            parent = common.json_string(spawn.get("parent_thread_id"))
    git = payload.get("git")
    info = payload.get("info")
    # Missing totals are excluded from lag; explicit JSON null remains the value "null".
    total = (
        json.dumps(info["total_token_usage"], ensure_ascii=False, separators=(",", ":"))
        if isinstance(info, dict) and "total_token_usage" in info
        else None
    )
    yield Row(
        "codex_fields",
        {
            "event_id": location["event_id"],
            "ordinal": common.json_string(record.get("ordinal")),
            "meta_id": common.json_string(payload.get("id")),
            "history_start": common.json_string(payload.get("subagent_history_start_ordinal")),
            "parent": parent,
            "cwd": common.json_string(payload.get("cwd")),
            "git_branch": common.json_string(git.get("branch")) if isinstance(git, dict) else None,
            "model": model,
            "total": total,
        },
    )
    loc = {key: location[key] for key in ("event_id", "session_id", "seq")}
    if kind in ("response_item:web_search_call", "event_msg:web_search_end"):
        action = json.dumps(payload.get("action"), sort_keys=True, ensure_ascii=False)
        yield Row(
            "codex_web_fields",
            {
                "event_id": location["event_id"],
                "seq": location["seq"],
                "record_type": kind,
                "call_id": common.as_str(payload.get("call_id")),
                "action_hash": hashlib.sha256(action.encode()).hexdigest(),
            },
        )
    yield from _extra_tools(kind, payload, loc)
    call = common.json_string(payload.get("call_id"))
    if call is None:
        return
    if kind in ("response_item:function_call", "response_item:custom_tool_call"):
        name = common.json_string(payload.get("name"))
        if name is None:
            return
        raw = common.json_string(
            payload.get("arguments" if kind == "response_item:function_call" else "input")
        )
        encoded = json.dumps(raw, ensure_ascii=False) if raw is not None else None
        if kind == "response_item:function_call" and raw is not None:
            try:
                json.loads(raw)
                encoded = raw
            except ValueError:
                pass
        yield Row("lane_uses", {**loc, "call_id": call, "tool_name": name, "input": encoded})
    elif kind in ("response_item:function_call_output", "response_item:custom_tool_call_output"):
        yield Row(
            "lane_results",
            {
                **loc,
                "call_id": call,
                "result_text": common.result_text(payload.get("output"), _MESSAGE_TEXT),
                "is_error": None,
            },
        )


def _use(loc: dict[str, Any], call: str, name: str, arguments: object) -> Row:
    return Row(
        "lane_uses",
        {
            **loc,
            "call_id": call,
            "tool_name": name,
            "input": json.dumps(arguments, ensure_ascii=False),
        },
    )


def _result(loc: dict[str, Any], call: str, output: object, error: bool | None = None) -> Row:
    text = common.result_text(output, (*_MESSAGE_TEXT, "text"))
    if text is None and output is not None:
        text = common.json_string(output)
    return Row("lane_results", {**loc, "call_id": call, "result_text": text, "is_error": error})


def _extra_tools(kind: str, payload: dict[str, Any], loc: dict[str, Any]) -> Iterator[Row]:
    call = common.as_str(payload.get("call_id"))
    child = (
        payload.get("agent_thread_id")
        or payload.get("new_thread_id")
        or payload.get("receiver_thread_id")
    )
    if isinstance(child, str) and kind in (
        "event_msg:sub_agent_activity",
        "event_msg:collab_agent_spawn_end",
        "event_msg:collab_agent_interaction_end",
    ):
        yield Row(
            "session_relations",
            {
                "event_id": loc["event_id"],
                "session_id": loc["session_id"],
                "child_session_id": child,
                "kind": common.as_str(payload.get("kind")) or kind,
            },
        )
    if kind == "response_item:web_search_call":
        # Some rollout versions omit the web call id; preserve the call with a
        # stable event identity. No result is invented for those versions.
        yield _use(loc, call or "web:" + loc["event_id"], "web_search", payload.get("action"))
    if call is None:
        return
    if kind == "response_item:tool_search_call":
        yield _use(loc, call, "tool_search", payload.get("arguments"))
    elif kind == "response_item:tool_search_output":
        yield _result(loc, call, payload.get("tools"))
    elif kind == "event_msg:mcp_tool_call_end":
        invocation = payload.get("invocation", {})
        name = f"mcp__{invocation.get('server', 'unknown')}__{invocation.get('tool', 'unknown')}"
        yield _use(loc, call, name, invocation.get("arguments"))
        result = payload.get("result", {})
        ok = result.get("Ok") if isinstance(result, dict) else None
        error = isinstance(result, dict) and "Err" in result
        if isinstance(ok, dict):
            error = error or bool(ok.get("isError"))
        yield _result(loc, call, ok.get("content") if isinstance(ok, dict) else result, error)
    elif kind == "event_msg:web_search_end":
        yield _use(loc, call, "web_search", payload.get("action", {"query": payload.get("query")}))
        yield _result(loc, call, payload.get("action"))
    elif kind == "event_msg:exec_command_end":
        yield _use(
            loc,
            call,
            "exec_command",
            {"command": payload.get("command"), "cwd": payload.get("cwd")},
        )
        exit_code = payload.get("exit_code")
        yield _result(
            loc,
            call,
            payload.get("aggregated_output"),
            exit_code != 0 if isinstance(exit_code, int) else None,
        )
    elif kind == "event_msg:patch_apply_end":
        changes = payload.get("changes", {})
        yield _use(loc, call, "apply_patch", {"changes": changes})
        yield _result(loc, call, payload.get("stdout"), payload.get("success") is False)
    elif kind.startswith("event_msg:collab_"):
        collab_name = {
            "event_msg:collab_agent_spawn_end": "spawn_agent",
            "event_msg:collab_agent_interaction_end": "send_input",
            "event_msg:collab_close_end": "close_agent",
            "event_msg:collab_waiting_end": "wait",
        }.get(kind)
        if collab_name:
            yield _use(loc, call, collab_name, payload)
            yield _result(loc, call, payload.get("status", payload.get("statuses")))


_RECOVERY_TYPES = (
    "response_item:web_search_call",
    "event_msg:exec_command_end",
    "event_msg:patch_apply_end",
    "event_msg:collab_agent_spawn_end",
    "event_msg:collab_agent_interaction_end",
    "event_msg:collab_close_end",
    "event_msg:collab_waiting_end",
)


def _recovery_fields(record: dict[str, Any], kind: str, loc: dict[str, Any]) -> Iterator[Row]:
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    child = payload.get("new_thread_id") or payload.get("receiver_thread_id")
    yield Row(
        "codex_recovery",
        {
            "event_id": loc["event_id"],
            "session_id": loc["session_id"],
            "seq": loc["seq"],
            "record_type": kind,
            "call_id": common.as_str(payload.get("call_id")),
            "child": common.as_str(child)
            if kind
            in ("event_msg:collab_agent_spawn_end", "event_msg:collab_agent_interaction_end")
            else None,
            "ordinal": common.as_int(record.get("ordinal")),
            "history_start": common.as_int(payload.get("subagent_history_start_ordinal")),
            "meta_id": common.as_str(payload.get("id")),
        },
    )


def _recover_known(con: duckdb.DuckDBPyConnection, config: Config) -> None:
    """Recover older recognized records whose tool/relationship extraction was absent.

    Decode only selected types, one row/byte batch at a time. Windows and anti-joins
    carry narrow identities. The durable revision and pending markers commit together;
    failed/interrupted derivations remain pending for normal finalization on retry.
    """
    if con.execute(
        "SELECT version FROM ingest_parser_versions WHERE harness='codex' AND version >= 2"
    ).fetchall():
        return
    con.execute(
        "CREATE TEMP TABLE codex_recovery (event_id VARCHAR, session_id VARCHAR, "
        "seq BIGINT, record_type VARCHAR, call_id VARCHAR, child VARCHAR, "
        "ordinal BIGINT, history_start BIGINT, meta_id VARCHAR)"
    )
    batches = analysis.event_batches(con, config.ingest.batch_rows)
    completed = 0
    while True:
        with analysis.event_transaction(con, batches) as span:
            if span is None:
                break
            start, end = span
            values: list[Row] = []
            for eid, sid, seq, kind, raw in con.sql(
                "SELECT event_id, session_id, seq, record_type, parsed::VARCHAR "
                "FROM events WHERE rowid >= $1 AND rowid < $2 AND seq IS NOT NULL "
                "AND source_file_id IN (SELECT source_file_id FROM source_files "
                "WHERE harness='codex') "
                "AND record_type IN (SELECT unnest($3::VARCHAR[]))",
                params=[start, end, ["session_meta", *_RECOVERY_TYPES]],
            ).iter_rows(batch_size=1):
                record = json.loads(raw) if raw is not None else {}
                if isinstance(record, dict):
                    values.extend(
                        _recovery_fields(
                            record, kind, {"event_id": eid, "session_id": sid, "seq": seq}
                        )
                    )
            engine._insert_rows(con, values)
        completed += 1
        tick(f"stage recovery batch {completed}")
    with engine.transaction(con):
        con.execute(
            "CREATE TEMP TABLE codex_recovery_meta AS SELECT session_id,history_start "
            "FROM codex_recovery WHERE record_type='session_meta' "
            "QUALIFY row_number() OVER(PARTITION BY session_id "
            "ORDER BY meta_id IS DISTINCT FROM session_id,seq)=1"
        )
        con.execute(
            "UPDATE events SET seq=NULL WHERE event_id IN (SELECT r.event_id "
            "FROM codex_recovery r LEFT JOIN codex_recovery_meta m USING(session_id) "
            "WHERE r.record_type<>'session_meta' "
            "AND NOT coalesce(r.ordinal < m.history_start,false) AND ("
            "(r.call_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM tool_calls t "
            " WHERE t.session_id=r.session_id AND t.call_id=r.call_id AND "
            " (r.record_type='response_item:web_search_call' "
            " OR t.result_event_id IS NOT NULL))) OR "
            "(r.record_type='response_item:web_search_call' AND r.call_id IS NULL "
            " AND NOT EXISTS (SELECT 1 FROM tool_calls t WHERE t.session_id=r.session_id "
            " AND t.tool_name='web_search' AND t.use_event_id=r.event_id)) OR "
            "(r.child IS NOT NULL AND NOT EXISTS (SELECT 1 FROM session_relations l "
            " WHERE l.event_id=r.event_id))))"
        )
        con.execute("DELETE FROM ingest_parser_versions WHERE harness='codex'")
        con.execute("INSERT INTO ingest_parser_versions VALUES ('codex',2)")
        con.execute("DROP TABLE codex_recovery; DROP TABLE codex_recovery_meta")


def finalize(con: duckdb.DuckDBPyConnection, config: Config) -> None:
    """Derive sessions, tool calls, usage models and price-table cost of every pending Codex
    session; publishes completion after bounded body batches."""
    _recover_known(con, config)
    common.reclassify(
        con,
        HARNESS,
        KINDS,
        rederive={
            "event_msg:mcp_tool_call_end",
            "event_msg:web_search_end",
            "response_item:tool_search_call",
            "response_item:tool_search_output",
            "event_msg:sub_agent_activity",
            "event_msg:collab_agent_interaction_end",
        },
    )
    con.execute(common._QUEUE, [HARNESS])
    con.execute(
        "DELETE FROM usage WHERE event_id IN (SELECT e.event_id FROM events e "
        "JOIN lane_queue q USING(session_id) JOIN source_files f USING(source_file_id) "
        "WHERE f.harness='codex')"
    )
    con.execute("DROP TABLE lane_queue")
    common.pending(con, HARNESS, lambda: _derive(con, config))
    con.execute(
        "UPDATE sessions SET parent_session_id = r.session_id FROM "
        "(SELECT child_session_id, arg_min(session_id, event_id) AS session_id "
        "FROM session_relations WHERE kind='event_msg:collab_agent_spawn_end' "
        "GROUP BY child_session_id) r "
        "WHERE sessions.session_id=r.child_session_id AND sessions.harness='codex' "
        "AND sessions.parent_session_id IS NULL"
    )


def _derive(con: duckdb.DuckDBPyConnection, config: Config) -> None:
    with common.tool_tables(con, config) as bodies:
        con.execute(
            "CREATE TEMP TABLE codex_fields (event_id VARCHAR, ordinal VARCHAR, meta_id VARCHAR, "
            "history_start VARCHAR, parent VARCHAR, cwd VARCHAR, git_branch VARCHAR, "
            "model VARCHAR, total VARCHAR)"
        )
        con.execute(
            "CREATE TEMP TABLE codex_web_fields (event_id VARCHAR, seq BIGINT, "
            "record_type VARCHAR, call_id VARCHAR, action_hash VARCHAR)"
        )
        con.execute(
            "CREATE TEMP TABLE codex_usage (event_id VARCHAR, input_tokens BIGINT, "
            "output_tokens BIGINT, cache_read BIGINT, cache_write BIGINT, model VARCHAR)"
        )
        common.json_batches(con, config, _fields, bodies)
        for statement in (_META, _INHERITED, _OWN, _MODELS):
            con.execute(statement)
        for table in (
            "messages",
            "codex_usage",
            "lane_uses",
            "lane_results",
            "session_relations",
            "codex_web_fields",
        ):
            con.execute(
                f"DELETE FROM {table} WHERE event_id IN (SELECT event_id FROM codex_inherited)"
            )
        for statement in (_REPEATED_USAGE, _USAGE_MODEL):
            con.execute(statement)
        # Older rollouts emit an id-bearing web_search_end immediately beside
        # a response_item without its id. Collapse that duplicate observation,
        # keeping the end event's real identity and result. Windows carry hashes.
        con.execute(
            "DELETE FROM lane_uses WHERE event_id IN (SELECT event_id FROM ("
            "SELECT *, lag(record_type) OVER w AS prev_kind, "
            "lag(action_hash) OVER w AS prev_action, "
            "lead(record_type) OVER w AS next_kind, "
            "lead(action_hash) OVER w AS next_action FROM codex_web_fields "
            "WINDOW w AS (ORDER BY seq)) WHERE "
            "record_type='response_item:web_search_call' AND call_id IS NULL AND "
            "((prev_kind='event_msg:web_search_end' AND prev_action=action_hash) OR "
            "(next_kind='event_msg:web_search_end' AND next_action=action_hash)))"
        )
        common.insert_tools(con, config, bodies)
        con.execute("INSERT INTO usage SELECT * FROM codex_usage")
        con.execute(_SESSIONS)
        common.insert_costs(con, config, usage_table="codex_usage")
        con.execute("DROP VIEW codex_own")
        for table in (
            "codex_models",
            "codex_inherited",
            "codex_meta",
            "codex_fields",
            "codex_web_fields",
            "codex_usage",
        ):
            con.execute(f"DROP TABLE {table}")


def source(root: Path | None = None) -> engine.HarnessSource:
    """Every ``*.jsonl`` under ``root``, or under the configured Codex sessions root."""

    def discover(config: Config) -> list[Path]:
        base = root if root is not None else config.ingest.sources.codex_sessions
        return sorted(base.rglob("*.jsonl")) if base.is_dir() else []

    return engine.HarnessSource(CodexParser(), discover, finalize)
