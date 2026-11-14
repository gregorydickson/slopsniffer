"""Skill inventory: every installed ``SKILL.md``, the denominator for never-fired detection.

Scopes: ``global`` (each configured skill root), ``plugin`` (``skills/`` trees under each
configured plugin root, named ``<plugin>:<skill>`` when an enclosing
``.claude-plugin/plugin.json`` names the plugin) and ``project`` (``.claude/skills``,
``.agents/skills`` and ``.codex/skills`` of each repository a recorded session ran in; the
repository is the nearest ancestor of ``sessions.cwd`` holding ``.git``, else the cwd).

The lane reads no lines, so it discovers no source files; :func:`finalize` rebuilds the
whole ``skills`` table from disk. Walks follow symlinked directories once each and skip
hidden directories below the walked root.
"""

import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

from slopsniffer.config import Config
from slopsniffer.contracts import Harness
from slopsniffer.ingest import engine
from slopsniffer.ingest.reader import LineContext, ParsedLine

HARNESS: Harness = "skills"
SKILL_FILE = "SKILL.md"
PROJECT_SKILL_DIRS = (".claude/skills", ".agents/skills", ".codex/skills")
PLUGIN_MANIFEST = Path(".claude-plugin") / "plugin.json"
BLOCK_SCALARS = {">": " ", ">-": " ", "|": "\n", "|-": "\n"}


@dataclass(frozen=True)
class Skill:
    path: str
    name: str
    scope: str
    description: str | None
    content_hash: str
    repo: str | None


@dataclass(frozen=True)
class _NoLines:
    """The lane has no line sources; the engine never calls this."""

    def parse_line(self, line: bytes, ctx: LineContext) -> ParsedLine:
        raise ValueError(f"the {HARNESS} lane reads no lines ({ctx.source_path})")


def frontmatter(text: str) -> dict[str, str]:
    """Top-level ``key: value`` pairs of a leading ``---`` block; block scalars are joined."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return {}
    fields: dict[str, str] = {}
    body = lines[1:end]
    i = 0
    while i < len(body):
        key, sep, value = body[i].partition(":")
        i += 1
        if not sep or not key or key[0].isspace() or key.startswith("#"):
            continue
        value = value.strip()
        if value in BLOCK_SCALARS:
            block = []
            while i < len(body) and (not body[i].strip() or body[i][0].isspace()):
                block.append(body[i].strip())
                i += 1
            value = BLOCK_SCALARS[value].join(part for part in block if part)
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        fields[key.strip()] = value
    return fields


def skill_files(root: Path) -> Iterator[Path]:
    """``SKILL.md`` files under ``root``, following each symlinked directory once."""
    root = root.expanduser()
    if root.is_file():
        if root.name == SKILL_FILE:
            yield root
        return
    seen: set[str] = set()
    for current, dirs, files in os.walk(root, followlinks=True):
        real = os.path.realpath(current)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        if SKILL_FILE in files:
            yield Path(current) / SKILL_FILE


def _plugin_name(skill_dir: Path, root: Path) -> str | None:
    for parent in skill_dir.parents:
        if parent == root or root not in parent.parents:
            return None
        try:
            name = json.loads((parent / PLUGIN_MANIFEST).read_text("utf-8")).get("name")
        except OSError, ValueError, AttributeError:
            continue
        if isinstance(name, str) and name:
            return name
    return None


def _skill(path: Path, scope: str, repo: Path | None, namespace: str | None = None) -> Skill | None:
    """The skill at ``path``; None when it cannot be read (a dangling symlink, say)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    fields = frontmatter(data.decode("utf-8", "replace"))
    name = fields.get("name") or path.parent.name
    return Skill(
        str(path),
        f"{namespace}:{name}" if namespace else name,
        scope,
        fields.get("description") or None,
        hashlib.sha256(data).hexdigest(),
        str(repo) if repo else None,
    )


def repository(cwd: Path) -> Path | None:
    """The nearest ancestor of ``cwd`` (itself included) holding ``.git``, else ``cwd``."""
    if not cwd.is_dir():
        return None
    return next((d for d in (cwd, *cwd.parents) if (d / ".git").exists()), cwd)


def inventory(
    skill_roots: Sequence[Path], plugin_roots: Sequence[Path], cwds: Sequence[str]
) -> list[Skill]:
    """Readable skills by path; a path reached from two scopes keeps the first, so global and
    plugin roots win over a project directory that is the same tree (a session run in ``~``)."""
    found = [_skill(p, "global", None) for root in skill_roots for p in skill_files(root)]
    for root in plugin_roots:
        for path in skill_files(root):
            if root.name == "skills" or "skills" in path.relative_to(root).parts[:-2]:
                found.append(_skill(path, "plugin", None, _plugin_name(path.parent, root)))
    repos = {repo for cwd in cwds if (repo := repository(Path(cwd))) is not None}
    for repo in sorted(repos):
        for sub in PROJECT_SKILL_DIRS:
            found.extend(_skill(p, "project", repo) for p in skill_files(repo / sub))
    by_path: dict[str, Skill] = {}
    for skill in found:
        if skill is not None:
            by_path.setdefault(skill.path, skill)
    return list(by_path.values())


_INSERT = """
INSERT INTO skills (path, name, scope, description, content_hash, repo)
SELECT unnest($1::VARCHAR[]), unnest($2::VARCHAR[]), unnest($3::VARCHAR[]),
       unnest($4::VARCHAR[]), unnest($5::VARCHAR[]), unnest($6::VARCHAR[])
"""


def source(
    skill_roots: Sequence[Path] | None = None, plugin_roots: Sequence[Path] | None = None
) -> engine.HarnessSource:
    """The inventory of the given roots, or of the configured skill and plugin roots."""

    def finalize(con: duckdb.DuckDBPyConnection, config: Config) -> None:
        roots = config.ingest.sources
        cwds = [
            cwd
            for (cwd,) in con.execute(
                "SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL"
            ).fetchall()
        ]
        skills = inventory(
            skill_roots if skill_roots is not None else roots.skill_roots,
            plugin_roots if plugin_roots is not None else roots.plugin_roots,
            cwds,
        )
        with engine.transaction(con):
            con.execute("DELETE FROM skills")
            if skills:
                columns = zip(
                    *(
                        (s.path, s.name, s.scope, s.description, s.content_hash, s.repo)
                        for s in skills
                    ),
                    strict=True,
                )
                con.execute(_INSERT, [list(c) for c in columns])

    return engine.HarnessSource(_NoLines(), lambda config: [], finalize)
