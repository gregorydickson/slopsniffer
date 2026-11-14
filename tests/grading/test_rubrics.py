import hashlib
import re
from pathlib import Path
from typing import get_args

import pytest

from slopsniffer.contracts import Rubric as RubricName
from slopsniffer.grading import rubrics
from tests.helpers import REPO_ROOT

RUBRICS_DIR = REPO_ROOT / "src" / "slopsniffer" / "grading" / "rubrics"
VALUE = re.compile(r'^\s+- value: "([^"]+)"$', re.M)
SCORE = re.compile(r"^\s+score: ([0-9.]+)$", re.M)
SHA = re.compile(r"\b[0-9a-f]{40}\b")


def frontmatter(path: Path) -> str:
    text = path.read_text("utf-8")
    return text.split("\n---\n", 1)[0]


def valid_text() -> str:
    return (RUBRICS_DIR / "verbosity.md").read_text("utf-8")


def test_parsed_label_sets_equal_the_frontmatter_values() -> None:
    loaded = rubrics.load_rubrics()
    assert set(loaded) == set(get_args(RubricName))
    for name, rubric in loaded.items():
        expected = set(VALUE.findall(frontmatter(RUBRICS_DIR / f"{name}.md")))
        assert expected, name
        assert rubric.label_values == expected


def test_parsed_scores_equal_the_frontmatter_scores() -> None:
    for name, rubric in rubrics.load_rubrics().items():
        front = frontmatter(RUBRICS_DIR / f"{name}.md")
        assert [(label.value, label.score) for label in rubric.labels] == list(
            zip(VALUE.findall(front), map(float, SCORE.findall(front)), strict=True)
        )


def test_vendored_scorers_are_exactly_the_contract_rubrics_plus_skill_improvements() -> None:
    vendored = {p.stem for p in RUBRICS_DIR.glob("*.md")}
    assert vendored == {*get_args(RubricName), rubrics.SKILL_IMPROVEMENTS, "SOURCE"}
    assert rubrics.skill_improvements_text().startswith("# Skill improvement guidelines")


def test_rubric_hash_is_the_sha256_of_the_vendored_file() -> None:
    for name, rubric in rubrics.load_rubrics().items():
        assert (
            rubric.sha256 == hashlib.sha256((RUBRICS_DIR / f"{name}.md").read_bytes()).hexdigest()
        )


def test_source_names_a_40_hex_commit_and_the_license() -> None:
    source = (RUBRICS_DIR / "SOURCE.md").read_text("utf-8")
    assert SHA.search(source)
    assert "MIT" in source
    assert "Permission is hereby granted" in (RUBRICS_DIR / "LICENSE").read_text("utf-8")


def test_editing_a_label_changes_the_hash_and_the_parsed_labels() -> None:
    original = rubrics.parse_rubric("verbosity", valid_text())
    edited = rubrics.parse_rubric(
        "verbosity", valid_text().replace('value: "terse"', 'value: "curt"')
    )
    assert edited.sha256 != original.sha256
    assert edited.label_values == (original.label_values - {"terse"}) | {"curt"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.removeprefix("---\n"),
        lambda t: re.sub(r"^labels:\n(?:  .*\n|    .*\n)+", "", t, count=1, flags=re.M),
        lambda t: t.replace("    score: 1\n", "", 1),
        lambda t: t.replace("    score: 1\n", '    score: "high"\n', 1),
        lambda t: t.replace("    score: 1\n", "    score: 1\n    extra: 2\n", 1),
        lambda t: t.replace("name: Verbosity", "name: [Verbosity]", 1),
        lambda t: t.replace('description: "Whether', '# stray\ndescription: "Whether', 1),
    ],
    ids=["no-fence", "no-labels", "no-score", "string-score", "extra-key", "bare-name", "stray"],
)
def test_malformed_frontmatter_is_rejected(mutate: object) -> None:
    assert callable(mutate)
    text = mutate(valid_text())
    assert text != valid_text()
    with pytest.raises(rubrics.RubricError):
        rubrics.parse_rubric("verbosity", text)
