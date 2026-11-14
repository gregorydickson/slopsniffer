"""``status``: per-harness file states and unknown record types, last runs and DB size."""

import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from slopsniffer.cli import COMMANDS, HARNESSES
from slopsniffer.config import Config, load_config
from slopsniffer.db import SCHEMA_VERSION, open_read
from slopsniffer.status import collect
from tests.cli.support import cli, fixture_home, source_files, validate
from tests.grading.support import memory_db

UNKNOWN_TYPES = ["mystery-a", "mystery-b", "mystery-a"]
WRITERS = {name for name, command in COMMANDS.items() if command.writes}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
def test_file_states_and_unknown_record_types_per_harness(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    roots = fixture_home(isolated_home)
    odd = roots["claude"] / "-odd" / "odd.jsonl"
    odd.parent.mkdir()
    odd.write_text(
        "".join(
            json.dumps({"type": t, "sessionId": "odd", "uuid": f"u{i}"}) + "\n"
            for i, t in enumerate(UNKNOWN_TYPES)
        )
    )
    files = source_files(roots)
    claude = sorted(files["claude"])
    locked, removed = Path(claude[0]), Path(claude[1])
    assert cli(capsys, "ingest")[0] == 0
    removed.unlink()
    # Grown, so the engine must open it again; unreadable, so it fails.
    with locked.open("a") as grown:
        grown.write(json.dumps({"type": "user", "sessionId": "grown"}) + "\n")
    locked.chmod(0)
    try:
        assert cli(capsys, "ingest")[0] == 3
    finally:
        locked.chmod(0o644)

    code, doc = cli(capsys, "status")
    assert code == 0, doc
    validate("status", doc)
    expected_state = {path: "complete" for paths in files.values() for path in paths}
    expected_state[str(locked)] = "failed"
    expected_state[str(removed)] = "missing"
    for harness in HARNESSES:
        if harness == "skills":
            assert "files_by_status" not in doc["harnesses"][harness]
            continue
        mine = Counter(
            state for path, state in expected_state.items() if path in files.get(harness, set())
        )
        assert doc["harnesses"][harness]["files_by_status"] == dict(mine), harness
    unknown = {h: doc["harnesses"][h]["unknown_record_types"] for h in HARNESSES}
    assert unknown == {h: dict(Counter(UNKNOWN_TYPES)) if h == "claude" else {} for h in HARNESSES}


def test_last_runs_name_every_writer_and_record_only_what_ran(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, doc = cli(capsys, "status")
    assert code == 0, doc
    assert doc["last_runs"] == dict.fromkeys(WRITERS)
    assert doc["schema_version"] == SCHEMA_VERSION

    before = datetime.now(UTC)
    assert cli(capsys, "ingest")[0] == 0
    after = datetime.now(UTC)
    code, doc = cli(capsys, "status")
    validate("status", doc)
    ran = {name for name, at in doc["last_runs"].items() if at is not None}
    assert ran == {"ingest"}
    assert set(doc["last_runs"]) == WRITERS
    assert before <= datetime.fromisoformat(doc["last_runs"]["ingest"]) <= after


def test_db_size_is_the_database_file_and_its_wal(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(capsys, "ingest")[0] == 0
    code, doc = cli(capsys, "status")
    assert code == 0, doc
    db = load_config().db_path
    wal = Path(f"{db}.wal")
    assert doc["db_path"] == str(db)
    assert doc["db_size_bytes"] == db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    assert doc["db_size_bytes"] > 0


def test_db_size_adds_the_wal_to_the_database_file(tmp_path: Path) -> None:
    db = tmp_path / "planted.duckdb"
    db.write_bytes(b"d" * 3)
    Path(f"{db}.wal").write_bytes(b"w" * 5)
    config = load_config()
    with open_read(config) as con:
        doc = collect(
            con,
            cast(Config, SimpleNamespace(db_path=db, pricing=config.pricing)),
            HARNESSES,
            WRITERS,
        )
    assert doc["db_size_bytes"] == db.stat().st_size + Path(f"{db}.wal").stat().st_size


def test_skills_status_omits_inapplicable_file_breakdown(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture_home(isolated_home)
    assert cli(capsys, "ingest")[0] == 0
    code, doc = cli(capsys, "status")
    assert code == 0
    assert doc["harnesses"]["skills"] == {"unknown_record_types": {}}
    for harness in set(HARNESSES) - {"skills"}:
        assert "files_by_status" in doc["harnesses"][harness]
    validate("status", doc)


@pytest.mark.parametrize("lane", ["skills", "claude"])
def test_status_schema_rejects_inapplicable_or_missing_file_breakdown(
    lane: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from jsonschema import ValidationError

    code, doc = cli(capsys, "status")
    assert code == 0
    validate("status", doc)
    if lane == "skills":
        doc["harnesses"][lane]["files_by_status"] = {}
    else:
        del doc["harnesses"][lane]["files_by_status"]
    with pytest.raises(ValidationError):
        validate("status", doc)


def test_collect_filters_unrequested_harnesses_and_omits_skill_file_states() -> None:
    files = [
        ("selected", "claude", "complete"),
        ("excluded", "codex", "failed"),
        ("inventory", "skills", "complete"),
    ]
    with memory_db() as con:
        con.executemany("INSERT INTO source_files (path, harness, status) VALUES (?, ?, ?)", files)
        doc = collect(con, load_config(), ["claude", "skills"], [])
    assert doc["harnesses"] == {
        "claude": {
            "files_by_status": dict(Counter(state for _, h, state in files if h == "claude")),
            "unknown_record_types": {},
        },
        "skills": {"unknown_record_types": {}},
    }
