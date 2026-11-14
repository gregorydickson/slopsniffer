"""Engine test helpers: the passthrough parser registered as a harness, and DB probes."""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from slopsniffer.cli import main
from slopsniffer.config import load_config
from slopsniffer.contracts import Harness, HarnessIngest
from slopsniffer.db import open_read
from slopsniffer.ingest import engine, passthrough
from slopsniffer.ingest.reader import Parser, event_id
from tests.helpers import CLI_SCHEMAS_DIR, REPO_ROOT, load_schema, schema_registry

HARNESS = "claude"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ingest"

# Child-process entry point: registers the passthrough source rooted at argv[1], then runs
# the CLI with the remaining arguments.
CHILD = """
import sys
from pathlib import Path
from slopsniffer.cli import main
from slopsniffer.ingest import engine, passthrough
engine.REGISTRY["claude"] = passthrough.source(Path(sys.argv[1]))
sys.exit(main(sys.argv[2:]))
"""


def corpus(tmp_path: Path) -> Path:
    """A writable copy of the synthetic fixtures."""
    root = tmp_path / "corpus"
    shutil.copytree(FIXTURES, root)
    return root


def register(monkeypatch: pytest.MonkeyPatch, root: Path, parser: Parser | None = None) -> None:
    source = passthrough.source(root)
    if parser is not None:
        source = replace(source, parser=parser)
    monkeypatch.setitem(engine.REGISTRY, cast(Harness, HARNESS), source)


def ingest(capture: pytest.CaptureFixture[Any], *args: str) -> tuple[int, HarnessIngest]:
    code = main(["ingest", "--harness", HARNESS, "--json", *args])
    doc = json.loads(capture.readouterr().out)
    schema = load_schema(CLI_SCHEMAS_DIR / "ingest.schema.json")
    Draft202012Validator(schema, registry=schema_registry()).validate(doc)
    counts: HarnessIngest = doc["harnesses"][HARNESS]
    return code, counts


def nonzero(counts: HarnessIngest) -> set[str]:
    return {key for key, value in counts.items() if value}


def run_child(
    root: Path, data_dir: Path, *args: str, **env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", CHILD, str(root), "ingest", "--harness", HARNESS, "--json", *args],
        env={**_base_env(), "SLOPSNIFFER_HOME": str(data_dir), **env},
        capture_output=True,
        text=True,
        timeout=1800,
        start_new_session=True,
        check=False,
    )


def _base_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != engine.KILL_ENV}


def query(sql: str, params: list[Any] | None = None, data_dir: Path | None = None) -> list[Any]:
    config = load_config()
    if data_dir is not None:
        config = replace(config, data_dir=data_dir)
    with open_read(config) as con:
        return con.execute(sql, params or []).fetchall()


def event_pairs(data_dir: Path | None = None) -> list[tuple[str, int]]:
    """(source path, byte offset) of every event, duplicates kept."""
    rows = query(
        "SELECT f.path, e.byte_offset FROM events e JOIN source_files f USING (source_file_id) "
        "ORDER BY 1, 2",
        data_dir=data_dir,
    )
    return [(path, offset) for path, offset in rows]


def event_ids() -> set[str]:
    return {row[0] for row in query("SELECT event_id FROM events")}


def file_state(path: Path) -> tuple[str, int]:
    rows = query(
        "SELECT status, committed_offset FROM source_files WHERE path = ?", [str(path.resolve())]
    )
    assert len(rows) == 1, rows
    status, offset = rows[0]
    return status, offset


def error_offsets(path: Path) -> set[int | None]:
    rows = query(
        "SELECT byte_offset FROM ingest_errors JOIN source_files USING (source_file_id) "
        "WHERE path = ?",
        [str(path.resolve())],
    )
    return {row[0] for row in rows}


def line_starts(path: Path) -> set[tuple[str, int]]:
    """Offsets of every newline-terminated line in the file."""
    resolved, offset, starts = str(path.resolve()), 0, set()
    for line in path.read_bytes().splitlines(keepends=True):
        if line.endswith(b"\n"):
            starts.add((resolved, offset))
        offset += len(line)
    return starts


def ids_of(pairs: set[tuple[str, int]]) -> set[str]:
    return {event_id(path, offset) for path, offset in pairs}


def jsonl(*records: object) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in records)
