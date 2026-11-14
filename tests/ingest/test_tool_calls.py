from pathlib import Path

import pytest

from tests.ingest import claude_support, support

CALLS = """
SELECT t.call_id, t.tool_name, u.parsed->>'uuid', r.parsed->>'uuid', t.is_error,
       t.result_text, t.duration_ms
FROM tool_calls t JOIN events u ON u.event_id = t.use_event_id
LEFT JOIN events r ON r.event_id = t.result_event_id
"""


@pytest.fixture
def calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> dict[str, tuple[object, ...]]:
    claude_support.fixture_root(tmp_path, monkeypatch)
    claude_support.ingest(capsys)
    return {row[0]: row[1:] for row in support.query(CALLS)}


def fixture_pairs() -> set[tuple[str, str, str | None]]:
    """(call id, use uuid, result uuid or None) read straight from the fixture."""
    uses: dict[str, str] = {}
    results: dict[str, str] = {}
    for r in claude_support.records(*claude_support.FIXTURES.rglob("*.jsonl")):
        content = r.get("message", {}).get("content")
        for block in content if isinstance(content, list) else []:
            if block["type"] == "tool_use":
                uses[block["id"]] = r["uuid"]
            elif block["type"] == "tool_result":
                results[block["tool_use_id"]] = r["uuid"]
    return {(cid, use, results.get(cid)) for cid, use in uses.items()}


def test_uses_join_their_results_by_id(calls: dict[str, tuple[object, ...]]) -> None:
    assert {(cid, row[1], row[2]) for cid, row in calls.items()} == fixture_pairs()


def test_orphaned_use_has_null_result(calls: dict[str, tuple[object, ...]]) -> None:
    assert calls["t-orphan"] == ("Grep", "s-main-u5", None, None, None, None)


def test_is_error_both_polarities_and_result_text(calls: dict[str, tuple[object, ...]]) -> None:
    assert calls["t-err"][3:5] == (True, "no such\nfile")
    assert calls["t-ok"][3:6] == (False, "built", 1234)
    # A result without is_error is a success; a non-object toolUseResult carries no duration.
    assert calls["t-skill"][3] is False
    assert calls["t-err"][5] is None
    # A line's toolUseResult duration is not attributable when the line holds two results.
    assert {calls[c][4:6] for c in ("t-pair-1", "t-pair-2")} == {("a", None), ("b", None)}
