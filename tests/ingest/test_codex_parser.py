import json
from pathlib import Path
from typing import Any

import pytest

from slopsniffer.config import load_config
from slopsniffer.ingest import codex
from slopsniffer.ingest.codex import KINDS, SUBTYPED, UNKNOWN
from tests.ingest import lane_support, support
from tests.ingest.record_support import CODEX

FABRICATED = "event_msg:zzz_fabricated"
ROOT = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"
OLD_CHILD = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return lane_support.fixture_root(tmp_path, monkeypatch, codex.HARNESS, codex.source)


def _rollout(root: Path, session: str) -> list[dict[str, Any]]:
    (path,) = root.rglob(f"rollout-*-{session}.jsonl")
    return lane_support.records(path)


def _canon(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def _fixture_type(record: dict[str, Any]) -> str:
    sub = record["payload"].get("type") if record["type"] in SUBTYPED else None
    return f"{record['type']}:{sub}" if isinstance(sub, str) else str(record["type"])


def test_every_fixture_record_type_is_stored_and_unknown_is_kept(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (path,) = root.rglob(f"*-{ROOT}.jsonl")
    with path.open("a") as out:
        for record in CODEX:
            out.write(json.dumps(record) + "\n")
    fixture_types = {_fixture_type(r) for r in lane_support.all_records(root)}
    assert set(KINDS) <= fixture_types, "fixture must contain every known record type"
    assert FABRICATED in fixture_types and FABRICATED not in KINDS

    lane_support.ingest(capsys, codex.HARNESS)

    stored = dict(support.query("SELECT DISTINCT record_type, kind FROM events"))
    assert set(stored) == fixture_types
    assert {t for t, kind in stored.items() if kind == UNKNOWN} == {FABRICATED}


def test_function_calls_pair_with_their_outputs_by_call_id(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lane_support.ingest(capsys, codex.HARNESS)
    rows = support.query(
        "SELECT t.session_id, call_id, tool_name, CAST(input AS VARCHAR), result_text, "
        "u.record_type, r.record_type FROM tool_calls t "
        "JOIN events u ON u.event_id = t.use_event_id "
        "LEFT JOIN events r ON r.event_id = t.result_event_id"
    )
    web = [(c, json.loads(i)) for s, c, name, i, *_ in rows if name == "web_search"]
    assert [(c.startswith("web:"), i) for c, i in web] == [(True, {"type": "search"})]
    calls = {
        (s, c, name, _canon(json.loads(i)), text, use, r)
        for s, c, name, i, text, use, r in rows
        if name != "web_search"
    }
    fc, fco = "response_item:function_call", "response_item:function_call_output"
    cc, cco = "response_item:custom_tool_call", "response_item:custom_tool_call_output"
    old_output = json.dumps({"output": "ok", "metadata": {"exit_code": 0}})
    # p-c3 has no output; the child's replayed copy of p-c1 stays the parent's.
    assert calls == {
        (ROOT, "p-c1", "shell", _canon({"cmd": ["ls"]}), "file-a\nfile-b", fc, fco),
        (ROOT, "p-c2", "apply_patch", _canon("*** Begin Patch"), "Done", cc, cco),
        (ROOT, "p-c3", "read", _canon("not json"), None, fc, None),
        (CHILD, "c-c1", "shell", _canon({"cmd": ["pwd"]}), "/work/alpha", fc, fco),
        (OLD_CHILD, "o-c1", "shell", _canon({"cmd": ["true"]}), old_output, fc, fco),
    }


def test_replayed_parent_history_yields_no_rows_in_the_subagent(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lane_support.ingest(capsys, codex.HARNESS)
    texts = support.query(
        "SELECT e.session_id, m.role, m.text FROM messages m JOIN events e USING (event_id)"
    )
    assert set(texts) == {
        (ROOT, "user", "hello codex"),
        (ROOT, "assistant", "done"),
        (CHILD, "assistant", "child done"),
    }
    models = dict(support.query("SELECT session_id, model FROM sessions"))
    assert models == {ROOT: "gpt-5", CHILD: "gpt-unpriced", OLD_CHILD: "gpt-5-codex"}


def test_child_rollouts_link_to_the_parent_their_meta_names(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lane_support.ingest(capsys, codex.HARNESS)
    rows = support.query(
        "SELECT session_id, harness, parent_session_id, cwd, git_branch FROM sessions"
    )
    assert set(rows) == {
        (ROOT, "codex", None, "/work/alpha", "main"),
        (CHILD, "codex", ROOT, "/work/alpha", None),
        (OLD_CHILD, "codex", ROOT, "/work/beta", None),
    }


def _last_usage(record: dict[str, Any]) -> dict[str, int]:
    last: dict[str, int] = record["payload"]["info"]["last_token_usage"]
    return last


type UsageRow = tuple[str, int, int, int, int, str]


def _usage_row(session: str, last: dict[str, int], model: str) -> UsageRow:
    cached = last["cached_input_tokens"]
    return (
        session,
        last["input_tokens"] - cached,
        last["output_tokens"],
        cached,
        last["cache_write_input_tokens"],
        model,
    )


def test_usage_counts_each_response_once_and_cost_comes_from_the_price_table(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    by_ordinal = {r["ordinal"]: r for r in _rollout(root, ROOT)}
    # Ordinal 14 repeats ordinal 12's cumulative total and the final token_count has no info.
    expected: set[UsageRow] = {
        _usage_row(ROOT, _last_usage(by_ordinal[12]), "gpt-5"),
        _usage_row(ROOT, _last_usage(by_ordinal[20]), "gpt-5-codex"),
        _usage_row(CHILD, _last_usage(_rollout(root, CHILD)[8]), "gpt-unpriced"),
        *(
            _usage_row(OLD_CHILD, _last_usage(r), "gpt-5-codex")
            for r in _rollout(root, OLD_CHILD)
            if r["payload"].get("type") == "token_count"
        ),
    }

    lane_support.ingest(capsys, codex.HARNESS)

    usage = support.query(
        "SELECT e.session_id, input_tokens, output_tokens, cache_read, cache_write, model "
        "FROM usage JOIN events e USING (event_id)"
    )
    assert set(usage) == expected

    pricing = load_config().pricing
    totals: dict[str, float] = {}
    for session, uncached, output, cached, written, model in expected:
        if (price := pricing.get(model)) is None:
            continue
        totals[session] = (
            totals.get(session, 0.0)
            + (
                uncached * price.input_usd_per_mtok
                + written * price.cache_write_usd_per_mtok
                + cached * price.cache_read_usd_per_mtok
                + output * price.output_usd_per_mtok
            )
            / 1e6
        )
    costs = {s: (c, m) for s, c, m in support.query("SELECT * FROM session_cost")}
    assert costs == {
        ROOT: (pytest.approx(totals[ROOT]), "price_table"),
        OLD_CHILD: (pytest.approx(totals[OLD_CHILD]), "price_table"),
        CHILD: (0.0, "unpriced"),
    }
