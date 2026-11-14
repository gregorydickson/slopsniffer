import hashlib
import json
import shutil
from pathlib import Path

import pytest

from slopsniffer.cli import main
from slopsniffer.ingest import claude, engine, skills
from tests.helpers import REPO_ROOT
from tests.ingest import support

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "skills"
EXPECTED = {
    ("alpha", "global"),
    ("bare", "global"),
    ("folded", "global"),
    ("linked", "global"),
    ("pl:tool", "plugin"),
    ("proj-claude", "project"),
    ("proj-agents", "project"),
    ("proj-codex", "project"),
}


def _transcript(path: Path, session: str, cwd: str) -> None:
    record = {
        "type": "user",
        "sessionId": session,
        "uuid": f"{session}-u1",
        "cwd": cwd,
        "timestamp": "2026-11-09T10:00:00.000Z",
        "message": {"role": "user", "content": "hi"},
    }
    path.write_text(json.dumps(record) + "\n")


def _ingest(harness: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ingest", "--harness", harness, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["harnesses"][harness]["files_failed"] == []


@pytest.fixture
def tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Path:
    root = tmp_path / "skills"
    shutil.copytree(FIXTURES, root)
    (root / "repo" / ".git").mkdir()
    linked = tmp_path / "elsewhere" / "linked"
    linked.mkdir(parents=True)
    (linked / skills.SKILL_FILE).write_text("---\nname: linked\n---\n")
    (root / "global" / "linked").symlink_to(linked)
    (root / "global" / "alpha" / "loop").symlink_to(root / "global")
    (root / "global" / "dangling").mkdir()
    (root / "global" / "dangling" / skills.SKILL_FILE).symlink_to(tmp_path / "gone" / "SKILL.md")
    projects = tmp_path / "projects" / "-repo"
    projects.mkdir(parents=True)
    _transcript(projects / "s-in-repo.jsonl", "s-in-repo", str(root / "repo" / "src"))
    _transcript(projects / "s-gone.jsonl", "s-gone", str(tmp_path / "no-such-dir"))
    monkeypatch.setitem(engine.REGISTRY, claude.HARNESS, claude.source(projects.parent))
    monkeypatch.setitem(
        engine.REGISTRY, skills.HARNESS, skills.source([root / "global"], [root / "plugins"])
    )
    _ingest(claude.HARNESS, capsys)
    _ingest(skills.HARNESS, capsys)
    return root


def test_name_scope_set_spans_global_plugin_and_project(tree: Path) -> None:
    rows = support.query("SELECT name, scope FROM skills")
    assert set(rows) == EXPECTED
    names = {name for name, _ in rows}
    assert {"old", "stray", "pl:stray"}.isdisjoint(names)


def test_skill_without_frontmatter_has_null_description(tree: Path) -> None:
    rows = dict(support.query("SELECT name, description FROM skills"))
    assert rows["bare"] is None
    assert (rows["alpha"], rows["folded"]) == ("Alpha does things.", "Folded across two lines.")


def test_rows_carry_path_hash_and_repo(tree: Path) -> None:
    rows = support.query("SELECT name, path, content_hash, repo FROM skills")
    for name, path, digest, repo in rows:
        assert digest == hashlib.sha256(Path(path).read_bytes()).hexdigest()
        assert repo == (str(tree / "repo") if name.startswith("proj-") else None)
    assert len({path for _, path, _, _ in rows}) == len(rows)


def test_reingest_rebuilds_the_inventory(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.rmtree(tree / "global" / "folded")
    _ingest(skills.HARNESS, capsys)
    assert set(support.query("SELECT name, scope FROM skills")) == EXPECTED - {("folded", "global")}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("no frontmatter\n", {}),
        ("---\nname: x\n", {}),
        ("---\nname: 'q'\ndescription: |\n  a\n  b\n---\n", {"name": "q", "description": "a\nb"}),
    ],
)
def test_frontmatter_shapes(text: str, expected: dict[str, str]) -> None:
    assert skills.frontmatter(text) == expected


def test_a_session_in_a_home_without_git_keeps_global_skills_global(tmp_path: Path) -> None:
    global_root = tmp_path / ".claude" / "skills"
    shutil.copytree(FIXTURES / "global" / "alpha", global_root / "alpha")
    found = skills.inventory([global_root], [], [str(tmp_path)])
    assert {(s.name, s.scope, s.repo) for s in found} == {("alpha", "global", None)}
