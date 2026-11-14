"""Parser-to-finalizer contracts with real rows and no child-process coverage gap."""

import json
import locale
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import duckdb
import pytest

from slopsniffer.config import ModelPrice, load_config
from slopsniffer.contracts import Harness
from slopsniffer.db import apply_schema
from slopsniffer.ingest import claude, codex, common, droid, engine, pickle, reader, skills
from tests.helpers import REPO_ROOT

FIXTURES = REPO_ROOT / "tests/fixtures/ingest"
RECORDS = json.loads((FIXTURES / "finalize-contracts.json").read_text("utf-8"))


@contextmanager
def ascii_locale() -> Iterator[None]:
    previous = locale.setlocale(locale.LC_CTYPE)
    locale.setlocale(locale.LC_CTYPE, "C")
    try:
        yield
    finally:
        locale.setlocale(locale.LC_CTYPE, previous)


@pytest.mark.parametrize("harness", ["claude", "codex", "droid"])
def test_finalization_derives_only_its_harness_and_prices_its_usage(
    harness: Harness, tmp_path: Path
) -> None:
    path = tmp_path / "root.jsonl"
    path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in RECORDS[harness]))
    shutil.copy(FIXTURES / "sidecar-contract.json", path.with_suffix(".settings.json"))
    config = replace(load_config(), pricing={"model-é": ModelPrice(3, 1, 5, 3)})
    source = {"claude": claude.source, "codex": codex.source, "droid": droid.source}[harness]()
    with duckdb.connect(config={"threads": 1}) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO source_files (source_file_id, path, harness) VALUES (17, ?, ?)",
            [str(path), harness],
        )
        con.execute(
            "INSERT INTO source_files (source_file_id, path, harness) "
            "VALUES (23, '/synthetic/foreign', 'pickle')"
        )
        con.execute(
            "INSERT INTO events (event_id, source_file_id, session_id, record_type, kind, "
            "byte_offset, byte_length) VALUES ('foreign', 23, 'foreign', 'other', 'other', 0, 1)"
        )
        counts = engine._empty_counts()
        writer = engine._Writer(con, {17: engine._Tracked(str(path), counts)})
        batch = reader.read_batch(
            reader.BatchRequest(str(path), 17, 0, path.stat().st_size, 100, source.parser)
        )
        assert writer.commit(batch)
        assert source.finalize is not None
        with ascii_locale():
            source.finalize(con, config)
        assert con.execute(
            "SELECT session_id, harness, project, cwd, model FROM sessions"
        ).fetchall() == [("root", harness, "repo", "/repo", "model-é")]
        assert con.execute(
            "SELECT input_tokens, output_tokens, cache_read, cache_write, model FROM usage"
        ).fetchall() == [(23, 5, 7, 2, "model-é")]
        calls = con.execute(
            "SELECT session_id, call_id, tool_name, input, result_text, is_error FROM tool_calls"
        ).fetchall()
        assert [(s, c, n, json.loads(i), t, e) for s, c, n, i, t, e in calls] == [
            ("root", "call", "Read", {"path": "x"}, "done", None if harness == "codex" else False)
        ]
        cost = 0.75 if harness == "claude" else ((23 + 2) * 3 + 7 * 1 + 5 * 5) / 1e6
        method = "cost_state" if harness == "claude" else "price_table"
        assert con.execute("SELECT session_id, cost_usd, method FROM session_cost").fetchall() == [
            ("root", pytest.approx(cost), method)
        ]
        assert con.execute("SELECT event_id FROM events WHERE seq IS NULL").fetchall() == [
            ("foreign",)
        ]


@pytest.mark.parametrize("value", [None, [], {}, {"tokenUsage": []}, {"tokenUsage": None}])
def test_sidecar_without_usage_emits_no_row(tmp_path: Path, value: object) -> None:
    path = tmp_path / "root.jsonl"
    path.with_suffix(".settings.json").write_text(json.dumps(value))
    assert droid._sidecar_usage("probe", path) is None


def test_sidecar_rejects_bad_json_and_missing_files(tmp_path: Path) -> None:
    path = tmp_path / "root.jsonl"
    assert droid._sidecar_usage("probe", path) is None
    path.with_suffix(".settings.json").write_text("{bad json}")
    assert droid._sidecar_usage("probe", path) is None


