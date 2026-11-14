import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest

from slopsniffer.config import load_config
from slopsniffer.db import apply_schema
from slopsniffer.ingest import claude, codex, common, engine, reader
from tests.ingest.record_support import CLAUDE, CODEX


@pytest.mark.parametrize("backfill", [False, True])
def test_codex_new_records_recover_tools_compaction_and_child_links(
    tmp_path: Path, backfill: bool
) -> None:
    path = tmp_path / "parent.jsonl"
    records = [{"type": "session_meta", "payload": {"id": "parent", "cwd": "/work"}}, *CODEX]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    config = replace(load_config(), ingest=replace(load_config().ingest, batch_rows=2))
    with duckdb.connect(config={"threads": 1, "memory_limit": "64MB"}) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO source_files (source_file_id,path,harness) VALUES (17,?,'codex')",
            [str(path)],
        )
        batch = reader.read_batch(
            reader.BatchRequest(str(path), 17, 0, path.stat().st_size, 100, codex.CodexParser())
        )
        writer = engine._Writer(con, {17: engine._Tracked(str(path), engine._empty_counts())})
        assert writer.commit(batch)
        if backfill:
            con.execute("INSERT INTO ingest_parser_versions VALUES ('codex',2)")
            con.execute("UPDATE events SET seq=1")
            con.execute("UPDATE events SET kind='unknown' WHERE record_type<>'session_meta'")
            con.execute("INSERT INTO sessions (session_id,harness) VALUES ('parent','codex')")
        con.execute(
            "INSERT INTO sessions (session_id,harness) VALUES ('child','codex'),('peer','codex')"
        )
        codex.finalize(con, config)
        calls = con.execute(
            "SELECT call_id,tool_name,input::VARCHAR,result_text,is_error FROM tool_calls"
        ).fetchall()
        assert {
            (c, n, json.dumps(json.loads(i), sort_keys=True), t, e) for c, n, i, t, e in calls
        } == {
            ("mcp-ok", "mcp__memory__lookup", '{"query": "needle"}', "found", False),
            (
                "mcp-error",
                "mcp__memory__lookup",
                '{"query": "missing"}',
                '{"Err":"transport failed"}',
                True,
            ),
            (
                "web",
                "web_search",
                '{"query": "sqlite", "type": "search"}',
                '{"type":"search","query":"sqlite"}',
                None,
            ),
            ("search", "tool_search", '{"query": "tools"}', '[{"name":"lookup"}]', None),
            (
                "spawn",
                "spawn_agent",
                json.dumps(CODEX[5]["payload"], sort_keys=True),
                "pending_init",
                None,
            ),
            (
                "interaction",
                "send_input",
                json.dumps(CODEX[-1]["payload"], sort_keys=True),
                '{"completed":"done"}',
                None,
            ),
        }
        assert con.execute(
            "SELECT parent_session_id FROM sessions WHERE session_id='child'"
        ).fetchall() == [("parent",)]
        assert set(
            con.execute("SELECT child_session_id,kind FROM session_relations").fetchall()
        ) == {
            ("child", "started"),
            ("peer", "event_msg:collab_agent_interaction_end"),
            ("child", "event_msg:collab_agent_spawn_end"),
        }
        assert con.execute("SELECT record_type FROM events WHERE kind='compaction'").fetchall() == [
            ("event_msg:context_compacted",)
        ]
        assert not con.execute("SELECT record_type FROM events WHERE kind='unknown'").fetchall()
        assert con.execute(
            "SELECT parent_session_id FROM sessions WHERE session_id='peer'"
        ).fetchall() == [(None,)]
        before = con.execute("SELECT * FROM tool_calls ORDER BY call_id").fetchall()
        links = con.execute("SELECT * FROM session_relations ORDER BY event_id").fetchall()
        con.execute("UPDATE events SET seq=NULL WHERE session_id='parent'")
        codex.finalize(con, config)
        assert con.execute("SELECT * FROM tool_calls ORDER BY call_id").fetchall() == before
        assert con.execute("SELECT * FROM session_relations ORDER BY event_id").fetchall() == links


