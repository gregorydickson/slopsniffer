"""Proposal files under ``<data_dir>/proposals/<skill>/``, and nowhere else.

Every path written is resolved and checked first: it must sit inside the proposals directory of
the data dir and must not be any real instruction file the run read. A failed check raises
:class:`UnsafeTarget` before anything is written.
"""

import difflib
import hashlib
import json
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

PROPOSALS_DIR = "proposals"
SKILL_FILE = "SKILL.md"
DIFF_FILE = "change.diff"
EVIDENCE_FILE = "evidence.json"
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class UnsafeTarget(RuntimeError):
    """A proposal path would leave the data dir's proposals tree or overwrite a real file."""


@dataclass(frozen=True)
class Written:
    path: Path
    diff_path: Path
    evidence_path: Path


def directory_name(surface: str) -> str:
    """``surface`` as one safe path segment: ``ll:review`` → ``ll-review``."""
    name = _UNSAFE_CHARS.sub("-", surface).strip("-.")
    return name or "surface"


class Writer:
    def __init__(self, data_dir: Path, real_paths: Iterable[str]) -> None:
        link = data_dir / PROPOSALS_DIR
        if link.is_symlink() or (link.exists() and not link.is_dir()):
            raise UnsafeTarget(f"{link} is not a plain directory")
        self.root = link.resolve()
        self.real = {Path(p).resolve() for p in real_paths}
        self.used: set[str] = set()

    def check(self, target: Path) -> Path:
        resolved = target.resolve()
        if resolved == self.root or not resolved.is_relative_to(self.root):
            raise UnsafeTarget(f"{target} is outside the proposals directory {self.root}")
        if resolved in self.real:
            raise UnsafeTarget(f"{target} is a real instruction file")
        return resolved

    def reset(self) -> None:
        """Remove the previous run's proposals: the directory is rebuilt every run."""
        inside = sorted(str(p) for p in self.real if p.is_relative_to(self.root))
        if inside:
            raise UnsafeTarget(f"real instruction files inside {self.root}: {', '.join(inside)}")
        if self.root.exists():
            shutil.rmtree(self.root)

    def write(
        self, surface: str, real_path: str, current: str, improved: str, evidence: object
    ) -> Written:
        name = directory_name(surface)
        if name in self.used:
            name = f"{name}-{hashlib.sha256(surface.encode('utf-8')).hexdigest()[:8]}"
        self.used.add(name)
        folder = self.check(self.root / name)
        targets = [self.check(folder / f) for f in (SKILL_FILE, DIFF_FILE, EVIDENCE_FILE)]
        skill, diff, evidence_path = targets
        diff_text = "".join(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                improved.splitlines(keepends=True),
                fromfile=real_path,
                tofile=str(skill),
            )
        )
        folder.mkdir(parents=True, exist_ok=True)
        skill.write_text(improved, "utf-8")
        diff.write_text(diff_text, "utf-8")
        with evidence_path.open("w", encoding="utf-8") as output:
            _write_json(evidence, output)
            output.write("\n")
        return Written(skill, diff, evidence_path)


def _write_json(value: object, output: TextIO) -> None:
    """Stream evidence iterators without constructing a history-sized JSON string."""
    if isinstance(value, Mapping):
        output.write("{")
        separator = ""
        for key in sorted(value):
            output.write(separator + json.dumps(key) + ":")
            _write_json(value[key], output)
            separator = ","
        output.write("}")
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        output.write("[")
        separator = ""
        for item in value:
            output.write(separator)
            _write_json(item, output)
            separator = ","
        output.write("]")
    else:
        output.write(json.dumps(value))