def test_sidecar_numeric_fields_exclude_bools_strings_and_floats(tmp_path: Path) -> None:
    path = tmp_path / "root.jsonl"
    path.with_suffix(".settings.json").write_text(
        json.dumps(
            {
                "model": 3,
                "tokenUsage": {
                    "inputTokens": True,
                    "outputTokens": "5",
                    "cacheReadTokens": 2.5,
                    "cacheCreationTokens": False,
                },
            }
        )
    )
    assert droid._sidecar_usage("probe", path) == ("probe", None, None, None, None, None)


def test_utf8_sidecar_fields_are_independent_of_locale(tmp_path: Path) -> None:
    path = tmp_path / "root.jsonl"
    shutil.copy(FIXTURES / "sidecar-contract.json", path.with_suffix(".settings.json"))
    with ascii_locale():
        assert droid._sidecar_usage("probe", path) == ("probe", 23, 5, 7, 2, "model-é")


def test_utf8_pickle_state_and_plugin_manifests_are_independent_of_locale(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(
        '{"active":true,"step":"étape","iteration":3,"current_ticket":"ticket",'
        '"max_iterations":7,"start_time_epoch":123,"working_dir":"/répo"}',
        encoding="utf-8",
    )
    root = tmp_path / "plugins"
    skill = root / "plug/skills/demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\n---\n")
    manifest = root / "plug/.claude-plugin/plugin.json"
    manifest.parent.mkdir()
    manifest.write_text('{"name":"plüg"}', encoding="utf-8")
    with ascii_locale():
        assert pickle._run_row("run", run) == (
            "run",
            str(run),
            True,
            "étape",
            3,
            7,
            "ticket",
            123,
            "/répo",
        )
        assert {s.name for s in skills.inventory([], [root], [])} == {"plüg:demo"}
    (run / "state.json").write_text("[]")
    assert pickle._run_row("run", run) == (
        "run",
        str(run),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    (run / "state.json").write_text(
        json.dumps(
            {
                "active": "yes",
                "step": 3,
                "iteration": True,
                "max_iterations": "7",
                "current_ticket": [],
                "start_time_epoch": 2.5,
                "working_dir": {},
            }
        )
    )
    assert pickle._run_row("run", run) == (
        "run",
        str(run),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )


def test_finalization_cleans_orphans_without_deleting_live_rows() -> None:
    with duckdb.connect(config={"threads": 1}) as con:
        apply_schema(con)
        con.execute("INSERT INTO sessions (session_id, harness) VALUES ('kept', 'claude')")
        con.execute(
            "INSERT INTO source_files (source_file_id, path, harness) "
            "VALUES (17, '/synthetic/kept', 'claude')"
        )
        con.execute(
            "INSERT INTO events (event_id, session_id, seq, record_type, kind, source_file_id, "
            "byte_offset, byte_length) "
            "VALUES ('kept-event', 'kept', 1, 'user', 'message', 17, 0, 1)"
        )
        for sid, eid in [("kept", "kept-event"), ("gone", "missing-event")]:
            con.execute(
                "INSERT INTO tool_calls (session_id, call_id, use_event_id, tool_name) "
                "VALUES (?, 'call', ?, 'Read')",
                [sid, eid],
            )
            con.execute(
                "INSERT INTO session_cost (session_id, cost_usd, method) "
                "VALUES (?, 0.25, 'cost_state')",
                [sid],
            )
        claude.finalize(con, load_config())
        assert con.execute("SELECT session_id, use_event_id FROM tool_calls").fetchall() == [
            ("kept", "kept-event")
        ]
        assert con.execute("SELECT session_id, cost_usd, method FROM session_cost").fetchall() == [
            ("kept", 0.25, "cost_state")
        ]


def test_pending_traversal_includes_empty_keys_but_excludes_null_sessions(tmp_path: Path) -> None:
    path = tmp_path / "imported.jsonl"
    records = [("empty", ""), ("later", "later"), ("without-session", None)]
    lines = [
        json.dumps({"type": "system", "sessionId": sid}).encode() + b"\n" for _, sid in records
    ]
    path.write_bytes(b"".join(lines))
    with duckdb.connect(config={"threads": 1}) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO source_files (source_file_id, path, harness) VALUES (17, ?, 'claude')",
            [str(path)],
        )
        offset = 0
        for (eid, sid), line in zip(records, lines, strict=True):
            con.execute(
                "INSERT INTO events (event_id, session_id, record_type, kind, source_file_id, "
                "byte_offset, byte_length, parsed) VALUES (?, ?, 'system', 'system', 17, ?, ?, ?)",
                [eid, sid, offset, len(line), line.decode().strip()],
            )
            offset += len(line)
        claude.finalize(con, load_config())
        assert set(con.execute("SELECT session_id FROM sessions").fetchall()) == {("",), ("later",)}
        assert con.execute("SELECT event_id FROM events WHERE seq IS NULL").fetchall() == [
            ("without-session",)
        ]


