"""CLI test helpers: a fixture home laid out where the packaged config looks for every lane,
fake ``gh``/``codex``/``claude`` executables, and the ``--json`` document of one invocation.

The home is the synthetic signals corpus plus one Claude session that opened :data:`PR`, so a
plain ``run`` exercises every lane, every signal SQL and a GitHub fetch without touching
``engine.REGISTRY``.
"""

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from slopsniffer.cli import main
from slopsniffer.contracts import Harness
from slopsniffer.ingest import pickle
from tests.helpers import CLI_SCHEMAS_DIR, fake_bin, load_schema, schema_registry
from tests.outcomes.support import FAKE_GH, FakeGh, pr_node, short_gh_timeout, write_pr_session
from tests.proposals.support import Fakes, install_fakes
from tests.signals.support import FIXTURES as SIGNALS
from tests.signals.support import RUN_STATE

PR = ("acme/widgets", 1)
# Packaged-config root of each lane, relative to HOME, and the corpus directory copied there.
LANE_ROOTS: dict[Harness, tuple[str, str]] = {
    "claude": (".claude/projects", "claude"),
    "codex": (".codex/sessions", "codex"),
    "droid": (".factory/sessions", "droid"),
    "pickle": (".local/share/pickle-rick/sessions", "pickle"),
    "skills": (".claude/skills", "skills/global"),
}
FILE_LANES: tuple[Harness, ...] = ("claude", "codex", "droid", "pickle")


def fixture_home(home: Path) -> dict[Harness, Path]:
    """Every lane's corpus copied under ``home`` at its packaged-config root."""
    roots: dict[Harness, Path] = {}
    for harness, (relative, corpus) in LANE_ROOTS.items():
        roots[harness] = home / relative
        shutil.copytree(SIGNALS / corpus, roots[harness])
    for run in roots["pickle"].iterdir():
        shutil.copyfile(run / RUN_STATE, run / pickle.STATE)
    write_pr_session(roots["claude"] / "-prs" / "prs.jsonl", [PR])
    return roots


def source_files(roots: dict[Harness, Path]) -> dict[Harness, set[str]]:
    """The files each file-backed lane discovers under ``roots``."""
    suffix = {"claude": ".jsonl", "codex": ".jsonl", "droid": ".jsonl", "pickle": ".log"}
    return {h: {str(p.resolve()) for p in roots[h].rglob(f"*{suffix[h]}")} for h in FILE_LANES}


def install_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    """A fake ``gh`` first on PATH that knows :data:`PR` as merged."""
    short_gh_timeout(monkeypatch)
    bin_dir = fake_bin("gh", FAKE_GH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    fake = FakeGh(tmp_path / "gh-scenario.json", tmp_path / "gh-calls.jsonl")
    monkeypatch.setenv("FAKE_GH_SCENARIO", str(fake.scenario_path))
    monkeypatch.setenv("FAKE_GH_LOG", str(fake.log_path))
    merged = pr_node(PR[1], state="MERGED", merged_at="2026-11-09T12:00:00Z")
    fake.scenario(prs={f"{PR[0]}#{PR[1]}": merged})
    return fake


def install_graders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    work = tmp_path / "graders"
    work.mkdir()
    return install_fakes(work, monkeypatch)


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any]:
    """Exit code and the single JSON document ``argv --json`` printed on stdout."""
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    assert out.count("\n") == 1, out
    return code, json.loads(out)


def validate(schema: str, doc: object) -> None:
    validator = Draft202012Validator(
        load_schema(CLI_SCHEMAS_DIR / f"{schema}.schema.json"), registry=schema_registry()
    )
    validator.validate(doc)
