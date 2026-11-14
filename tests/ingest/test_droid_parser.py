import json
import re
from pathlib import Path
from typing import Any

import pytest

from slopsniffer.config import load_config
from slopsniffer.ingest import droid
from slopsniffer.ingest.droid import KINDS, UNKNOWN
from tests.helpers import REPO_ROOT
from tests.ingest import lane_support, support

FABRICATED = "zzz-fabricated"
ROOT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CHILD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
FORK = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
FORMAT_DOC = REPO_ROOT / "docs" / "formats" / "droid.md"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return lane_support.fixture_root(tmp_path, monkeypatch, droid.HARNESS, droid.source)


def _fixture_types(root: Path) -> set[str]:
    return {r["type"] for r in lane_support.all_records(root)}


def test_every_fixture_record_type_is_stored_and_unknown_is_kept(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture_types = _fixture_types(root)
    assert set(KINDS) <= fixture_types, "fixture must contain every known record type"
    assert FABRICATED in fixture_types and FABRICATED not in KINDS

    lane_support.ingest(capsys, droid.HARNESS)

    stored = dict(support.query("SELECT DISTINCT record_type, kind FROM events"))
    assert set(stored) == fixture_types
    assert {t for t, kind in stored.items() if kind == UNKNOWN} == {FABRICATED}


def test_format_doc_names_every_record_type_the_fixture_uses(root: Path) -> None:
    documented = set(re.findall(r"^### `([^`]+)`", FORMAT_DOC.read_text(), re.M))
    assert documented == _fixture_types(root) - {FABRICATED}


def _canon(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def test_tool_uses_pair_with_their_results(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lane_support.ingest(capsys, droid.HARNESS)
    rows = support.query(
        "SELECT session_id, call_id, tool_name, CAST(input AS VARCHAR), result_text, is_error "
        "FROM tool_calls"
    )
    calls = {(s, c, name, _canon(json.loads(i)), text, err) for s, c, name, i, text, err in rows}
    # a-t3 has no result, so neither text nor error state is known.
    assert calls == {
        (ROOT, "a-t1", "Read", _canon({"file_path": "/work/alpha/x"}), "boom", True),
        (ROOT, "a-t2", "Grep", _canon({"pattern": "x"}), "match", False),
        (ROOT, "a-t3", "Execute", _canon({"command": "ls"}), None, None),
        (CHILD, "b-t1", "LS", _canon({"path": "/work/beta"}), "a b", False),
    }


def test_subagents_and_forks_link_to_the_session_their_start_names(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lane_support.ingest(capsys, droid.HARNESS)
    rows = support.query("SELECT session_id, harness, parent_session_id, cwd, model FROM sessions")
    assert set(rows) == {
        (ROOT, "droid", None, "/work/alpha", "claude-sonnet-4-5"),
        (CHILD, "droid", ROOT, "/work/beta", "unpriced-model"),
        (FORK, "droid", ROOT, "/work/alpha", None),
    }


def _sidecar(root: Path, session: str) -> dict[str, Any]:
    (path,) = root.rglob(f"{session}{droid.SIDECAR_SUFFIX}")
    settings: dict[str, Any] = json.loads(path.read_text())
    return settings


def test_usage_and_cost_come_from_the_sidecar_and_the_price_table(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not list(root.rglob(f"{FORK}{droid.SIDECAR_SUFFIX}")), "fork has no sidecar"
    sidecars = {s: _sidecar(root, s) for s in (ROOT, CHILD)}

    lane_support.ingest(capsys, droid.HARNESS)

    usage = support.query(
        "SELECT e.session_id, e.record_type, input_tokens, output_tokens, cache_read, "
        "cache_write, model FROM usage JOIN events e USING (event_id)"
    )
    assert set(usage) == {
        (
            s,
            "session_start",
            c["tokenUsage"]["inputTokens"],
            c["tokenUsage"]["outputTokens"],
            c["tokenUsage"]["cacheReadTokens"],
            c["tokenUsage"]["cacheCreationTokens"],
            c["model"],
        )
        for s, c in sidecars.items()
    }
    tokens, price = sidecars[ROOT]["tokenUsage"], load_config().pricing[sidecars[ROOT]["model"]]
    root_cost = (
        tokens["inputTokens"] * price.input_usd_per_mtok
        + tokens["cacheCreationTokens"] * price.cache_write_usd_per_mtok
        + tokens["cacheReadTokens"] * price.cache_read_usd_per_mtok
        + tokens["outputTokens"] * price.output_usd_per_mtok
    ) / 1e6
    costs = {s: (c, m) for s, c, m in support.query("SELECT * FROM session_cost")}
    assert sidecars[CHILD]["model"] not in load_config().pricing
    assert costs == {ROOT: (pytest.approx(root_cost), "price_table"), CHILD: (0.0, "unpriced")}


def test_a_grown_session_rereads_its_sidecar(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lane_support.ingest(capsys, droid.HARNESS)
    (transcript,) = root.rglob(f"{CHILD}.jsonl")
    (sidecar,) = root.rglob(f"{CHILD}{droid.SIDECAR_SUFFIX}")
    settings = json.loads(sidecar.read_text())
    settings["tokenUsage"]["outputTokens"] += 7
    sidecar.write_text(json.dumps(settings))
    with transcript.open("a") as f:
        f.write(json.dumps({"type": "session_end", "timestamp": "2026-11-09T12:00:00.000Z"}) + "\n")

    lane_support.ingest(capsys, droid.HARNESS)

    rows = support.query(
        "SELECT output_tokens FROM usage JOIN events e USING (event_id) WHERE e.session_id = ?",
        [CHILD],
    )
    assert rows == [(settings["tokenUsage"]["outputTokens"],)]
