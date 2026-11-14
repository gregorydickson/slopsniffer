"""Signal test helpers: the synthetic signals corpus ingested through every lane, and the
``signals --json`` report it produces.

Every fixture line a test names carries a top-level ``"fx"`` label; :func:`labelled` maps
labels to the event ids the engine derives from the line's file and byte offset.
"""

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from slopsniffer.cli import main
from slopsniffer.contracts import SignalsReport
from slopsniffer.ingest import claude, codex, droid, engine, pickle, skills
from slopsniffer.ingest.reader import event_id
from tests.helpers import CLI_SCHEMAS_DIR, REPO_ROOT, load_schema, schema_registry
from tests.ingest.support import query

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "signals"
CODEX_SESSION = "0000aaaa-0000-4000-8000-000000000001"
RUN_STATE = "run_state.json"
# Pickle runs whose logs were last written long before any orphan_hours a test configures.
STALE_RUNS = ("stale-run", "idle-run")
STALE_SECONDS = 10 * 365 * 24 * 3600

type Evidence = tuple[str, frozenset[str]]


def corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Path:
    """Copy the corpus to tmp, register every lane on it and ingest it."""
    root = tmp_path / "signals-corpus"
    shutil.copytree(FIXTURES, root)
    for run in (root / "pickle").iterdir():
        shutil.copyfile(run / RUN_STATE, run / pickle.STATE)
        stale = time.time() - STALE_SECONDS if run.name in STALE_RUNS else time.time()
        for log in run.glob("*.log"):
            os.utime(log, (stale, stale))
    sources = {
        claude.HARNESS: claude.source(root / "claude"),
        codex.HARNESS: codex.source(root / "codex"),
        droid.HARNESS: droid.source(root / "droid"),
        pickle.HARNESS: pickle.source([root / "pickle"]),
        skills.HARNESS: skills.source([root / "skills" / "global"], []),
    }
    for harness, source in sources.items():
        monkeypatch.setitem(engine.REGISTRY, harness, source)
    code = main(["ingest", "--harness", ",".join(sources), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0, doc
    return root


def run(capsys: pytest.CaptureFixture[str], *args: str) -> SignalsReport:
    """``signals --json`` with ``args``; the document must match the schema."""
    code = main(["signals", "--json", *args])
    doc: SignalsReport = json.loads(capsys.readouterr().out)
    assert code == 0, doc
    schema = load_schema(CLI_SCHEMAS_DIR / "signals.schema.json")
    Draft202012Validator(schema, registry=schema_registry()).validate(doc)
    return doc


def evidence(report: SignalsReport, kind: str) -> set[Evidence]:
    return {
        (s["session_id"], frozenset(s["evidence_event_ids"]))
        for s in report["signals"]
        if s["kind"] == kind
    }


def subjects(report: SignalsReport, kind: str) -> set[str]:
    return {s["session_id"] for s in report["signals"] if s["kind"] == kind}


def labelled(root: Path) -> dict[str, str]:
    """Event id of every labelled fixture line under ``root``."""
    ids: dict[str, str] = {}
    for path in sorted(p for p in root.rglob("*") if p.suffix in (".jsonl", ".log")):
        offset = 0
        for line in path.read_bytes().splitlines(keepends=True):
            record: Any = json.loads(line)
            if isinstance(record, dict) and "fx" in record:
                ids[record["fx"]] = event_id(str(path.resolve()), offset)
            offset += len(line)
    return ids


def ids(root: Path, *labels: str) -> frozenset[str]:
    found = labelled(root)
    return frozenset(found[label] for label in labels)


def session_ids() -> set[str]:
    """Ingested sessions, so a negative assertion cannot pass on a fixture that never loaded."""
    return {row[0] for row in query("SELECT session_id FROM sessions")}
