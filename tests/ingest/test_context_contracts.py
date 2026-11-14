"""Context extraction contracts, exercised through the shipped Claude parser."""

import hashlib
import json
from dataclasses import asdict
from typing import Any

import pytest

from slopsniffer.ingest.claude import ClaudeParser
from slopsniffer.ingest.reader import LineContext
from tests.helpers import REPO_ROOT

CASES = json.loads((REPO_ROOT / "tests/fixtures/ingest/context-contracts.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_context_rows_preserve_content_and_source_identity(case: dict[str, Any]) -> None:
    line = json.dumps(case["record"]).encode() + b"\n"
    ctx = LineContext("/logs/fallback.jsonl", 17, 29, "event-probe")
    parsed = ClaudeParser().parse_line(line, ctx)
    expected = list(case["rows"])
    for ordinal, block in enumerate(case["blocks"]):
        content = block["content"]
        digest = hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()
        expected.extend(
            [
                {"table": "blobs", "values": {"content_hash": digest, "content": content}},
                {
                    "table": "context_blocks",
                    "values": {
                        "event_id": ctx.event_id,
                        "ordinal": ordinal,
                        "kind": block["kind"],
                        "source_path": block["source_path"],
                        "content_hash": digest,
                    },
                },
            ]
        )
    assert [asdict(row) for row in parsed.rows] == expected
