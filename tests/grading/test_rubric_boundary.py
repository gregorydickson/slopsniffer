"""Frontmatter contracts using synthetic rubric data."""

import hashlib
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

import pytest

from slopsniffer.grading import rubrics
from tests.helpers import REPO_ROOT

FIXTURE = REPO_ROOT / "tests/fixtures/grading/synthetic-rubric.md"


def test_rubric_loader_preserves_all_fields_under_alternate_text_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_utf8_text_defaults: None,
) -> None:
    root = tmp_path / "package"
    (root / "rubrics").mkdir(parents=True)
    text = FIXTURE.read_text("utf-8")
    (root / "rubrics/verbosity.md").write_text(text, "utf-8")

    class StrictPackage:
        def joinpath(self, *parts: str) -> Traversable:
            path = root
            for part in parts:
                children = {p.name: p for p in path.iterdir()}
                if part not in children:
                    raise FileNotFoundError(part)
                path = children[part]
            return path

    def files(anchor: str | None = None) -> StrictPackage:
        if anchor not in (None, "slopsniffer.grading"):
            raise ModuleNotFoundError(anchor)
        return StrictPackage()

    monkeypatch.setattr(resources, "files", files)
    parsed = rubrics.load_rubric("verbosity")
    assert (parsed.name, parsed.title, parsed.description, parsed.body, parsed.sha256) == (
        "verbosity",
        "Synthetic Rubric",
        "Grading café: retain exact text.",
        "Body café.\n\nKeep every newline.\n",
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    assert [(label.value, label.description, label.score) for label in parsed.labels] == [
        ("good", "Fine: clear", 1.0),
        ("bad", "Needs a fix.", 0.25),
    ]
    assert parsed.label_values == {"good", "bad"}
    with pytest.raises(rubrics.RubricError):
        rubrics.parse_rubric("verbosity", text.replace("    score: 0.25", '    score: "bad"'))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("labels:\n", ""),
        ("extra: Plain metadata", "extra: Plain metadata\n    score: 0.75"),
        ("extra: Plain metadata", "extra: Plain metadata\n extra: Unexpected"),
        ('description: "Grading café: retain exact text."\n', ""),
        ("name: Synthetic Rubric\n", ""),
        ("name: Synthetic Rubric", "name: 7"),
        ('  - value: "good"\n', "    value: good\n"),
        ("    description: Fine: clear", "    description: 7"),
        ('  - value: "good"', "  - value: 7"),
        ("    score: 1", "    score 1"),
        ("name: Synthetic Rubric", 'name: "unterminated'),
        ("name: Synthetic Rubric", "name: 'quoted'"),
        ("name: Synthetic Rubric", "name: {map}"),
        ("name: Synthetic Rubric", "name: |"),
        ("name: Synthetic Rubric", "name: >"),
        ("name: Synthetic Rubric", "name: #comment"),
        ("name: Synthetic Rubric", "name: "),
    ],
)
def test_malformed_synthetic_frontmatter_is_rejected(old: str, new: str) -> None:
    text = FIXTURE.read_text("utf-8")
    changed = text.replace(old, new, 1)
    with pytest.raises(rubrics.RubricError):
        rubrics.parse_rubric("verbosity", changed)


@pytest.mark.parametrize(
    "text",
    [
        "---\n---\nbody",
        "---\nname: Unclosed",
        "",
        "---\nname: Empty\ndescription: Empty\n---\n",
        "---\n  - value: good\n    description: Fine\n    score: 1\n"
        "name: Synthetic\ndescription: Fine\n---\nbody",
    ],
)
def test_incomplete_frontmatter_is_rejected(text: str) -> None:
    with pytest.raises(rubrics.RubricError):
        rubrics.parse_rubric("verbosity", text)
