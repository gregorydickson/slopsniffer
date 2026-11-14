"""Helpers for the Codex and Droid parser tests: a writable fixture copy and an ingest run."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from slopsniffer.cli import main
from slopsniffer.contracts import Harness
from slopsniffer.ingest import engine
from tests.helpers import CLI_SCHEMAS_DIR, REPO_ROOT, load_schema, schema_registry

FIXTURES = REPO_ROOT / "tests" / "fixtures"


def fixture_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
    make_source: Callable[[Path], engine.HarnessSource],
) -> Path:
    root = tmp_path / harness
    shutil.copytree(FIXTURES / harness, root)
    monkeypatch.setitem(engine.REGISTRY, harness, make_source(root))
    return root


def ingest(capsys: pytest.CaptureFixture[str], harness: Harness) -> None:
    code = main(["ingest", "--harness", harness, "--json"])
    doc = json.loads(capsys.readouterr().out)
    schema = load_schema(CLI_SCHEMAS_DIR / "ingest.schema.json")
    Draft202012Validator(schema, registry=schema_registry()).validate(doc)
    assert code == 0
    assert doc["harnesses"][harness]["files_failed"] == []


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def all_records(root: Path) -> list[dict[str, Any]]:
    return [r for path in sorted(root.rglob("*.jsonl")) for r in records(path)]
