"""Line-passthrough parser: one JSON value per line, kept verbatim as ``parsed``.

It knows no harness record shapes, so the engine can be exercised without any of them.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from slopsniffer.config import Config
from slopsniffer.ingest.engine import HarnessSource
from slopsniffer.ingest.reader import Event, LineContext, ParsedLine

KIND = "passthrough"


@dataclass(frozen=True)
class PassthroughParser:
    def parse_line(self, line: bytes, ctx: LineContext) -> ParsedLine:
        del ctx
        text = line.decode("utf-8").rstrip("\r\n")
        value = json.loads(text)
        record_type = value.get("type") if isinstance(value, dict) else None
        return ParsedLine(
            Event(record_type if isinstance(record_type, str) else "unknown", KIND, parsed=text)
        )


def source(root: Path) -> HarnessSource:
    """Every ``*.jsonl`` file under ``root``."""

    def discover(config: Config) -> list[Path]:
        del config
        return sorted(root.rglob("*.jsonl"))

    return HarnessSource(PassthroughParser(), discover)
