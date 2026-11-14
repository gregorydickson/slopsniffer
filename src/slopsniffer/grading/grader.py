"""Map-reduce grading: one verdict per (chunk, rubric), reduced to one verdict per session.

Map: each chunk of a selected session is graded against each rubric by the first usable backend
in the run's order (``auto`` is Codex, then Claude). A backend that reports an exhausted quota
is not called again in the run. A chunk no backend could grade is an error, and its session's
verdict for that rubric is ``error``; the rubric's remaining chunks are not graded.

Reduce, with ``insufficient_evidence`` chunks excluded: code-quality and procedure-compliance take
the worst chunk's label; efficiency and verbosity take the mean chunk score, labelled with the
rubric label nearest that mean. A chunk's score is the rubric's frontmatter score for its label.

``max_calls`` caps backend invocations. When the cap is reached the run stops; a rubric whose
chunks were not all graded writes nothing and stays ungraded for the next run.
"""

import json
import tempfile
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Literal

import duckdb

from slopsniffer.config import Config
from slopsniffer.contracts import ChunkVerdict, GradeSummary
from slopsniffer.contracts import Rubric as RubricName
from slopsniffer.grading.backends import BACKENDS, ORDER, Backend, Invocation, Outcome, Request
from slopsniffer.grading.render import Chunk, render_chunks
from slopsniffer.grading.rubrics import Rubric
from slopsniffer.grading.selection import select_worst

INSUFFICIENT = "insufficient_evidence"
Family = Literal["worst_label", "mean_score"]
FAMILIES: Mapping[RubricName, Family] = {
    "code-quality": "worst_label",
    "procedure-compliance": "worst_label",
    "efficiency": "mean_score",
    "verbosity": "mean_score",
}
NO_EVENTS_REASON = "The session rendered no gradable events."


@dataclass(frozen=True)
class SessionVerdict:
    label: str
    score: float | None
    reason: str | None
    cited_event_ids: list[str]


def chunk_score(rubric: Rubric, verdict: ChunkVerdict) -> float:
    return next(label.score for label in rubric.labels if label.value == verdict["label"])


def reduce(rubric: Rubric, verdicts: Sequence[ChunkVerdict]) -> SessionVerdict:
    scored = [(v, chunk_score(rubric, v)) for v in verdicts if v["label"] != INSUFFICIENT]
    if not scored:
        reason = verdicts[0]["reason"] if verdicts else NO_EVENTS_REASON
        return SessionVerdict(INSUFFICIENT, None, reason, [])
    worst, worst_score = min(scored, key=lambda pair: pair[1])
    if FAMILIES[rubric.name] == "worst_label":
        return SessionVerdict(
            worst["label"], worst_score, worst["reason"], list(worst["cited_event_ids"])
        )
    # Frontmatter scores are decimals: exact arithmetic keeps an equidistant mean a real tie.
    mean = sum(_exact(score) for _, score in scored) / len(scored)
    nearest = min(
        (label for label in rubric.labels if label.value != INSUFFICIENT),
        key=lambda label: (abs(_exact(label.score) - mean), label.score),
    )
    cited = list(dict.fromkeys(e for v, _ in scored for e in v["cited_event_ids"]))
    return SessionVerdict(nearest.value, float(mean), worst["reason"], cited)


def _exact(score: float) -> Fraction:
    return Fraction(repr(score))


def build_prompt(rubric: Rubric, chunk: Chunk, chunk_count: int) -> str:
    labels = "\n".join(
        f"- `{lb.value}` (score {lb.score:g}): {lb.description}" for lb in rubric.labels
    )
    return "\n\n".join(
        part
        for part in (
            f"You are grading part of an AI coding-agent session transcript against the "
            f"{rubric.title} rubric. This is chunk {chunk.index + 1} of {chunk_count}; judge "
            "only what this chunk shows.",
            f"# Rubric: {rubric.name}\n{rubric.description}\n{rubric.body.strip()}",
            f"# Labels (choose exactly one)\n{labels}",
            f'Answer with one JSON object: "rubric" is "{rubric.name}", "label" is one of the '
            'label values above, "score" is the score listed for that label, "reason" is one to '
            'three sentences, and "cited_event_ids" lists the ids of the events that support '
            'the verdict (the text after "[e:" in an event header). Cite only ids that appear '
            "in this chunk.",
            chunk.header,
            f"# Transcript\n{chunk.text}",
        )
        if part
    )


class _CapReached(Exception):
    pass


@dataclass
class _Calls:
    con: duckdb.DuckDBPyConnection
    order: tuple[str, ...]
    backends: Mapping[str, Backend]
    max_calls: int | None
    workdir: Path
    by_backend: Counter[str] = field(default_factory=Counter)
    exhausted: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return sum(self.by_backend.values())

    def grade(self, session_id: str, chunk_index: int, request: Request) -> ChunkVerdict | None:
        for name in self.order:
            if name in self.exhausted:
                continue
            if self.max_calls is not None and self.total >= self.max_calls:
                raise _CapReached
            invocation = self.backends[name](request, self.workdir)
            self.by_backend[name] += 1
            _record_call(self.con, session_id, chunk_index, request.rubric.name, invocation)
            if invocation.verdict is not None:
                return invocation.verdict
            if invocation.outcome is Outcome.QUOTA:
                self.exhausted.add(name)
        return None