def test_claude_context_file_history_and_fork_are_queryable_after_backfill(tmp_path: Path) -> None:
    path = tmp_path / "child.jsonl"
    path.write_text("".join(json.dumps({**r, "sessionId": "child"}) + "\n" for r in CLAUDE))
    config = load_config()
    with duckdb.connect(config={"threads": 1, "memory_limit": "64MB"}) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO source_files (source_file_id,path,harness) VALUES (17,?,'claude')",
            [str(path)],
        )
        batch = reader.read_batch(
            reader.BatchRequest(str(path), 17, 0, path.stat().st_size, 100, claude.ClaudeParser())
        )
        writer = engine._Writer(con, {17: engine._Tracked(str(path), engine._empty_counts())})
        assert writer.commit(batch)
        con.execute("UPDATE events SET kind='unknown',seq=1")
        con.execute("INSERT INTO sessions (session_id,harness) VALUES ('child','claude')")
        claude.finalize(con, config)
        assert con.execute(
            "SELECT session_id,parent_session_id,cwd,git_branch FROM sessions"
        ).fetchall() == [("child", "ancestor", "/work/final", "review")]
        assert set(
            con.execute("SELECT record_type FROM events WHERE kind='file_history'").fetchall()
        ) == {("file-history-snapshot",), ("file-history-delta",)}
        assert not con.execute("SELECT record_type FROM events WHERE kind='unknown'").fetchall()


def test_claude_workflow_journals_keep_distinct_owners_and_repair_existing_rows(
    tmp_path: Path,
) -> None:
    config = load_config()
    with duckdb.connect(config={"threads": 1, "memory_limit": "64MB"}) as con:
        apply_schema(con)
        expected = set()
        for fid, owner in enumerate(["parent-a", "parent-b"], 17):
            path = tmp_path / owner / "subagents/workflows/wf-1/journal.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"type": "started", "agentId": owner + "-child"}) + "\n")
            parsed = claude.ClaudeParser().parse_line(
                path.read_bytes(), reader.LineContext(str(path), fid, 0, "test")
            )
            assert parsed.event.session_id == owner
            con.execute(
                "INSERT INTO source_files (source_file_id,path,harness) VALUES (?,?,'claude')",
                [fid, str(path)],
            )
            # Seed exactly the old parser's collision, bypassing the repaired parser.
            con.execute(
                "INSERT INTO events (event_id,session_id,seq,record_type,kind,source_file_id,"
                "byte_offset,byte_length,parsed) VALUES "
                "(?,'journal',1,'started','unknown',?,0,?,?)",
                [owner, fid, path.stat().st_size, path.read_text()],
            )
            expected.add((owner, owner + "-child"))
        con.execute("INSERT INTO sessions (session_id,harness) VALUES ('journal','claude')")
        claude.finalize(con, config)
        assert (
            set(con.execute("SELECT session_id,child_session_id FROM session_relations").fetchall())
            == expected
        )
        assert {row[0] for row in con.execute("SELECT session_id FROM sessions").fetchall()} == {
            parent for parent, _ in expected
        }
        before = con.execute("SELECT * FROM session_relations ORDER BY session_id").fetchall()
        claude.finalize(con, config)
        assert (
            con.execute("SELECT * FROM session_relations ORDER BY session_id").fetchall() == before
        )


