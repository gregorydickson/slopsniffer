import os
from pathlib import Path

import pytest

from slopsniffer.config import CONFIG_ENV
from slopsniffer.ingest.reader import MALFORMED
from tests.ingest import support
from tests.ingest.parsers import ExplodingParser, RejectedRowParser

GOOD = support.jsonl({"type": "good", "n": 1})
BAD = b'{"type": "bad", \n'
TAIL = b'{"type": "tail"}'


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    support.register(monkeypatch, root)
    return root


def kinds_by_offset(path: Path) -> dict[int, str]:
    rows = support.query(
        "SELECT e.byte_offset, e.kind FROM events e JOIN source_files f USING (source_file_id) "
        "WHERE f.path = ?",
        [str(path.resolve())],
    )
    return dict(rows)


def test_bad_middle_line_is_recorded_while_its_neighbours_ingest(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = root / "mixed.jsonl"
    path.write_bytes(GOOD + BAD + GOOD)
    code, counts = support.ingest(capsys)
    assert code == 0
    assert counts["files_failed"] == []
    bad_offset = len(GOOD)
    assert support.error_offsets(path) == {bad_offset}
    assert kinds_by_offset(path) == {
        offset: (MALFORMED if offset == bad_offset else "passthrough")
        for _, offset in support.line_starts(path)
    }
    assert support.file_state(path) == ("complete", path.stat().st_size)


def test_clean_file_records_no_errors(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = root / "clean.jsonl"
    path.write_bytes(GOOD + GOOD)
    support.ingest(capsys)
    assert support.error_offsets(path) == set()
    assert set(kinds_by_offset(path).values()) == {"passthrough"}


def test_unterminated_last_line_waits_for_its_newline(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = root / "live.jsonl"
    path.write_bytes(GOOD + GOOD + TAIL)
    tail_offset = len(GOOD) * 2
    support.ingest(capsys)
    assert set(kinds_by_offset(path)) == {offset for _, offset in support.line_starts(path)}
    assert tail_offset not in kinds_by_offset(path)
    assert support.file_state(path) == ("complete", tail_offset)
    assert support.error_offsets(path) == set()

    with path.open("ab") as f:
        f.write(b"\n")
    support.ingest(capsys)
    assert kinds_by_offset(path)[tail_offset] == "passthrough"
    assert support.file_state(path) == ("complete", path.stat().st_size)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
def test_unreadable_file_fails_alone(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    locked, fine = root / "locked.jsonl", root / "fine.jsonl"
    locked.write_bytes(GOOD)
    fine.write_bytes(GOOD)
    locked.chmod(0)
    try:
        code, counts = support.ingest(capsys)
    finally:
        locked.chmod(0o644)
    assert code == 3
    assert counts["files_failed"] == [str(locked.resolve())]
    assert support.file_state(locked)[0] == "failed"
    assert support.error_offsets(locked) == {None}
    assert support.file_state(fine) == ("complete", fine.stat().st_size)


def test_parser_bug_fails_only_its_file(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    support.register(monkeypatch, root, ExplodingParser())
    broken, fine = root / "broken.jsonl", root / "fine.jsonl"
    broken.write_bytes(GOOD + support.jsonl({"type": "boom"}))
    fine.write_bytes(GOOD)
    code, counts = support.ingest(capsys)
    assert code == 3
    assert counts["files_failed"] == [str(broken.resolve())]
    assert support.file_state(broken) == ("failed", 0)
    assert support.file_state(fine) == ("complete", fine.stat().st_size)


def test_batch_the_database_rejects_rolls_back_and_fails_only_its_file(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[ingest]\nbatch_rows = 1\n", "utf-8")
    monkeypatch.setenv(CONFIG_ENV, str(config))
    support.register(monkeypatch, root, RejectedRowParser())
    rejected, fine = root / "rejected.jsonl", root / "fine.jsonl"
    rejected.write_bytes(support.jsonl({"type": "reject"}) + GOOD + GOOD)
    fine.write_bytes(GOOD)
    code, counts = support.ingest(capsys)
    assert code == 3
    assert counts["files_failed"] == [str(rejected.resolve())]
    assert support.file_state(rejected) == ("failed", 0)
    assert support.file_state(fine) == ("complete", fine.stat().st_size)
    committed = {path for path, _ in support.event_pairs()}
    assert str(rejected.resolve()) not in committed
    assert str(fine.resolve()) in committed
