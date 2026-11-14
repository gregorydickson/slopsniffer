"""Line-to-rows contracts run in pytest's process so mutmut can measure parser coverage."""

import json
from dataclasses import asdict
from typing import Any

import pytest

from slopsniffer.ingest import claude, codex, droid, passthrough, pickle
from slopsniffer.ingest.reader import LineContext, Parser
from tests.helpers import REPO_ROOT

PARSERS: dict[str, Parser] = {
    "claude": claude.ClaudeParser(),
    "codex": codex.CodexParser(),
    "droid": droid.DroidParser(),
    "passthrough": passthrough.PassthroughParser(),
    "pickle": pickle.PickleParser(),
}
CASES = json.loads((REPO_ROOT / "tests/fixtures/ingest/parser-contracts.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["harness"] + "-" + c["name"])
def test_line_to_rows_contract(case: dict[str, Any]) -> None:
    # Spaces before the terminator are data: only CR/LF may be removed.
    text = json.dumps(case["record"], ensure_ascii=True) + " \t"
    ctx = LineContext(case.get("path", "/logs/fallback.jsonl"), 17, 29, "event-probe")
    parsed = PARSERS[case["harness"]].parse_line(text.encode() + b"\r\n", ctx)
    assert asdict(parsed.event) == {
        **case["event"],
        "parsed": None if case.get("no_parsed") else text,
    }
    assert [asdict(row) for row in parsed.rows] == case["rows"]


@pytest.mark.parametrize("harness", ["claude", "codex", "droid", "passthrough"])
@pytest.mark.parametrize("line", [b"{bad json}\n", b"\xff\n", b'{"type":"probe"}XX\n'])
def test_strict_json_parsers_reject_bad_lines(harness: str, line: bytes) -> None:
    with pytest.raises(ValueError):
        PARSERS[harness].parse_line(line, LineContext("/logs/fallback.jsonl", 17, 29, "probe"))


@pytest.mark.parametrize("line", [b"plain log\n", b"\xff\n", b"false\n", b'{"type":"probe"}XX\n'])
def test_pickle_non_objects_are_text_without_json_rows(line: bytes) -> None:
    parsed = PARSERS["pickle"].parse_line(line, LineContext("/logs/runner.log", 17, 29, "probe"))
    assert asdict(parsed.event) == {
        "record_type": "text",
        "kind": "runner",
        "session_id": None,
        "seq": None,
        "ts": None,
        "parsed": None,
    }
    assert list(parsed.rows) == []
