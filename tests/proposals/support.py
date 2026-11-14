"""Proposal test helpers: real skill and rule files copied to tmp, graded sessions seeded through
the grades writer, and fake ``codex``/``claude`` executables on PATH."""

import hashlib
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import duckdb
import pytest

from slopsniffer.config import load_config
from slopsniffer.contracts import Rubric as RubricName
from slopsniffer.db import open_write
from slopsniffer.grading import backends, grader
from slopsniffer.grading.rubrics import load_rubrics
from slopsniffer.ingest import skills
from tests.helpers import REPO_ROOT, fake_bin

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "proposals"
FAKE_GRADER = REPO_ROOT / "tests" / "grading" / "fake_grader.py"
RUBRICS = load_rubrics()


@dataclass(frozen=True)
class Grade:
    """One ``grades`` row as the grader stores it; ``verdict`` None is an error row."""

    rubric: RubricName
    verdict: grader.SessionVerdict | None
    stale: bool = False


def failing(rubric: RubricName, label: str, reason: str, cited: Sequence[str] = ()) -> Grade:
    score = next(lb.score for lb in RUBRICS[rubric].labels if lb.value == label)
    return Grade(rubric, grader.SessionVerdict(label, score, reason, list(cited)))


@dataclass(frozen=True)
class Session:
    session_id: str
    grades: tuple[Grade, ...]
    skills: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    cwd: str | None = None


def invocation_event(session_id: str, skill: str) -> str:
    return f"{session_id}-skill-{skill}"


@dataclass(frozen=True)
class Home:
    root: Path
    skill_root: Path
    rule_file: Path

    def skill_file(self, name: str) -> Path:
        return self.skill_root / name / "SKILL.md"

    def real_files(self) -> list[Path]:
        return sorted(p for p in self.root.rglob("*") if p.is_file())

    def checksums(self) -> dict[Path, str]:
        return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.real_files()}


def real_home(tmp_path: Path) -> Home:
    root = tmp_path / "real"
    shutil.copytree(FIXTURES, root)
    return real_home_at(root)


def real_home_at(root: Path) -> Home:
    return Home(root, root / "skills", root / "rules" / "CLAUDE.md")


def seed(home: Home, sessions: Sequence[Session]) -> None:
    """Sessions, their skill invocations and CLAUDE.md blocks, grades and the skill inventory."""
    config = load_config()
    with open_write(config) as con:
        for s in sessions:
            con.execute(
                "INSERT INTO sessions (session_id, harness, cwd) VALUES (?, 'claude', ?)",
                [s.session_id, s.cwd],
            )
            for offset, skill in enumerate(s.skills):
                event_id = invocation_event(s.session_id, skill)
                _event(con, event_id, s.session_id, offset)
                con.execute(
                    "INSERT INTO skill_invocations VALUES (?, ?, ?, 'skill_tool', NULL)",
                    [event_id, s.session_id, skill],
                )
            for ordinal, rule in enumerate(s.rules):
                event_id = f"{s.session_id}-rules-{ordinal}"
                _event(con, event_id, s.session_id, 100 + ordinal)
                con.execute(
                    "INSERT INTO context_blocks VALUES (?, 0, 'claude_md', ?, ?)",
                    [event_id, rule, hashlib.sha256(rule.encode()).hexdigest()],
                )
            for g in s.grades:
                rubric = RUBRICS[g.rubric]
                if g.stale:
                    rubric = replace(rubric, sha256="0" * 64)
                grader._store_grade(con, s.session_id, rubric, g.verdict)
        inventory_skills(con, home)


def inventory_skills(con: duckdb.DuckDBPyConnection, home: Home) -> None:
    """The ``skills`` table rebuilt by the skills lane from the copied skill root."""
    finalize = skills.source([home.skill_root], []).finalize
    assert finalize is not None
    finalize(con, load_config())


def _event(con: duckdb.DuckDBPyConnection, event_id: str, session_id: str, offset: int) -> None:
    con.execute(
        "INSERT INTO events (event_id, session_id, seq, record_type, kind, source_file_id, "
        "byte_offset, byte_length) VALUES (?, ?, ?, 'assistant', 'tool_use', 1, ?, 1)",
        [event_id, session_id, offset, offset],
    )


@dataclass(frozen=True)
class Fakes:
    scenario_path: Path
    log_path: Path

    def scenario(self, **behaviours: object) -> None:
        self.scenario_path.write_text(json.dumps(behaviours))

    def calls(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]


def install_fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    # A hang guard only; deadline behavior is covered by test_backend_boundary.py.
    monkeypatch.setattr(backends, "CALL_TIMEOUT_S", 30)
    dirs = [str(fake_bin(name, FAKE_GRADER, name)) for name in (backends.CODEX, backends.CLAUDE)]
    monkeypatch.setenv("PATH", os.pathsep.join([*dirs, os.environ["PATH"]]))
    fakes = Fakes(tmp_path / "scenario.json", tmp_path / "calls.jsonl")
    monkeypatch.setenv("FAKE_GRADER_SCENARIO", str(fakes.scenario_path))
    monkeypatch.setenv("FAKE_GRADER_LOG", str(fakes.log_path))
    fakes.scenario(codex=["ok"], claude=["ok"])
    return fakes
