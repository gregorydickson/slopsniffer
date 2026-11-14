"""Shared fake-backend environment for grading boundary tests."""

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pytest

from slopsniffer.cli import main
from slopsniffer.config import load_config
from slopsniffer.db import open_write
from slopsniffer.grading import backends
from slopsniffer.grading.render import render_chunks
from slopsniffer.grading.rubrics import RUBRIC_NAMES, load_rubrics
from tests.helpers import fake_bin

FAKE_GRADER = Path(__file__).with_name("fake_grader.py")
RUBRICS = load_rubrics()
LONG = "The agent edited the parser, reran the suite and reported the failing case. " * 2
# Worst first: the selection ranks by signal severity, so this is also the grading order.
SESSIONS: dict[str, list[str | None]] = {
    "s-multi": [LONG, LONG, None, LONG, LONG, LONG],
    "s-single": ["Short session.", None],
}
OK_LABELS = {
    "code-quality": "approve",
    "procedure-compliance": "mostly_compliant",
    "efficiency": "mostly_efficient",
    "verbosity": "verbose",
}

Task = tuple[str, str, tuple[str, ...]]  # (session_id, rubric, chunk event ids)


@dataclass(frozen=True)
class Env:
    scenario_path: Path
    log_path: Path
    capsys: pytest.CaptureFixture[str]

    def scenario(self, **backends_: list[str] | dict[str, str]) -> None:
        self.scenario_path.write_text(json.dumps(backends_))

    def calls(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def grade(self, *extra: str) -> tuple[int, dict[str, Any], str]:
        self.capsys.readouterr()
        code = main(["grade", "--worst", str(len(SESSIONS)), *extra, "--json"])
        out, err = self.capsys.readouterr()
        summary: dict[str, Any] = json.loads(out)
        return code, summary, err


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Env:
    config = tmp_path / "config.toml"
    config.write_text("[grading]\nchunk_tokens = 60\nchunk_overlap = 10\n")
    monkeypatch.setenv("SLOPSNIFFER_CONFIG", str(config))
    # A hang guard only; deadline behavior is covered by test_backend_boundary.py.
    monkeypatch.setattr(backends, "CALL_TIMEOUT_S", 30)
    dirs = [str(fake_bin(name, FAKE_GRADER, name)) for name in (backends.CODEX, backends.CLAUDE)]
    monkeypatch.setenv("PATH", os.pathsep.join([*dirs, os.environ["PATH"]]))
    fake = Env(tmp_path / "scenario.json", tmp_path / "calls.jsonl", capsys)
    monkeypatch.setenv("FAKE_GRADER_SCENARIO", str(fake.scenario_path))
    monkeypatch.setenv("FAKE_GRADER_LOG", str(fake.log_path))
    seed(SESSIONS)
    return fake


def seed(sessions: dict[str, list[str | None]]) -> None:
    """Sessions with message events (NULL text where None) and a descending signal severity."""
    with open_write(load_config()) as con:
        for rank, (sid, texts) in enumerate(sessions.items()):
            con.execute("INSERT INTO sessions (session_id, harness) VALUES (?, 'claude')", [sid])
            for seq, text in enumerate(texts):
                event_id = f"{sid}-e{seq}"
                con.execute(
                    "INSERT INTO events (event_id, session_id, seq, record_type, kind, "
                    "source_file_id, byte_offset, byte_length) "
                    "VALUES (?, ?, ?, 'user', 'message', 1, ?, 1)",
                    [event_id, sid, seq, seq],
                )
                if text is not None:
                    con.execute("INSERT INTO messages VALUES (?, 'user', ?)", [event_id, text])
            con.execute(
                "INSERT INTO signals VALUES (?, 'rerun_loop', ?, ?, [], NULL, now())",
                [f"sig-{sid}", sid, float(len(sessions) - rank)],
            )


@pytest.fixture
def db() -> Iterator[Callable[[], duckdb.DuckDBPyConnection]]:
    opened: list[duckdb.DuckDBPyConnection] = []

    def connect() -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(str(load_config().db_path), read_only=True)
        opened.append(con)
        return con

    yield connect
    for con in opened:
        con.close()


def ordered_tasks() -> list[Task]:
    """Every (session, rubric, chunk) in grading order: session, then rubric, then chunk."""
    config = load_config()
    con = duckdb.connect(str(config.db_path), read_only=True)
    try:
        chunks = {sid: render_chunks(con, config.grading, sid) for sid in SESSIONS}
    finally:
        con.close()
    return [(sid, r, c.event_ids) for sid in SESSIONS for r in RUBRIC_NAMES for c in chunks[sid]]


def logged_tasks(calls: list[dict[str, Any]]) -> list[Task]:
    return [
        (c["event_ids"][0].rsplit("-e", 1)[0], c["rubric"], tuple(c["event_ids"])) for c in calls
    ]


def grade_rows(con: duckdb.DuckDBPyConnection) -> set[tuple[str, str, str, str | None]]:
    rows = con.execute("SELECT session_id, rubric, status, label FROM grades").fetchall()
    return {(sid, rubric, status, label) for sid, rubric, status, label in rows}


def call_rows(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str, int | None, int | None]]:
    return con.execute(
        "SELECT backend, status, input_tokens, output_tokens FROM grade_calls ORDER BY started_at"
    ).fetchall()


def every_session_rubric() -> set[tuple[str, str]]:
    return {(sid, r) for sid in SESSIONS for r in RUBRIC_NAMES}
