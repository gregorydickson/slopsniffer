"""Discovery honors explicit roots and configured defaults without running a worker."""

import shutil
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import duckdb
import pytest

from slopsniffer.config import Config, load_config
from slopsniffer.db import apply_schema
from slopsniffer.ingest import claude, codex, droid, passthrough, pickle, skills
from tests.helpers import REPO_ROOT


def configured(config: Config, **roots: Any) -> Config:
    return replace(
        config, ingest=replace(config.ingest, sources=replace(config.ingest.sources, **roots))
    )


@pytest.mark.parametrize(
    ("module", "field"),
    [(claude, "claude_projects"), (codex, "codex_sessions"), (droid, "droid_sessions")],
    ids=["claude", "codex", "droid"],
)
def test_jsonl_sources_choose_the_explicit_or_configured_root(
    module: ModuleType, field: str, tmp_path: Path
) -> None:
    first, second = tmp_path / "configured", tmp_path / "explicit"
    for root in (first, second):
        root.mkdir()
        (root / "z.jsonl").write_text("{}\n")
        (root / "nested").mkdir()
        (root / "nested" / "a.jsonl").write_text("{}\n")
        (root / "ignore.txt").write_text("{}\n")
    config = configured(load_config(), **{field: first})
    for source, root in ((module.source(), first), (module.source(second), second)):
        assert list(source.discover(config)) == sorted(
            [
                root / "z.jsonl",
                root / "nested/a.jsonl",
            ]
        )
        assert (
            type(source.parser)
            is {
                "claude_projects": claude.ClaudeParser,
                "codex_sessions": codex.CodexParser,
                "droid_sessions": droid.DroidParser,
            }[field]
        )
        assert source.finalize is module.finalize
    assert list(module.source(tmp_path / "missing").discover(config)) == []


def test_passthrough_source_discovers_only_jsonl(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    keep = root / "keep.jsonl"
    keep.write_text("{}\n")
    (root / "ignore.log").write_text("{}\n")
    source = passthrough.source(root)
    assert list(source.discover(load_config())) == [keep]
    assert source.finalize is None
    assert list(passthrough.source(root / "missing").discover(load_config())) == []


def test_pickle_discovery_distinguishes_default_empty_and_explicit_roots(tmp_path: Path) -> None:
    roots = [tmp_path / "configured", tmp_path / "explicit"]
    for root in roots:
        run = root / "run"
        run.mkdir(parents=True)
        (run / "state.json").write_text("{}")
        (run / "hooks.log").write_text("log\n")
        ticket = run / "ticket"
        ticket.mkdir()
        (ticket / "worker_session_1.log").write_text("log\n")
        (ticket / "nested").mkdir()
        (ticket / "nested/ignore.log").write_text("log\n")
        (run / "ignore.txt").write_text("ignore")
        (root / "not-a-run").mkdir()
        (root / "not-a-run/ignore.log").write_text("log\n")
    config = configured(load_config(), pickle_sessions=(roots[0],))
    for source, root in ((pickle.source(), roots[0]), (pickle.source([roots[1]]), roots[1])):
        assert set(source.discover(config)) == {
            root / "run/hooks.log",
            root / "run/ticket/worker_session_1.log",
        }
    assert list(pickle.source([]).discover(config)) == []
    assert pickle.run_dirs([tmp_path / "gone"]) == {}
    assert pickle.run_dirs([tmp_path / "gone", *roots]) == {"run": (roots[0] / "run").resolve()}


def test_pickle_finalize_rebuilds_empty_and_vanished_runs(tmp_path: Path) -> None:
    root = tmp_path / "pickle"
    run = root / "run"
    run.mkdir(parents=True)
    (run / "state.json").write_text("{}")
    config = configured(load_config(), pickle_sessions=(root,))
    source = pickle.source()
    assert source.finalize is not None
    with duckdb.connect(config={"threads": 1}) as con:
        apply_schema(con)
        source.finalize(con, config)
        assert con.execute(
            "SELECT pickle_run_id, session_dir, active, step, iteration, max_iterations, "
            "current_ticket, start_time_epoch, working_dir, last_log_write_at FROM pickle_runs"
        ).fetchall() == [
            ("run", str(run.resolve()), None, None, None, None, None, None, None, None)
        ]
        assert con.execute("SELECT * FROM pickle_iterations").fetchall() == []
        assert con.execute("SELECT * FROM pickle_session_links").fetchall() == []
        shutil.rmtree(run)
        source.finalize(con, config)
        assert con.execute("SELECT * FROM pickle_runs").fetchall() == []


def test_skills_source_uses_config_and_respects_explicit_empty_roots(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    shutil.copytree(REPO_ROOT / "tests/fixtures/skills/global/alpha", root / "alpha")
    config = configured(load_config(), skill_roots=(root,), plugin_roots=())
    with duckdb.connect(config={"threads": 1}) as con:
        apply_schema(con)
        source = skills.source()
        assert list(source.discover(config)) == []
        assert source.finalize is not None
        source.finalize(con, config)
        assert con.execute("SELECT name, scope, repo FROM skills").fetchall() == [
            ("alpha", "global", None)
        ]
        empty = skills.source([], [])
        assert empty.finalize is not None
        empty.finalize(con, config)
        assert con.execute("SELECT * FROM skills").fetchall() == []