def test_claude_finalizer_preserves_json_values_and_tool_result_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.ingest.support import jsonl

    path = tmp_path / "root.jsonl"
    path.write_bytes(
        jsonl(
            [1, 2],
            {
                "type": "assistant",
                "sessionId": "coerce",
                "cwd": {"répo": ["déjà", 1]},
                "gitBranch": [],
                "message": {
                    "model": True,
                    "content": [
                        None,
                        {"type": "tool_use", "name": "missing-id"},
                        {"type": "tool_use", "id": "missing-name"},
                        {"type": "tool_use", "id": 0, "name": False, "input": None},
                        {
                            "type": "tool_use",
                            "id": "é",
                            "name": "Read",
                            "input": {"s": "é", "n": 1},
                        },
                        {"type": "tool_use", "id": "solo", "name": "Read"},
                        {"type": "tool_use", "id": "numeric", "name": "Read"},
                        {"type": "tool_use", "id": "empty", "name": "Read"},
                        {"type": "text", "id": "not-use", "name": "Read", "text": "ignored"},
                        {"type": "tool_result", "tool_use_id": "solo", "content": "wrong-role"},
                    ],
                },
            },
            {
                "type": "user",
                "sessionId": "coerce",
                "toolUseResult": {"durationMs": "19"},
                "message": {
                    "content": [
                        {"type": "tool_result", "content": "orphan"},
                        {"type": "tool_use", "id": "wrong-role", "name": "Read"},
                        {"type": "not-result", "tool_use_id": "solo", "content": "ignored"},
                        {"type": "tool_result", "tool_use_id": "numeric", "content": 7},
                        {"type": "tool_result", "tool_use_id": "empty", "content": []},
                        {
                            "type": "tool_result",
                            "tool_use_id": 0,
                            "is_error": "yes",
                            "content": [
                                {"type": "text", "text": {"répo": ["déjà", 1]}},
                                {"type": "text", "text": True},
                                {"type": "text", "text": None},
                                {"type": "image", "text": "excluded"},
                            ],
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "é",
                            "is_error": "invalid",
                            "content": None,
                        },
                    ]
                },
            },
            {
                "type": "user",
                "sessionId": "coerce",
                "toolUseResult": {"durationMs": "12"},
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "solo", "is_error": 1, "content": ""}
                    ]
                },
            },
            {"type": "cost-state", "sessionId": "coerce", "totalCostUSD": "0.5"},
            {"type": "assistant", "sessionId": "coerce", "message": None},
        )
    )
    with duckdb.connect(config={"memory_limit": "128MB", "threads": 1}) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO source_files (source_file_id, path, harness) VALUES (17, ?, 'claude')",
            [str(path)],
        )
        writer = engine._Writer(con, {17: engine._Tracked(str(path), engine._empty_counts())})
        assert writer.commit(
            reader.read_batch(
                reader.BatchRequest(
                    str(path), 17, 0, path.stat().st_size, 100, claude.ClaudeParser()
                )
            )
        )
        from tests.ingest.deadlines import deadline

        monkeypatch.setattr(reader, "BATCH_BYTES", 100)
        config = load_config()
        config = replace(config, ingest=replace(config.ingest, batch_rows=2))
        with deadline(3, "bounded Claude field batches"):
            claude.finalize(con, config)
        sessions = con.execute(
            "SELECT session_id, cwd, git_branch, model FROM sessions ORDER BY session_id"
        ).fetchall()
        assert sessions == [
            ("coerce", '{"répo":["déjà",1]}', "[]", "true"),
            ("root", None, None, None),
        ]
        calls = con.execute(
            "SELECT call_id, tool_name, input, result_text, is_error, duration_ms "
            "FROM tool_calls ORDER BY call_id"
        ).fetchall()
        assert calls == [
            ("0", "false", "null", '{"répo":["déjà",1]}\ntrue', True, None),
            ("empty", "Read", None, None, False, None),
            ("numeric", "Read", None, None, False, None),
            ("solo", "Read", None, "", True, 12),
            ("é", "Read", '{"s":"é","n":1}', None, False, None),
        ]
        assert con.execute("SELECT session_id, cost_usd, method FROM session_cost").fetchall() == [
            ("coerce", 0.5, "cost_state")
        ]


