"""Read batches in the parent process; integration tests retain the spawn boundary."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from slopsniffer.ingest import reader
from slopsniffer.ingest.reader import BatchRequest, Event, Row
from tests.helpers import REPO_ROOT
from tests.ingest.parsers import ExplodingParser, MessageEchoParser

FIXTURE = REPO_ROOT / "tests/fixtures/ingest/lossless.jsonl"


def test_reader_supplies_the_exact_line_context(tmp_path: Path) -> None:
    seen: list[tuple[bytes, reader.LineContext]] = []

    class ContextParser:
        def parse_line(self, line: bytes, ctx: reader.LineContext) -> reader.ParsedLine:
            seen.append((line, ctx))
            return reader.ParsedLine(Event("probe", "probe"))

    line = b"probe\n"
    path = tmp_path / "context"
    path.write_bytes(line + line)
    req = BatchRequest(str(path), 17, len(line), len(line) * 2, 100, ContextParser())
    reader.read_batch(req)
    assert seen == [
        (line, reader.LineContext(str(path), 17, len(line), reader.event_id(str(path), len(line))))
    ]


@pytest.mark.parametrize("start", [0, 5, 20])
def test_reader_never_reads_past_a_limit_inside_a_line(tmp_path: Path, start: int) -> None:
    path = tmp_path / "bounded"
    path.write_bytes(b"skip\n" + b'{"type":"second"}\n' * 2)
    req = BatchRequest(str(path), 17, start, 13, 100, MessageEchoParser())
    batch = reader.read_batch(req)
    expected = [(0, 5)] if start == 0 else []
    assert [(offset, length) for _, offset, length, _ in batch.events] == expected
    assert batch.end == (5 if start == 0 else start)
    assert batch.eof and batch.failure is None


def test_byte_cap_is_checked_before_reading_the_next_whole_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = b'{"type":"line"}\n'
    path = tmp_path / "byte-cap"
    path.write_bytes(line + line + line)
    req = BatchRequest(str(path), 17, 0, len(line) * 3, 100, MessageEchoParser())
    for cap, offsets in [(1, [0]), (len(line) + 1, [0, len(line)])]:
        monkeypatch.setattr(reader, "BATCH_BYTES", cap)
        batch = reader.read_batch(req)
        assert [offset for _, offset, _, _ in batch.events] == offsets
        assert batch.end == offsets[-1] + len(line)


def test_reader_batches_resume_without_consuming_partial_lines(tmp_path: Path) -> None:
    lines = FIXTURE.read_bytes().splitlines(keepends=True)
    path = tmp_path / "input.jsonl"
    tail = b'{"type":"unfinished"}'
    path.write_bytes(b"".join(lines) + tail)
    req = BatchRequest(str(path), 17, 0, path.stat().st_size, 2, MessageEchoParser())
    expected = []
    offset = 0
    for line in lines:
        expected.append((offset, len(line), line.decode().rstrip("\r\n")))
        offset += len(line)
    events: list[tuple[str, int, int, Event]] = []
    messages = []
    while True:
        batch = reader.read_batch(req)
        assert batch.failure is None
        assert batch.source_file_id == req.source_file_id
        assert batch.errors == []
        assert [
            (start, length, event.parsed) for _, start, length, event in batch.events
        ] == expected[len(events) : len(events) + req.max_rows]
        events.extend(batch.events)
        messages.extend(batch.rows)
        if batch.eof:
            break
        assert batch.end > req.start
        req = replace(req, start=batch.end)
    assert batch.end == len(path.read_bytes()) - len(tail)
    assert [(start, length, event.parsed) for _, start, length, event in events] == expected
    expected_ids = [
        hashlib.sha256(f"{path}\0{start}".encode()).hexdigest()[:32] for start, _, _ in expected
    ]
    assert [eid for eid, _, _, _ in events] == expected_ids
    assert messages == [
        Row("messages", {"event_id": eid, "role": "user", "text": None}) for eid in expected_ids
    ]


@pytest.mark.parametrize("limit", [0, 1])
def test_reader_respects_snapshot_limit(tmp_path: Path, limit: int) -> None:
    path = tmp_path / "bounded.jsonl"
    line = b'{"type":"good"}\n'
    path.write_bytes(line + line)
    req = BatchRequest(str(path), 17, 0, len(line) * limit, 100, MessageEchoParser())
    batch = reader.read_batch(req)
    assert [(offset, length) for _, offset, length, _ in batch.events] == [(0, len(line))] * limit
    assert (batch.end, batch.eof, batch.failure) == (req.limit, True, None)


def test_reader_byte_cap_yields_a_complete_line_then_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "capped.jsonl"
    line = b'{"type":"good"}\n'
    path.write_bytes(line + line)
    monkeypatch.setattr(reader, "BATCH_BYTES", len(line))
    req = BatchRequest(str(path), 17, 0, path.stat().st_size, 100, MessageEchoParser())
    first = reader.read_batch(req)
    assert [(offset, length) for _, offset, length, _ in first.events] == [(0, len(line))]
    assert (first.end, first.eof, first.failure) == (len(line), False, None)
    second = reader.read_batch(replace(req, start=first.end))
    assert [(offset, length) for _, offset, length, _ in second.events] == [(len(line), len(line))]
    assert (second.end, second.eof, second.failure) == (path.stat().st_size, False, None)


def test_reader_keeps_malformed_events_and_reports_their_offsets(tmp_path: Path) -> None:
    path = tmp_path / "malformed.jsonl"
    good, bad = b'{"type":"good"}\n', b"{bad}\n"
    path.write_bytes(good + bad + good)
    req = BatchRequest(str(path), 17, 0, path.stat().st_size, 100, MessageEchoParser())
    batch = reader.read_batch(req)
    assert [
        (offset, length, event.record_type, event.kind) for _, offset, length, event in batch.events
    ] == [
        (0, len(good), "good", "passthrough"),
        (len(good), len(bad), "malformed", "malformed"),
        (len(good) + len(bad), len(good), "good", "passthrough"),
    ]
    assert batch.events[1][-1] == Event("malformed", "malformed")
    assert [offset for offset, _ in batch.errors] == [len(good)]
    assert all(error.startswith("JSONDecodeError:") for _, error in batch.errors)
    assert batch.rows == [
        Row("messages", {"event_id": eid, "role": "user", "text": None})
        for eid, _, _, event in batch.events
        if event.kind == "passthrough"
    ]
    assert (batch.end, batch.eof, batch.failure) == (path.stat().st_size, True, None)


def test_reader_discards_batch_on_parser_bug_and_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "bug.jsonl"
    good = b'{"type":"good"}\n'
    path.write_bytes(good + b'{"type":"boom"}\n')
    req = BatchRequest(str(path), 17, 0, path.stat().st_size, 100, ExplodingParser())
    batch = reader.read_batch(req)
    assert (batch.source_file_id, batch.end, batch.eof) == (17, 0, False)
    assert (batch.events, batch.rows, batch.errors) == ([], [], [])
    assert batch.failure == "RuntimeError: parser bug"
    path.unlink()
    missing = reader.read_batch(req)
    assert (missing.events, missing.rows, missing.errors) == ([], [], [])
    assert (missing.source_file_id, missing.end, missing.eof) == (17, 0, False)
    assert missing.failure is not None and missing.failure.startswith("FileNotFoundError:")


def test_event_id_hashes_raw_path_bytes_and_offset_with_a_separator() -> None:
    for path, offset in [("/tmp/café", 7), ("/tmp/bad-\udcff", 70), ("/tmp/café7", 0)]:
        key = path.encode("utf-8", "surrogateescape") + b"\0" + str(offset).encode()
        assert reader.event_id(path, offset) == hashlib.sha256(key).hexdigest()[:32]


@pytest.mark.parametrize("size", [0, 3, reader.HEAD_BYTES, reader.HEAD_BYTES + 17])
def test_head_hash_obeys_snapshot_and_prefix_cap(tmp_path: Path, size: int) -> None:
    path = tmp_path / "head"
    content = b"a" * reader.HEAD_BYTES + b"tail"
    path.write_bytes(content)
    assert (
        reader.head_sha256(path, size)
        == hashlib.sha256(content[: min(size, reader.HEAD_BYTES)]).hexdigest()
    )


def test_worker_protocol_answers_requests_and_stops_on_sentinel(tmp_path: Path) -> None:
    import multiprocessing
    import signal

    path = tmp_path / "worker.jsonl"
    line = b'{"type":"worker"}\n'
    path.write_bytes(line)
    parent, child = multiprocessing.Pipe()
    previous = signal.getsignal(signal.SIGINT)
    req = BatchRequest(str(path), 17, 0, len(line), 100, MessageEchoParser())
    try:
        parent.send(req)
        parent.send(None)
        reader.serve(child)
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        assert parent.poll()
        batch = parent.recv()
        assert batch == reader.Batch(
            source_file_id=17,
            end=len(line),
            eof=True,
            events=[
                (
                    reader.event_id(str(path), 0),
                    0,
                    len(line),
                    Event("worker", "passthrough", parsed=line.decode().rstrip("\n")),
                )
            ],
            rows=[
                Row(
                    "messages",
                    {
                        "event_id": reader.event_id(str(path), 0),
                        "role": "user",
                        "text": None,
                    },
                )
            ],
        )
        assert not parent.poll()
    finally:
        signal.signal(signal.SIGINT, previous)
        parent.close()
        child.close()


@pytest.mark.parametrize("closed", ["parent", "child"])
def test_worker_protocol_handles_closed_pipes(closed: str) -> None:
    import multiprocessing
    import signal

    parent, child = multiprocessing.Pipe()
    previous = signal.getsignal(signal.SIGINT)
    try:
        (parent if closed == "parent" else child).close()
        reader.serve(child)
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, previous)
        parent.close()
        child.close()
