"""Exercise the actual draft boundary with deterministic model responses."""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from slopsniffer.grading.backends import Outcome, Raw, _Run
from slopsniffer.grading.rubrics import skill_improvements_text
from slopsniffer.proposals.clusters import Cluster, Failure, Surface
from slopsniffer.proposals.drafter import Draft, Drafter, Executor, build_prompt


@pytest.mark.parametrize(
    ("output", "exit_code", "error"),
    [
        (None, 0, None),
        ({"propose": "yes", "reason": "why", "skill_md": "new"}, 0, None),
        ({"propose": True, "reason": "why", "skill_md": "new", "extra": 1}, 0, None),
        ({"propose": True, "reason": "why", "skill_md": "new"}, 1, None),
        ({"propose": True, "reason": "why", "skill_md": "new"}, 0, Outcome.TIMEOUT),
    ],
)
def test_unusable_answers_fall_back_and_valid_answers_keep_every_field(
    tmp_path: Path, output: object, exit_code: int, error: Outcome | None
) -> None:
    seen: list[tuple[str, str, Path, Mapping[str, Any]]] = []

    def invoke(name: str, answer: object, code: int, problem: Outcome | None) -> Executor:
        def execute(schema: Mapping[str, Any], prompt: str, cwd: Path) -> Raw:
            seen.append((name, prompt, cwd, schema))
            return Raw(_Run(datetime.now(UTC), 0, code, "", "", problem), answer, (None, None))

        return execute

    valid = {"propose": False, "reason": "No repeated evidence.", "skill_md": "current é\n"}
    drafter = Drafter(
        ("codex", "claude"),
        tmp_path,
        {
            "codex": invoke("codex", output, exit_code, error),
            "claude": invoke("claude", valid, 0, None),
        },
    )
    assert drafter.draft("full prompt") == (
        Draft(False, "No repeated evidence.", "current é\n"),
        None,
    )
    assert [(name, prompt, cwd) for name, prompt, cwd, _ in seen] == [
        ("codex", "full prompt", tmp_path),
        ("claude", "full prompt", tmp_path),
    ]
    for _, _, _, schema in seen:
        assert schema == {
            "type": "object",
            "additionalProperties": False,
            "required": ["propose", "reason", "skill_md"],
            "properties": {
                "propose": {"type": "boolean"},
                "reason": {"type": "string"},
                "skill_md": {"type": "string"},
            },
        }


def test_exhausted_backend_is_skipped_across_drafts_but_failures_are_retried(
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    def codex(schema: Mapping[str, Any], prompt: str, cwd: Path) -> Raw:
        seen.append("codex")
        return Raw(_Run(datetime.now(UTC), 0, 1, "", "quota", None), None, (None, None))

    def claude(schema: Mapping[str, Any], prompt: str, cwd: Path) -> Raw:
        seen.append("claude")
        return Raw(_Run(datetime.now(UTC), 0, 1, "", "disconnected", None), None, (None, None))

    drafter = Drafter(("codex", "claude"), tmp_path, {"codex": codex, "claude": claude})
    assert drafter.draft("prompt") == (
        None,
        "codex: quota (exit 1): quota; claude: failed (exit 1): disconnected",
    )
    assert drafter.draft("prompt") == (None, "claude: failed (exit 1): disconnected")
    assert seen == ["codex", "claude", "claude"]
    drafter.exhausted.add("claude")
    assert drafter.draft("prompt") == (None, "every backend's quota is exhausted")


def test_prompt_preserves_evidence_protective_status_guidelines_and_output_instructions() -> None:
    surface = Surface("review", "/synthetic/review/SKILL.md")
    ordinary = Cluster(
        surface,
        "code-quality",
        "skipped tests",
        (
            Failure("s-a", "code-quality", "block", 0, "Skipped tests.", ("e-a",)),
            Failure("s-b", "code-quality", "block", 0, "Tests skipped.", ("e-b",)),
        ),
    )
    protective = Cluster(
        surface,
        "procedure-compliance",
        "force push",
        (Failure("s-c", "procedure-compliance", "noncompliant", 0, "Force push.", ("e-c",)),),
    )
    prompt = build_prompt(surface, "Current instructions é", [ordinary, protective])
    assert prompt.split("\n\n") == [
        "You are proposing an edit to an AI coding agent's instruction file, based on graded "
        "failures from its recorded sessions. Follow these guidelines exactly.",
        *skill_improvements_text().strip().split("\n\n"),
        "# Instruction file: review\nPath: /synthetic/review/SKILL.md",
        "Current instructions é",
        "# Scored failures attributed to this file\n"
        "- [code-quality] sessions s-a, s-b\n"
        "  - s-a `block`: Skipped tests.\n  - s-b `block`: Tests skipped.\n"
        "- [procedure-compliance] sessions s-c (protective-rule breach)\n"
        "  - s-c `noncompliant`: Force push.",
        'Answer with one JSON object: "propose" is true only when the guidelines say to file '
        'a change; "reason" states, in one to three sentences, the behavioural rule and why '
        'it clears or misses the bar; "skill_md" is the complete improved file text when '
        "proposing, else the current text unchanged.",
    ]