def test_codex_missing_totals_keep_usage_and_do_not_break_repeated_total_detection() -> None:
    samples = [
        ("first", True, {"input_tokens": 1}),
        ("missing-before-repeat", False, None),
        ("repeated", True, {"input_tokens": 1}),
        ("explicit-null", True, None),
        ("repeated-null", True, None),
        ("missing-after-null", False, None),
    ]
    with duckdb.connect(config={"memory_limit": "64MB", "threads": 1}) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO source_files (source_file_id, path, harness) "
            "VALUES (1, '/synthetic/root.jsonl', 'codex')"
        )
        for offset, (eid, present, total) in enumerate(samples):
            info = {"total_token_usage": total} if present else {}
            info["last_token_usage"] = {"input_tokens": 1}
            record = {"type": "event_msg", "payload": {"type": "token_count", "info": info}}
            con.execute(
                "INSERT INTO events (event_id, session_id, source_file_id, byte_offset, "
                "byte_length, record_type, kind, parsed) "
                "VALUES (?, 'root', 1, ?, 1, 'event_msg:token_count', 'usage', ?)",
                [eid, offset, json.dumps(record)],
            )
            con.execute("INSERT INTO usage (event_id, input_tokens) VALUES (?, 1)", [eid])
        codex.finalize(con, load_config())
        assert set(con.execute("SELECT event_id FROM usage").fetchall()) == {
            ("first",),
            ("missing-before-repeat",),
            ("explicit-null",),
            ("missing-after-null",),
        }
        assert set(con.execute("SELECT event_id FROM events").fetchall()) == {
            (eid,) for eid, _, _ in samples
        }


def test_interleaved_sessions_fetch_only_the_requested_raw_identities() -> None:
    samples = [
        ("own-first", "own", "/work/own"),
        ("foreign", "foreign", "/work/foreign"),
        ("own-unparsed", "own", None),
        ("own-last", "own", "/wrong"),
    ]
    with duckdb.connect(config={"memory_limit": "64MB", "threads": 1}) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO source_files (source_file_id, path, harness) "
            "VALUES (1, '/mixed', 'claude')"
        )
        for offset, (eid, sid, cwd) in enumerate(samples):
            con.execute(
                "INSERT INTO events (event_id, session_id, source_file_id, byte_offset, "
                "byte_length, record_type, kind, parsed) "
                "VALUES (?, ?, 1, ?, 1, 'system', 'system', ?)",
                [
                    eid,
                    sid,
                    offset,
                    json.dumps(
                        {
                            "type": "system",
                            "sessionId": sid,
                            "cwd": cwd,
                            "gitBranch": "topic" if eid == "own-last" else None,
                        }
                    )
                    if cwd is not None
                    else None,
                ],
            )
        claude.finalize(con, load_config())
        assert set(con.execute("SELECT session_id, cwd, git_branch FROM sessions").fetchall()) == {
            ("own", "/work/own", "topic"),
            ("foreign", "/work/foreign", None),
        }
        assert set(con.execute("SELECT event_id FROM events WHERE seq IS NOT NULL").fetchall()) == {
            (eid,) for eid, _, _ in samples
        }


def test_body_spool_closes_on_derivation_failure() -> None:
    with (
        duckdb.connect(config={"memory_limit": "64MB", "threads": 1}) as con,
        pytest.raises(RuntimeError, match="derivation failed"),
        common.tool_tables(con, load_config()) as stream,
    ):
        stream.write(b"body")
        raise RuntimeError("derivation failed")
    assert stream.closed


