import json
import shutil
from pathlib import Path

import pytest

from slopsniffer.ingest.claude import KINDS, UNKNOWN
from tests.ingest import claude_support, support
from tests.ingest.record_support import CLAUDE

FABRICATED = "zzz-fabricated"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return claude_support.fixture_root(tmp_path, monkeypatch)


def test_every_fixture_record_type_is_stored_and_unknown_is_kept(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = root / "expanded.jsonl"
    path.write_text("".join(json.dumps({**r, "sessionId": "expanded"}) + "\n" for r in CLAUDE))
    fixture_types = {r["type"] for r in claude_support.records(*root.rglob("*.jsonl"))}
    assert set(KINDS) <= fixture_types, "fixture must contain every known record type"
    assert FABRICATED in fixture_types and FABRICATED not in KINDS

    claude_support.ingest(capsys)

    stored = dict(support.query("SELECT DISTINCT record_type, kind FROM events"))
    assert set(stored) == fixture_types
    assert {t for t, kind in stored.items() if kind == UNKNOWN} == {FABRICATED}


def test_messages_usage_and_session_cost(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    claude_support.ingest(capsys)
    usage = support.query(
        "SELECT e.parsed->>'uuid', input_tokens, output_tokens, cache_read, cache_write, model "
        "FROM usage JOIN events e USING (event_id)"
    )
    assert set(usage) == {("s-main-u5", 10, 20, 30, 40, "claude-test-1")}
    # cost-state is cumulative: the session's last one wins.
    assert set(support.query("SELECT session_id, cost_usd, method FROM session_cost")) == {
        ("s-main", 0.75, "cost_state"),
        ("a1", 0.1, "cost_state"),
    }
    texts = dict(
        support.query(
            "SELECT e.parsed->>'uuid', m.text FROM messages m JOIN events e USING (event_id) "
            "WHERE e.session_id = 's-split'"
        )
    )
    words = ["first", "second", "third", "fourth"]
    assert texts == {f"s-split-u{n}": word for n, word in enumerate(words, 1)}


def test_skill_invocations_from_skill_tool_and_slash_command_markup(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    claude_support.ingest(capsys)
    rows = support.query(
        "SELECT e.parsed->>'uuid', s.session_id, skill_name, via, args "
        "FROM skill_invocations s JOIN events e USING (event_id)"
    )
    # The prompt that merely quotes <command-name> markup is not an invocation.
    assert set(rows) == {
        ("s-main-u5", "s-main", "demo-skill", "skill_tool", "fast"),
        ("s-main-u10", "s-main", "plan-it", "slash_command", "ship it"),
    }


def test_split_session(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    parts = sorted((root / "-work-beta").glob("s-split-*.jsonl"))
    claude_support.ingest(capsys)

    assert support.query("SELECT session_id FROM sessions WHERE session_id = 's-split'") == [
        ("s-split",)
    ]
    ordered = support.query(
        "SELECT seq, parsed->>'uuid', parsed->>'timestamp' FROM events "
        "WHERE session_id = 's-split' ORDER BY seq"
    )
    seqs = [seq for seq, _, _ in ordered]
    assert seqs == list(range(1, len(claude_support.records(*parts)) + 1))
    stamped = [r for r in claude_support.records(*parts) if "timestamp" in r]
    expected = [r["uuid"] for r in sorted(stamped, key=lambda r: r["timestamp"])]
    assert [uuid for _, uuid, ts in ordered if ts is not None] == expected


def test_reingest_is_idempotent_for_derived_rows(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    claude_support.ingest(capsys)
    tables = ("sessions", "tool_calls", "session_cost", "skill_invocations", "context_blocks")

    def snapshot() -> dict[str, set[tuple[object, ...]]]:
        # Timestamps compare as text: fetching TIMESTAMPTZ values needs pytz.
        rows = {t: f"SELECT * FROM {t}" for t in tables}
        rows["sessions"] = "SELECT COLUMNS(*)::VARCHAR FROM sessions"
        return {t: set(support.query(sql)) for t, sql in rows.items()}

    before = snapshot()
    (root / "-work-alpha" / "s-main.jsonl").touch()
    with (root / "-work-beta" / "s-split-b.jsonl").open("a") as f:
        f.write('{"type": "tools-used", "sessionId": "s-split", "tools": []}\n')
    claude_support.ingest(capsys)
    assert snapshot() == before
    assert support.query("SELECT event_id FROM events WHERE seq IS NULL") == []


def test_cli_ingests_the_configured_claude_projects_root(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No registration here: the CLI's own source reads ingest.sources.claude_projects.
    shutil.copytree(claude_support.FIXTURES, isolated_home / ".claude" / "projects")
    claude_support.ingest(capsys)
    fixture_ids = {
        r["sessionId"] for r in claude_support.records(*claude_support.FIXTURES.rglob("*.jsonl"))
    }
    stored = {
        s
        for (s,) in support.query("SELECT session_id FROM sessions WHERE parent_session_id IS NULL")
    }
    assert stored == fixture_ids


def test_odd_shapes_neither_fail_the_file_nor_invent_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "projects"
    (root / "-odd").mkdir(parents=True)
    path = root / "-odd" / "s-odd.jsonl"
    path.write_bytes(
        support.jsonl(
            ["not", "an", "object"],
            {
                "type": "user",
                "sessionId": "s-odd",
                "uuid": "u1",
                "timestamp": "yesterday",
                "message": {
                    "role": "user",
                    "content": ["bare string block", {"type": "text", "text": 7}],
                },
            },
            {"type": "attachment", "sessionId": "s-odd", "uuid": "u2", "attachment": "flat"},
            {
                "type": "assistant",
                "sessionId": "s-odd",
                "uuid": "u3",
                "message": {"content": [{"type": "tool_use", "name": "Skill", "input": "flat"}]},
            },
        )
        + b"{not json}\n"
    )
    claude_support.register(monkeypatch, root)
    claude_support.ingest(capsys)

    assert support.query("SELECT record_type, kind, ts FROM events ORDER BY byte_offset") == [
        ("unknown", "unknown", None),
        ("user", "message", None),
        ("attachment", "context", None),
        ("assistant", "message", None),
        ("malformed", "malformed", None),
    ]
    assert support.error_offsets(path) == {len(path.read_bytes()) - len(b"{not json}\n")}
    assert set(support.query("SELECT role, text FROM messages")) == {
        ("user", None),
        ("assistant", None),
    }
    for table in ("context_blocks", "skill_invocations", "usage", "tool_calls"):
        assert support.query(f"SELECT * FROM {table}") == [], table
