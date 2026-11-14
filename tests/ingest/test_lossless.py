import base64
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from slopsniffer.cli import main
from slopsniffer.ingest.reader import HEAD_BYTES
from tests.helpers import CLI_SCHEMAS_DIR, load_schema, schema_registry
from tests.ingest import support


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = support.corpus(tmp_path)
    support.register(monkeypatch, root)
    return root


def source_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.jsonl"))


def all_line_starts(root: Path) -> set[tuple[str, int]]:
    return set().union(*(support.line_starts(p) for p in source_files(root)))


def events_with_slices() -> list[tuple[str, str, int, int]]:
    rows = support.query(
        "SELECT e.event_id, f.path, e.byte_offset, e.byte_length "
        "FROM events e JOIN source_files f USING (source_file_id)"
    )
    return [(eid, path, offset, length) for eid, path, offset, length in rows]


def test_rehydrated_bytes_equal_the_source_slice_for_every_event(
    root: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    support.ingest(capsysbinary)
    events = events_with_slices()
    assert {(path, offset) for _, path, offset, _ in events} == all_line_starts(root)
    for eid, path, offset, length in events:
        assert main(["rehydrate", eid]) == 0
        assert capsysbinary.readouterr().out == Path(path).read_bytes()[offset : offset + length]


def test_event_offsets_are_exactly_the_line_starts(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    support.ingest(capsys)
    pairs = support.event_pairs()
    assert len(pairs) == len(set(pairs))
    assert set(pairs) == all_line_starts(root)


def test_event_slices_tile_each_file(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    support.ingest(capsys)
    by_path: dict[str, bytes] = {}
    for _, path, offset, length in sorted(events_with_slices(), key=lambda e: (e[1], e[2])):
        by_path[path] = by_path.get(path, b"") + Path(path).read_bytes()[offset : offset + length]
    assert by_path == {str(p.resolve()): p.read_bytes() for p in source_files(root)}


@pytest.mark.usefixtures("root")
def test_rehydrate_json_carries_the_slice_as_base64(capsys: pytest.CaptureFixture[str]) -> None:
    support.ingest(capsys)
    eid, path, offset, length = events_with_slices()[0]
    assert main(["rehydrate", eid, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    schema = load_schema(CLI_SCHEMAS_DIR / "rehydrate.schema.json")
    Draft202012Validator(schema, registry=schema_registry()).validate(doc)
    assert (doc["event_id"], doc["source_path"], doc["byte_offset"], doc["byte_length"]) == (
        eid,
        path,
        offset,
        length,
    )
    assert base64.b64decode(doc["raw_base64"]) == Path(path).read_bytes()[offset : offset + length]


@pytest.mark.usefixtures("root")
def test_unknown_event_id_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    support.ingest(capsys)
    assert main(["rehydrate", "f" * 32, "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "unknown_event"


def test_missing_source(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    support.ingest(capsys)
    victim = source_files(root)[0]
    before = support.event_pairs()
    victim_events = [e for e in events_with_slices() if e[1] == str(victim.resolve())]
    victim.unlink()

    assert support.ingest(capsys)[0] == 0
    assert support.file_state(victim)[0] == "missing"
    assert support.event_pairs() == before
    assert main(["rehydrate", victim_events[0][0], "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "source_missing"


@pytest.mark.usefixtures("root")
def test_rehydrate_refuses_a_source_whose_head_changed(capsys: pytest.CaptureFixture[str]) -> None:
    support.ingest(capsys)
    eid, path, _, _ = events_with_slices()[0]
    data = Path(path).read_bytes()
    Path(path).write_bytes(b"#" + data[1:])
    assert main(["rehydrate", eid, "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "source_changed"


def test_rehydrate_refuses_an_event_cut_off_past_the_hashed_head(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    big = root / "big.jsonl"
    big.write_bytes(support.jsonl(*({"n": n, "pad": "x" * 100} for n in range(2000))))
    support.ingest(capsys)
    resolved = str(big.resolve())
    events = sorted((e for e in events_with_slices() if e[1] == resolved), key=lambda e: e[2])
    first, last = events[0], events[-1]
    assert last[2] > HEAD_BYTES
    with big.open("r+b") as f:
        f.truncate(last[2])

    assert main(["rehydrate", first[0], "--json"]) == 0
    capsys.readouterr()
    assert main(["rehydrate", last[0], "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "source_changed"
