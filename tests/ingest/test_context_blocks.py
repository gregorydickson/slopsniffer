from pathlib import Path

import pytest

from tests.ingest import claude_support, support

BLOCKS = """
SELECT e.parsed->>'uuid', c.kind, c.source_path, c.content_hash
FROM context_blocks c JOIN events e USING (event_id)
"""


@pytest.fixture
def blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> list[tuple[str, str, str | None, str]]:
    claude_support.fixture_root(tmp_path, monkeypatch)
    claude_support.ingest(capsys)
    return support.query(BLOCKS)


def kinds_of(
    blocks: list[tuple[str, str, str | None, str]], uuid: str
) -> set[tuple[str, str | None]]:
    return {(kind, path) for u, kind, path, _ in blocks if u == uuid}


def test_reminders_split_into_kinds_keyed_by_name(
    blocks: list[tuple[str, str, str | None, str]],
) -> None:
    # Instructions preamble, CLAUDE.md section, MEMORY.md section, skills listing, unrelated.
    assert kinds_of(blocks, "s-main-u1") == {
        ("other", None),
        ("claude_md", "/work/alpha/CLAUDE.md"),
        ("memory_index", "/home/u/.claude/memory/MEMORY.md"),
        ("skill_listing", None),
    }
    assert kinds_of(blocks, "s-main-u12") == {("hook_output", None)}
    assert kinds_of(blocks, "s-main-u2") == {("skill_listing", None)}
    assert kinds_of(blocks, "s-main-u3") == {("hook_output", None)}
    # Plain prompts and non-context attachments carry no blocks.
    assert kinds_of(blocks, "s-main-u4") == set()
    assert kinds_of(blocks, "s-split-u1") == set()


def test_unrelated_reminder_is_other_with_its_full_text(
    blocks: list[tuple[str, str, str | None, str]],
) -> None:
    others = {h for u, kind, _, h in blocks if u == "s-main-u1" and kind == "other"}
    contents = {
        c
        for (c,) in support.query(
            "SELECT content FROM blobs WHERE content_hash IN (SELECT unnest(?::VARCHAR[]))",
            [list(others)],
        )
    }
    assert "\nThe date has changed. Today is 2026-11-09.\n" in contents


def test_identical_blocks_in_two_sessions_share_one_blob(
    blocks: list[tuple[str, str, str | None, str]],
) -> None:
    def claude_md_hash(session_uuid: str) -> set[str]:
        return {h for u, kind, _, h in blocks if u == session_uuid and kind == "claude_md"}

    shared = claude_md_hash("s-main-u1")
    assert shared == claude_md_hash("s-other-u1")
    # The api-request-blob carries the same reminder, too.
    blob_line = {h for u, kind, _, h in blocks if u is None and kind == "claude_md"}
    assert blob_line == shared
    rows = support.query(
        "SELECT content_hash FROM blobs WHERE content_hash IN (SELECT unnest(?::VARCHAR[]))",
        [list(shared)],
    )
    assert rows == [(h,) for h in shared]


def test_resume_and_reingest_preserve_unique_history_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = claude_support.fixture_root(tmp_path, monkeypatch)
    target = next(root.rglob("*.jsonl"))
    original = target.read_bytes()
    claude_support.ingest(capsys)
    # Both append/resume and a changed head exercise the shipping offset/delete paths.
    for content in (original + original, b"{}\n" + original):
        target.write_bytes(content)
        claude_support.ingest(capsys)
        for table, key in {
            "source_files": "path",
            "events": "event_id",
            "messages": "event_id",
            "usage": "event_id",
            "context_blocks": "event_id, ordinal",
            "skill_invocations": "event_id, skill_name",
            "tool_calls": "session_id, call_id",
            "sessions": "session_id",
            "session_cost": "session_id",
            "blobs": "content_hash",
        }.items():
            assert (
                support.query(f"SELECT {key} FROM {table} GROUP BY {key} HAVING count(*) <> 1")
                == []
            ), table
        assert set(support.event_pairs()) == set().union(
            *(support.line_starts(p) for p in root.rglob("*.jsonl"))
        )
        assert (
            support.query("SELECT event_id FROM messages EXCEPT SELECT event_id FROM events") == []
        )
