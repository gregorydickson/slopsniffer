"""The grading backend asked, under ``skill-improvements.md``, for one surface's improved text.

The answer is ``{propose, reason, skill_md}``: the backend may decline when the evidence does not
clear the rubric's bar, which is a rejection, not a failure. Backends are tried in the run's order;
one that reports an exhausted quota is skipped for the rest of the run.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from slopsniffer.grading.backends import EXECUTORS, Outcome, Raw, describe, failure
from slopsniffer.grading.rubrics import skill_improvements_text
from slopsniffer.proposals.clusters import Cluster, Surface

DRAFT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["propose", "reason", "skill_md"],
    "properties": {
        "propose": {"type": "boolean"},
        "reason": {"type": "string"},
        "skill_md": {"type": "string"},
    },
}

Executor = Callable[[Mapping[str, Any], str, Path], Raw]


@dataclass(frozen=True)
class Draft:
    propose: bool
    reason: str
    text: str


def build_prompt(surface: Surface, current: str, clusters: Sequence[Cluster]) -> str:
    findings = "\n".join(
        f"- [{c.rubric}] sessions {', '.join(c.session_ids)}"
        + (" (protective-rule breach)" if c.protective else "")
        + "".join(f"\n  - {f.session_id} `{f.label}`: {f.reason}" for f in c.failures)
        for c in clusters
    )
    return "\n\n".join(
        (
            "You are proposing an edit to an AI coding agent's instruction file, based on graded "
            "failures from its recorded sessions. Follow these guidelines exactly.",
            skill_improvements_text().strip(),
            f"# Instruction file: {surface.name}\nPath: {surface.path}\n\n{current}",
            f"# Scored failures attributed to this file\n{findings}",
            'Answer with one JSON object: "propose" is true only when the guidelines say to file '
            'a change; "reason" states, in one to three sentences, the behavioural rule and why '
            'it clears or misses the bar; "skill_md" is the complete improved file text when '
            "proposing, else the current text unchanged.",
        )
    )


@dataclass
class Drafter:
    order: tuple[str, ...]
    workdir: Path
    executors: Mapping[str, Executor] = field(default_factory=lambda: EXECUTORS)
    exhausted: set[str] = field(default_factory=set)

    def draft(self, prompt: str) -> tuple[Draft | None, str | None]:
        """The first usable answer, or why no backend produced one."""
        problems: list[str] = []
        for name in self.order:
            if name in self.exhausted:
                continue
            raw = self.executors[name](DRAFT_SCHEMA, prompt, self.workdir)
            draft, problem = _usable(raw)
            if draft is not None:
                return draft, None
            outcome = failure(raw.run)
            if outcome is Outcome.QUOTA:
                self.exhausted.add(name)
            problems.append(f"{name}: {describe(outcome, raw.run, problem)}")
        return None, "; ".join(problems) or "every backend's quota is exhausted"


def _usable(raw: Raw) -> tuple[Draft | None, str | None]:
    if raw.output is None:
        return None, "no output"
    errors = list(Draft202012Validator(DRAFT_SCHEMA).iter_errors(raw.output))
    if errors:
        return None, f"schema: {errors[0].json_path}: {errors[0].message}"
    if raw.run.error is not None or raw.run.exit_code != 0:
        return None, None
    answer = cast(dict[str, Any], raw.output)
    return Draft(answer["propose"], answer["reason"], answer["skill_md"]), None
