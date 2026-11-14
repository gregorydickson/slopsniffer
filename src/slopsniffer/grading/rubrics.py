"""The vendored rubrics: each scorer's label table is read from its frontmatter."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from typing import get_args

from slopsniffer.contracts import Rubric as RubricName

RUBRIC_NAMES: tuple[RubricName, ...] = get_args(RubricName)
SKILL_IMPROVEMENTS = "skill-improvements"
_FENCE = "---"


class RubricError(ValueError):
    """A vendored rubric file does not have the frontmatter this module understands."""


@dataclass(frozen=True)
class Label:
    value: str
    description: str
    score: float


@dataclass(frozen=True)
class Rubric:
    name: RubricName
    title: str
    description: str
    labels: tuple[Label, ...]
    body: str
    sha256: str

    @property
    def label_values(self) -> frozenset[str]:
        return frozenset(label.value for label in self.labels)


def _read(name: str) -> str:
    return (
        resources.files("slopsniffer.grading").joinpath("rubrics", f"{name}.md").read_text("utf-8")
    )


def skill_improvements_text() -> str:
    return _read(SKILL_IMPROVEMENTS)


def load_rubric(name: RubricName) -> Rubric:
    return parse_rubric(name, _read(name))


def parse_rubric(name: RubricName, text: str) -> Rubric:
    front, body = _split(text, name)
    fields, labels = _parse_front(front, name)
    try:
        title, description = fields["name"], fields["description"]
    except KeyError as exc:
        raise RubricError(f"{name}: frontmatter has no {exc.args[0]}") from None
    if not labels:
        raise RubricError(f"{name}: frontmatter has no labels")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Rubric(name, title, description, tuple(labels), body, digest)


def load_rubrics() -> Mapping[RubricName, Rubric]:
    return {name: load_rubric(name) for name in RUBRIC_NAMES}


def _split(text: str, name: str) -> tuple[list[str], str]:
    lines = text.split("\n")
    if lines[0] != _FENCE or _FENCE not in lines[1:]:
        raise RubricError(f"{name}: no frontmatter block")
    end = lines.index(_FENCE, 1)
    return lines[1:end], "\n".join(lines[end + 1 :])


def _scalar(raw: str, name: str) -> str | float:
    """A double-quoted string, a number, or a plain string."""
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise RubricError(f"{name}: unsupported frontmatter value {raw!r}") from None
        if isinstance(value, str):
            return value
        raise RubricError(f"{name}: unsupported frontmatter value {raw!r}")
    try:
        return float(raw)
    except ValueError:
        pass
    if raw.startswith(("[", "{", "'", "|", ">", "#")) or not raw:
        raise RubricError(f"{name}: unsupported frontmatter value {raw!r}")
    return raw


def _parse_front(lines: list[str], name: str) -> tuple[dict[str, str], list[Label]]:
    fields: dict[str, str] = {}
    items: list[dict[str, str | float]] = []
    in_labels = False
    for line in lines:
        if line == "labels:":
            in_labels = True
        elif in_labels and line.startswith("  - "):
            items.append({})
            _assign(items[-1], line[4:], name)
        elif in_labels and line.startswith("    ") and items:
            _assign(items[-1], line[4:], name)
        elif not line.startswith(" ") and ": " in line:
            in_labels = False
            key, raw = line.split(": ", 1)
            value = _scalar(raw, name)
            if not isinstance(value, str):
                raise RubricError(f"{name}: {key} must be a string")
            fields[key] = value
        else:
            raise RubricError(f"{name}: unsupported frontmatter line {line!r}")
    return fields, [_label(item, name) for item in items]


def _assign(item: dict[str, str | float], text: str, name: str) -> None:
    key, sep, raw = text.partition(": ")
    if not sep:
        raise RubricError(f"{name}: unsupported label line {text!r}")
    item[key] = _scalar(raw, name)


def _label(item: dict[str, str | float], name: str) -> Label:
    value, description, score = item.get("value"), item.get("description"), item.get("score")
    if set(item) != {"value", "description", "score"}:
        raise RubricError(f"{name}: a label must set exactly value, description and score")
    if not (
        isinstance(value, str) and isinstance(description, str) and isinstance(score, int | float)
    ):
        raise RubricError(f"{name}: label fields have the wrong types")
    return Label(value, description, float(score))
