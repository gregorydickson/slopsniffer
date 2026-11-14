from pathlib import Path

import pytest

from tests.ingest import claude_support, support


def test_subagent_files_link_to_their_parent_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = claude_support.fixture_root(tmp_path, monkeypatch)
    expected = {
        (p.parent.parent.name, p.stem.removeprefix("agent-"))
        for p in root.rglob("subagents/agent-*.jsonl")
    }
    assert expected, "fixture must contain subagent files"
    claude_support.ingest(capsys)

    sessions = dict(support.query("SELECT session_id, parent_session_id FROM sessions"))
    assert {(parent, child) for child, parent in sessions.items() if parent} == expected
    top_level = {p.stem for p in root.glob("*/*.jsonl")} - {"s-split-a", "s-split-b"} | {"s-split"}
    assert {s for s, parent in sessions.items() if parent is None} == top_level
    # Subagent lines name the parent in sessionId but belong to the child session.
    owners = support.query(
        "SELECT DISTINCT session_id FROM events e JOIN source_files f USING (source_file_id) "
        "WHERE f.path LIKE '%/subagents/agent-a1.jsonl'"
    )
    assert owners == [("a1",)]
