"""Skill filesystem and frontmatter contracts without CLI setup."""

import hashlib
import json
import shutil
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from slopsniffer.ingest import skills
from slopsniffer.ingest.reader import LineContext
from tests.helpers import REPO_ROOT

CASES = json.loads((REPO_ROOT / "tests/fixtures/ingest/frontmatter-contracts.json").read_text())


@contextmanager
def finite_walk() -> Iterator[None]:
    # A broken increment or visited-directory guard must fail, not hang pytest.
    def expired(signum: int, frame: object) -> None:
        raise RuntimeError("finite skill input did not terminate")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 2)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize(("name", "text", "expected"), CASES, ids=[c[0] for c in CASES])
def test_frontmatter_contract(name: str, text: str, expected: dict[str, str]) -> None:
    with finite_walk():
        assert skills.frontmatter(text) == expected


def test_skill_walk_follows_links_once_and_skips_hidden_children(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    visible = root / "visible"
    visible.mkdir()
    (visible / "SKILL.md").write_text("visible")
    hidden = root / ".hidden"
    hidden.mkdir()
    (hidden / "SKILL.md").write_text("ignored")
    (visible / "loop").symlink_to(root, target_is_directory=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "SKILL.md").write_text("external")
    (root / "linked").symlink_to(external, target_is_directory=True)
    (root / "readme.md").write_text("not a skill")
    with finite_walk():
        assert list(skills.skill_files(root)) == [
            root / "linked/SKILL.md",
            root / "visible/SKILL.md",
        ]
        assert list(skills.skill_files(root / "missing")) == []


def test_repository_uses_nearest_git_marker_and_falls_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Model case-sensitive marker lookup even on a case-insensitive macOS volume.
    original_exists = Path.exists

    def exact_exists(path: Path, *, follow_symlinks: bool = True) -> bool:
        if path.name.casefold() == ".git":
            return any(p.name == path.name for p in path.parent.iterdir())
        return original_exists(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "exists", exact_exists)
    outer = tmp_path / "outer"
    inner = outer / "inner"
    cwd = inner / "src"
    cwd.mkdir(parents=True)
    (outer / ".git").mkdir()
    (inner / ".git").write_text("gitdir: synthetic")
    assert skills.repository(cwd) == inner
    assert skills.repository(inner) == inner
    assert skills.repository(tmp_path) == tmp_path
    assert skills.repository(tmp_path / "missing") is None
    assert skills.repository(inner / ".git") is None


def test_plugin_namespace_stops_at_root_and_skips_invalid_manifests(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    skill_dir = root / "plug/version/skills/demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n")
    outside = tmp_path / ".claude-plugin"
    outside.mkdir()
    (outside / "plugin.json").write_text('{"name":"outside"}')
    # A manifest at the walked root is outside the individual plugin namespace.
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin/plugin.json").write_text('{"name":"root"}')
    assert {s.name for s in skills.inventory([], [root], [])} == {"demo"}
    manifest = root / "plug/.claude-plugin/plugin.json"
    manifest.parent.mkdir()
    for value, expected in [
        ("not json", "demo"),
        ("[]", "demo"),
        ('{"name":3}', "demo"),
        ('{"name":""}', "demo"),
        ('{"name":"plug"}', "plug:demo"),
    ]:
        manifest.write_text(value)
        assert {s.name for s in skills.inventory([], [root], [])} == {expected}


def test_inventory_deduplicates_scopes_and_ignores_unreadable_skills(tmp_path: Path) -> None:
    root = tmp_path / ".claude/skills"
    shutil.copytree(REPO_ROOT / "tests/fixtures/skills/global/alpha", root / "alpha")
    (root / "dangling").mkdir()
    (root / "dangling/SKILL.md").symlink_to(tmp_path / "missing")
    assert {
        (s.name, s.scope, s.repo)
        for s in skills.inventory([root, root], [], [str(tmp_path), str(tmp_path / "missing")])
    } == {("alpha", "global", None)}


def test_skill_lane_refuses_line_input() -> None:
    source = skills.source([], [])
    ctx = LineContext("/synthetic/SKILL.md", 17, 29, "event-probe")
    with pytest.raises(ValueError, match=r"/synthetic/SKILL\.md"):
        source.parser.parse_line(b"ignored", ctx)


def test_inventory_keeps_damaged_utf8_and_uses_the_declared_name(tmp_path: Path) -> None:
    fixture = REPO_ROOT / "tests/fixtures/ingest/nonutf8-skill.md"
    root = tmp_path / "skills"
    path = root / "damaged/SKILL.md"
    path.parent.mkdir(parents=True)
    shutil.copy(fixture, path)
    absent = root / "dangling/SKILL.md"
    absent.parent.mkdir()
    absent.symlink_to(tmp_path / "gone")
    found = skills.inventory([root], [], [])
    assert {(s.path, s.name, s.description, s.content_hash) for s in found} == {
        (str(path), "damaged-name", "bad \ufffd", hashlib.sha256(fixture.read_bytes()).hexdigest())
    }


def test_single_file_tilde_and_direct_plugin_skill_roots(isolated_home: Path) -> None:
    root = isolated_home / "skills"
    root.mkdir()
    direct = root / "SKILL.md"
    direct.write_text("---\nname: direct\n---\n")
    other = root / "README.md"
    other.write_text("not a skill")
    plugin = isolated_home / "plugin/skills/tool/SKILL.md"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("---\nname: tool\n---\n")
    assert {
        (s.name, s.scope)
        for s in skills.inventory([Path("~/skills/SKILL.md"), other], [plugin.parent.parent], [])
    } == {("direct", "global"), ("tool", "plugin")}