@pytest.mark.parametrize("harness", ["codex", "droid"])
def test_tool_finalization_keeps_value_types_and_rejects_unrelated_blocks(
    harness: Harness,
    tmp_path: Path,
) -> None:
    text_kind = "output_text" if harness == "codex" else "text"
    parts = [
        None,
        {"type": "ignored", "text": "skip"},
        {"type": text_kind, "text": None},
        {"type": text_kind, "text": ""},
        {"type": text_kind, "text": 0},
        {"type": text_kind, "text": False},
        {"type": text_kind, "text": {"x": "é", "y": [1, 2]}},
        {"type": text_kind, "text": "done"},
    ]
    inputs = {"a": 1, "b": [1, 2]}
    if harness == "codex":
        records = [
            {"type": "session_meta", "ordinal": 1, "payload": {"id": "parent", "cwd": "/wrong"}},
            {
                "type": "session_meta",
                "ordinal": 5,
                "payload": {
                    "id": "root",
                    "cwd": "/repo",
                    "subagent_history_start_ordinal": 5,
                    "source": {"subagent": None},
                },
            },
            {"type": "turn_context", "ordinal": 6, "payload": {"model": "model-é"}},
            {"type": "unknown", "payload": None},
            {"type": "unknown", "payload": []},
            {"type": "unknown"},
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "good",
                    "name": "Read",
                    "arguments": json.dumps(inputs),
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call", "call_id": "no-input", "name": "Read"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "empty",
                    "name": "Read",
                    "arguments": "null",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "custom",
                    "name": "Read",
                    "input": '{"looks":"json"}',
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call", "call_id": "missing-name"},
            },
            {"type": "response_item", "payload": {"type": "function_call", "name": "missing-id"}},
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "good", "output": parts},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "empty",
                    "output": [None, {"type": "ignored", "text": "skip"}],
                },
            },
        ]
        source = codex.source(tmp_path)
        error = None
    else:
        records = [
            {"type": "session_start", "cwd": "/repo"},
            {
                "type": "todo_state",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "wrong-kind", "name": "Read", "input": {}}
                    ],
                },
            },
            {"type": "message", "message": None},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "modelId": "model-é",
                    "content": [
                        None,
                        7,
                        "odd",
                        {"type": "tool_use", "id": "good", "name": "Read", "input": inputs},
                        {"type": "tool_use", "id": "no-input", "name": "Read"},
                        {"type": "tool_use", "id": "empty", "name": "Read", "input": None},
                        {"type": "tool_use", "id": "missing-name"},
                        {"type": "tool_use", "name": "missing-id"},
                        {"type": "text", "id": "wrong-type", "name": "Read", "input": {}},
                        {"type": "tool_result", "tool_use_id": "good", "content": "wrong-role"},
                    ],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_use", "id": "wrong-role", "name": "Read", "input": {}},
                        {"type": "text", "tool_use_id": "good", "content": "wrong-type"},
                        {"type": "tool_result", "tool_use_id": "good", "content": parts},
                        {
                            "type": "tool_result",
                            "tool_use_id": "empty",
                            "content": [None, {"type": "ignored", "text": "skip"}],
                        },
                    ],
                },
            },
        ]
        source = droid.source(tmp_path)
        error = False
    path = tmp_path / "root.jsonl"
    path.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in records))
    with duckdb.connect(config={"memory_limit": "64MB", "threads": 1}) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO source_files (source_file_id, path, harness) VALUES (1, ?, ?)",
            [str(path), harness],
        )
        writer = engine._Writer(con, {1: engine._Tracked(str(path), engine._empty_counts())})
        batch = reader.read_batch(
            reader.BatchRequest(str(path), 1, 0, path.stat().st_size, len(records), source.parser)
        )
        assert writer.commit(batch)
        assert con.execute("SELECT error FROM ingest_errors").fetchall() == []
        con.execute("CHECKPOINT")
        assert source.finalize is not None
        source.finalize(con, load_config())
        rows = con.execute(
            "SELECT call_id, tool_name, input, result_text, is_error, "
            "result_event_id IS NOT NULL FROM tool_calls"
        ).fetchall()
        actual = {
            call: (
                name,
                json.loads(body) if body is not None else None,
                body is None,
                text,
                failed,
                matched,
            )
            for call, name, body, text, failed, matched in rows
        }
        expected: dict[str, tuple[object, ...]] = {
            "good": ("Read", inputs, False, '\n0\nfalse\n{"x":"é","y":[1,2]}\ndone', error, True),
            "no-input": ("Read", None, True, None, None, False),
            "empty": ("Read", None, False, None, error, True),
        }
        if harness == "codex":
            expected["custom"] = ("Read", '{"looks":"json"}', False, None, None, False)
        assert actual == expected
        assert con.execute("SELECT session_id, cwd, model FROM sessions").fetchall() == [
            ("root", "/repo", "model-é")
        ]