@dataclass(frozen=True)
class GradeRun:
    summary: GradeSummary
    capped: bool


def grade(
    con: duckdb.DuckDBPyConnection,
    config: Config,
    rubrics: Mapping[RubricName, Rubric],
    *,
    worst: int,
    backend: str,
    max_calls: int | None = None,
    regrade: bool = False,
    backends: Mapping[str, Backend] = BACKENDS,
) -> GradeRun:
    selected = select_worst(con, config.grading.badness_weights, rubrics, worst, regrade=regrade)
    errored: set[tuple[str, int]] = set()
    sessions_graded = 0
    capped = False
    with tempfile.TemporaryDirectory(prefix="slopsniffer-grade-") as tmp:
        calls = _Calls(con, ORDER[backend], backends, max_calls, Path(tmp))
        try:
            for session_id in selected:
                done = set() if regrade else _graded_rubrics(con, session_id, rubrics)
                chunks = render_chunks(con, config.grading, session_id)
                complete = True
                for name, rubric in rubrics.items():
                    if name in done:
                        continue
                    verdict = _grade_rubric(calls, session_id, rubric, chunks, errored)
                    _store_grade(con, session_id, rubric, verdict)
                    complete = complete and verdict is not None
                sessions_graded += complete
        except _CapReached:
            capped = True
    summary: GradeSummary = {
        "selected_session_ids": selected,
        "calls": calls.total,
        "calls_by_backend": dict(calls.by_backend),
        "chunks_errored": len(errored),
        "sessions_graded": sessions_graded,
    }
    return GradeRun(summary, capped)


def _grade_rubric(
    calls: _Calls,
    session_id: str,
    rubric: Rubric,
    chunks: list[Chunk],
    errored: set[tuple[str, int]],
) -> SessionVerdict | None:
    verdicts: list[ChunkVerdict] = []
    for chunk in chunks:
        request = Request(
            rubric, build_prompt(rubric, chunk, len(chunks)), frozenset(chunk.event_ids)
        )
        verdict = calls.grade(session_id, chunk.index, request)
        if verdict is None:
            errored.add((session_id, chunk.index))
            return None
        verdicts.append(verdict)
    return reduce(rubric, verdicts)


def _graded_rubrics(
    con: duckdb.DuckDBPyConnection, session_id: str, rubrics: Mapping[RubricName, Rubric]
) -> set[str]:
    rows = con.execute(
        "SELECT rubric, rubric_hash FROM grades WHERE session_id = ? AND status = 'graded'",
        [session_id],
    ).fetchall()
    return {r for r, h in rows if r in rubrics and rubrics[r].sha256 == h}


def _store_grade(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    rubric: Rubric,
    verdict: SessionVerdict | None,
) -> None:
    con.execute(
        "MERGE INTO grades AS stored USING (VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())) "
        "AS incoming (session_id, rubric, rubric_hash, status, label, score, reason, "
        "cited_event_ids, graded_at) ON stored.session_id = incoming.session_id "
        "AND stored.rubric = incoming.rubric AND stored.rubric_hash = incoming.rubric_hash "
        "WHEN MATCHED THEN UPDATE SET status = incoming.status, label = incoming.label, "
        "score = incoming.score, reason = incoming.reason, "
        "cited_event_ids = incoming.cited_event_ids, graded_at = incoming.graded_at "
        "WHEN NOT MATCHED THEN INSERT BY NAME",
        [
            session_id,
            rubric.name,
            rubric.sha256,
            "error" if verdict is None else "graded",
            None if verdict is None else verdict.label,
            None if verdict is None else verdict.score,
            None if verdict is None else verdict.reason,
            None if verdict is None else verdict.cited_event_ids,
        ],
    )


def _record_call(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    chunk_index: int,
    rubric: str,
    call: Invocation,
) -> None:
    con.execute(
        "INSERT INTO grade_calls (call_id, session_id, chunk_index, backend, started_at, "
        "duration_ms, input_tokens, output_tokens, exit_code, status, output, error) "
        "VALUES (?, ?, ?, ?, CAST(? AS TIMESTAMPTZ), ?, ?, ?, ?, ?, ?, ?)",
        [
            uuid.uuid4().hex,
            session_id,
            chunk_index,
            call.backend,
            call.started_at.isoformat(),
            call.duration_ms,
            call.input_tokens,
            call.output_tokens,
            call.exit_code,
            call.outcome.value,
            None if call.output is None else json.dumps(call.output),
            None if call.error is None else f"{rubric}: {call.error}",
        ],
    )