def test_codex_web_observations_pair_without_dropping_distinct_queries(tmp_path: Path) -> None:
    records = [
        {
            "type": "event_msg",
            "payload": {
                "type": "web_search_end",
                "call_id": "web-id",
                "action": {"type": "search", "query": "same"},
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "web_search_call", "action": {"type": "search", "query": "same"}},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "web_search_call",
                "action": {"type": "search", "query": "different"},
            },
        },
    ]
    path = tmp_path / "web.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    with duckdb.connect(config={"threads": 1}) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO source_files (source_file_id,path,harness) VALUES (17,?,'codex')",
            [str(path)],
        )
        writer = engine._Writer(con, {17: engine._Tracked(str(path), engine._empty_counts())})
        assert writer.commit(
            reader.read_batch(
                reader.BatchRequest(str(path), 17, 0, path.stat().st_size, 100, codex.CodexParser())
            )
        )
        codex.finalize(con, load_config())
        assert set(
            con.execute(
                "SELECT tool_name,input->>'query',result_event_id IS NOT NULL FROM tool_calls"
            ).fetchall()
        ) == {("web_search", "same", True), ("web_search", "different", False)}
        assert con.execute(
            "SELECT call_id FROM tool_calls WHERE result_event_id IS NOT NULL"
        ).fetchall() == [("web-id",)]


