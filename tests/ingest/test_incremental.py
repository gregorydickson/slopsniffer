import os
from pathlib import Path

import pytest

from slopsniffer.contracts import HarnessIngest
from slopsniffer.ingest.reader import HEAD_BYTES
from tests.ingest import support
from tests.ingest.parsers import MessageEchoParser


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = support.corpus(tmp_path)
    support.register(monkeypatch, root)
    return root


def corpus_starts(root: Path) -> set[tuple[str, int]]:
    return set().union(*(support.line_starts(p) for p in root.rglob("*.jsonl")))


def padded_lines(count: int, width: int = 1024) -> bytes:
    """``count`` JSON lines of exactly ``width`` bytes each."""
    lines = []
    for i in range(count):
        head = f'{{"type":"pad","n":{i},"pad":"'
        lines.append(head + "x" * (width - len(head) - 3) + '"}\n')
    return "".join(lines).encode()


def test_unchanged_files_are_skipped_and_keep_their_event_ids(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    support.ingest(capsys)
    before = support.event_ids()
    code, counts = support.ingest(capsys)
    assert code == 0
    assert support.event_ids() == before
    assert nonzero_without_seen(counts) == {"files_skipped_unchanged"}
    assert (
        counts["files_skipped_unchanged"]
        == counts["files_seen"]
        == len(list(root.rglob("*.jsonl")))
    )


def nonzero_without_seen(counts: HarnessIngest) -> set[str]:
    return support.nonzero(counts) - {"files_seen"}


def test_appended_file_adds_exactly_the_new_lines_events(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    support.ingest(capsys)
    before = support.event_ids()
    target = root / "lossless.jsonl"
    old_starts = support.line_starts(target)
    with target.open("ab") as f:
        f.write(support.jsonl({"type": "appended", "n": 1}, {"type": "appended", "n": 2}))
    added = support.line_starts(target) - old_starts

    code, counts = support.ingest(capsys)
    assert code == 0
    assert support.event_ids() == before | support.ids_of(added)
    assert nonzero_without_seen(counts) == {
        "files_skipped_unchanged",
        "files_resumed",
        "events_written",
    }


def test_rewritten_file_replaces_its_events_and_owned_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = support.corpus(tmp_path)
    support.register(monkeypatch, root, MessageEchoParser())
    target = root / "lossless.jsonl"
    target.write_bytes(support.jsonl({"type": "v1"}) + b"not json\n" + support.jsonl({"n": 1}))
    support.ingest(capsys)
    assert support.error_offsets(target) != set()

    target.write_bytes(support.jsonl(*({"type": "v2", "n": n} for n in range(5))))
    code, counts = support.ingest(capsys)
    assert code == 0
    assert set(support.event_pairs()) == corpus_starts(root)
    assert support.error_offsets(target) == set()
    messages = {row[0] for row in support.query("SELECT event_id FROM messages")}
    assert messages == support.event_ids()
    assert nonzero_without_seen(counts) == {
        "files_skipped_unchanged",
        "files_reingested",
        "events_written",
    }


def test_shrunk_file_with_intact_head_is_reingested(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = root / "big.jsonl"
    data = padded_lines(HEAD_BYTES // 1024 * 2)
    target.write_bytes(data)
    support.ingest(capsys)

    target.write_bytes(data[: HEAD_BYTES + 1024 * 4])
    _, counts = support.ingest(capsys)
    assert set(support.event_pairs()) == corpus_starts(root)
    assert "files_reingested" in support.nonzero(counts)


def test_small_file_grown_past_the_head_window_resumes(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = root / "lossless.jsonl"
    support.ingest(capsys)
    before = support.event_ids()
    with target.open("ab") as f:
        f.write(padded_lines(HEAD_BYTES // 1024 + 1))
    _, counts = support.ingest(capsys)
    assert nonzero_without_seen(counts) == {
        "files_skipped_unchanged",
        "files_resumed",
        "events_written",
    }
    assert before < support.event_ids()
    assert set(support.event_pairs()) == corpus_starts(root)


def test_empty(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = root / "empty.jsonl"
    empty.write_bytes(b"")
    code, counts = support.ingest(capsys)
    assert code == 0
    assert counts["files_failed"] == []
    assert support.file_state(empty) == ("complete", 0)
    assert {path for path, _ in support.event_pairs()} == {
        str(p.resolve()) for p in root.rglob("*.jsonl") if p != empty
    }
    assert support.error_offsets(empty) == set()


def test_since_skips_files_last_modified_before_the_date(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = root / "lossless.jsonl"
    os.utime(old, (946684800, 946684800))  # 2000-01-01
    support.ingest(capsys, "--since", "2020-01-01")
    paths = {row[0] for row in support.query("SELECT path FROM source_files")}
    assert paths == {str(p.resolve()) for p in root.rglob("*.jsonl") if p != old}
