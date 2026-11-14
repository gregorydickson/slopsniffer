"""Claude parser test helpers: a writable copy of the synthetic Claude fixture tree."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from slopsniffer.ingest import claude, engine
from tests.helpers import REPO_ROOT
from tests.ingest import support

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "claude"


def register(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setitem(engine.REGISTRY, claude.HARNESS, claude.source(root))


def fixture_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES, root)
    register(monkeypatch, root)
    return root


def ingest(capsys: pytest.CaptureFixture[str]) -> None:
    code, counts = support.ingest(capsys)
    assert code == 0
    assert counts["files_failed"] == []


def records(*paths: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for p in paths for line in p.read_text().splitlines()]