def test_known_record_recovery_selects_missing_calls_and_excludes_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(load_config(), ingest=replace(load_config().ingest, batch_rows=1))
    records: dict[str, list[dict[str, Any]]] = {
        "legacy": [
            {"type": "session_meta", "payload": {"id": "legacy"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "already",
                    "name": "Read",
                    "arguments": '{"path":"kept"}',
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "exec_command_end",
                    "call_id": "already",
                    "aggregated_output": "kept",
                    "exit_code": 0,
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "exec_command_end",
                    "call_id": "missing-shell",
                    "command": ["true"],
                    "aggregated_output": "done",
                    "exit_code": 0,
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "web_search_call", "action": {"query": "recover"}},
            },
            {
                "type": "response_item",
                "payload": {"type": "web_search_call", "action": {"query": "kept"}},
            },
        ],
        "inherited": [
            {
                "type": "session_meta",
                "ordinal": 100,
                "payload": {"id": "inherited", "subagent_history_start_ordinal": 100},
            },
            {
                "type": "response_item",
                "ordinal": 40,
                "payload": {"type": "web_search_call", "action": {"query": "parent replay"}},
            },
        ],
    }
    records["legacy"].extend(
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "partial",
                    "name": "Read",
                    "arguments": "{}",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "exec_command_end",
                    "call_id": "partial",
                    "aggregated_output": "recovered result",
                    "exit_code": 0,
                },
            },
        ]
    )
    with duckdb.connect(config={"threads": 1, "memory_limit": "64MB"}) as con:
        apply_schema(con)
        for fid, (sid, items) in enumerate(records.items(), 17):
            path = tmp_path / (sid + ".jsonl")
            path.write_text("".join(json.dumps(r) + "\n" for r in items))
            con.execute(
                "INSERT INTO source_files (source_file_id,path,harness) VALUES (?,?,'codex')",
                [fid, str(path)],
            )
            writer = engine._Writer(con, {fid: engine._Tracked(str(path), engine._empty_counts())})
            assert writer.commit(
                reader.read_batch(
                    reader.BatchRequest(
                        str(path), fid, 0, path.stat().st_size, 100, codex.CodexParser()
                    )
                )
            )
            con.execute("INSERT INTO sessions (session_id,harness) VALUES (?,'codex')", [sid])
        con.execute("UPDATE events SET seq=1")
        con.execute("INSERT INTO ingest_parser_versions VALUES ('codex',1)")
        existing = con.execute(
            "SELECT event_id FROM events WHERE record_type='response_item:function_call' "
            "AND json_extract_string(parsed, '$.payload.call_id')='already'"
        ).fetchall()[0][0]
        con.execute(
            "INSERT INTO tool_calls (call_id,session_id,use_event_id,tool_name) "
            "VALUES ('already','legacy',?,'Read')",
            [existing],
        )
        result_id = con.execute(
            "SELECT event_id FROM events WHERE record_type='event_msg:exec_command_end' "
            "AND json_extract_string(parsed,'$.payload.call_id')='already'"
        ).fetchall()[0][0]
        con.execute("UPDATE tool_calls SET result_event_id=? WHERE call_id='already'", [result_id])
        web_id = con.execute(
            "SELECT event_id FROM events WHERE record_type='response_item:web_search_call' "
            "AND json_extract_string(parsed,'$.payload.action.query')='kept'"
        ).fetchall()[0][0]
        con.execute(
            "INSERT INTO tool_calls (call_id,session_id,use_event_id,tool_name,input) "
            "VALUES (?,'legacy',?,'web_search','{\"query\":\"kept\"}')",
            ["web:" + web_id, web_id],
        )
        partial = con.execute(
            "SELECT event_id FROM events WHERE record_type='response_item:function_call' "
            "AND json_extract_string(parsed,'$.payload.call_id')='partial'"
        ).fetchall()[0][0]
        con.execute(
            "INSERT INTO tool_calls (call_id,session_id,use_event_id,tool_name) "
            "VALUES ('partial','legacy',?,'Read')",
            [partial],
        )
        commits: list[int] = []

        class Observe:
            def __getattr__(self, name: str) -> Any:
                return getattr(con, name)

            def commit(self) -> None:
                if con.execute(
                    "SELECT 1 FROM duckdb_tables() WHERE table_name='codex_recovery'"
                ).fetchall():
                    commits.append(
                        con.execute("SELECT count(*) FROM codex_recovery").fetchall()[0][0]
                    )
                con.commit()

        original_pending = common.pending

        def pending(*args: Any, **kwargs: Any) -> None:
            assert commits and all(
                b - a <= config.ingest.batch_rows
                for a, b in zip([0, *commits], commits, strict=False)
            )
            assert set(
                con.execute(
                    "SELECT session_id,record_type FROM events WHERE seq IS NULL"
                ).fetchall()
            ) == {
                ("legacy", "event_msg:exec_command_end"),
                ("legacy", "response_item:web_search_call"),
            }
            assert con.execute(
                "SELECT json_extract_string(parsed,'$.payload.action.query') FROM events "
                "WHERE seq IS NULL AND record_type='response_item:web_search_call'"
            ).fetchall() == [("recover",)]
            assert (
                con.execute(
                    "SELECT event_id FROM events WHERE seq IS NULL "
                    "AND json_extract_string(parsed, '$.payload.call_id')='already'"
                ).fetchall()
                == []
            )
            assert set(
                con.execute(
                    "SELECT json_extract_string(parsed,'$.payload.call_id') "
                    "FROM events WHERE seq IS NULL AND record_type='event_msg:exec_command_end'"
                ).fetchall()
            ) == {("missing-shell",), ("partial",)}
            original_pending(*args, **kwargs)

        monkeypatch.setattr(common, "pending", pending)
        codex.finalize(cast(duckdb.DuckDBPyConnection, Observe()), config)
        monkeypatch.setattr(common, "pending", original_pending)
        calls = con.execute("SELECT call_id,session_id,tool_name FROM tool_calls").fetchall()
        assert {
            (call if not call.startswith("web:") else "web", sid, name) for call, sid, name in calls
        } == {
            ("already", "legacy", "Read"),
            ("missing-shell", "legacy", "exec_command"),
            ("partial", "legacy", "Read"),
            ("web", "legacy", "web_search"),
        }
        assert con.execute(
            "SELECT result_text FROM tool_calls WHERE call_id='partial'"
        ).fetchall() == [("recovered result",)]
        assert set(
            con.execute(
                "SELECT input->>'query' FROM tool_calls WHERE tool_name='web_search'"
            ).fetchall()
        ) == {("kept",), ("recover",)}
        assert con.execute("SELECT * FROM ingest_parser_versions").fetchall() == [("codex", 2)]
        before = con.execute("SELECT * FROM tool_calls ORDER BY call_id").fetchall()
        codex.finalize(con, config)
        assert con.execute("SELECT * FROM tool_calls ORDER BY call_id").fetchall() == before
