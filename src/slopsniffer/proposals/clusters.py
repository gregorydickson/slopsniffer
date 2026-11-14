"""Failed verdicts, the instruction surface each one is attributed to, and the evidence gate.

A failed verdict is a current-rubric ``graded`` row scoring below ``FAIL_BELOW``; error rows and
``insufficient_evidence`` (NULL score) never qualify. Its surfaces, in order of preference: the
inventoried skills whose invocation the verdict cites; else every inventoried skill the session
invoked; else the CLAUDE.md files in force in the session (rule surfaces); else ``UNATTRIBUTED``.

Verdicts cluster by ``(surface, rubric, root cause)``, the root cause being the reason's distinct
content words, so restatements that differ only in case, punctuation, word order, cited ids or
filler words land together. A cluster passes the gate when it spans two or more sessions or holds a
protective-rule breach: a procedure-compliance failure whose reason names a protective rule, the
rubric's own definition of a breach that a single occurrence already proves.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb

from slopsniffer import analysis
from slopsniffer.contracts import Rubric as RubricName
from slopsniffer.grading.rubrics import Rubric
from slopsniffer.ingest.reader import BATCH_ROWS

FAIL_BELOW = 0.5
MIN_SESSIONS = 2
UNATTRIBUTED = "unattributed"
PROTECTIVE_RUBRIC = "procedure-compliance"
# Lower-cased phrases of the procedure-compliance rubric's protective rules.
PROTECTIVE_MARKERS = (
    "protective",
    "protected branch",
    "force-push",
    "force push",
    "--force",
    "destructive",
    "irreversib",
    "reset --hard",
    "rm -rf",
)
_STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
        "agent",
        "session",
    ]
)
_WORD = re.compile(r"[a-z]+")
# Duplicate skill names resolve to the narrowest scope that applies to the session.
_SCOPE_ORDER = {"project": 0, "global": 1, "plugin": 2}


@dataclass(frozen=True)
class Surface:
    """An instruction file a proposal would edit; ``path`` is None only for ``UNATTRIBUTED``."""

    name: str
    path: str | None


@dataclass(frozen=True)
class Failure:
    session_id: str
    rubric: RubricName
    label: str
    score: float
    reason: str
    event_ids: tuple[str, ...]

    @property
    def protective(self) -> bool:
        text = self.reason.lower()
        return self.rubric == PROTECTIVE_RUBRIC and any(m in text for m in PROTECTIVE_MARKERS)


@dataclass(frozen=True)
class Cluster:
    surface: Surface
    rubric: RubricName
    root_cause: str
    failures: Iterable[Failure]

    @property
    def key(self) -> str:
        return f"{self.surface.name}|{self.rubric}|{self.root_cause}"

    @property
    def session_ids(self) -> list[str]:
        return sorted({f.session_id for f in self.failures})

    @property
    def protective(self) -> bool:
        return any(f.protective for f in self.failures)

    def rejection(self) -> str | None:
        """Why the gate rejects this cluster, or None when it passes."""
        if self.surface.path is None:
            return "no owning skill or rule file could be identified for these sessions"
        if len(self.session_ids) >= MIN_SESSIONS or self.protective:
            return None
        return f"single session {self.session_ids[0]} and no protective-rule breach"


def root_cause(reason: str, event_ids: Sequence[str]) -> str:
    text = reason.lower()
    for event_id in event_ids:
        text = text.replace(event_id.lower(), " ")
    return " ".join(sorted(set(_WORD.findall(text)) - _STOP_WORDS))


def failures(con: duckdb.DuckDBPyConnection, rubrics: Mapping[RubricName, Rubric]) -> list[Failure]:
    return list(_failure_rows(con, rubrics))


def _failure_rows(
    con: duckdb.DuckDBPyConnection, rubrics: Mapping[RubricName, Rubric]
) -> Iterator[Failure]:
    # Page current grade identities, then fetch one reduced verdict. Wide reasons
    # never participate in a history-wide sort or Python result list on the CLI.
    last: tuple[str, str, str] | None = None
    while True:
        values: list[object] = [FAIL_BELOW, list(rubrics), [r.sha256 for r in rubrics.values()]]
        after = ""
        if last is not None:
            after = " AND (session_id, rubric, rubric_hash) > ($4, $5, $6)"
            values.extend(last)
        page = con.execute(
            "SELECT session_id, rubric, rubric_hash FROM grades WHERE status = 'graded' "
            "AND score < $1 AND (rubric, rubric_hash) IN "
            "(SELECT unnest($2::VARCHAR[]), unnest($3::VARCHAR[]))"
            + after
            + f" ORDER BY session_id, rubric, rubric_hash LIMIT {BATCH_ROWS}",
            values,
        ).fetchall()
        if not page:
            return
        for sid, rubric, digest in page:
            label, score, reason, cited = analysis.physical_row(
                con,
                "grades",
                "label, score, reason, cited_event_ids",
                "session_id = ? AND rubric = ? AND rubric_hash = ?",
                [sid, rubric, digest],
            )
            yield Failure(sid, rubric, label, score, reason or "", tuple(cited or ()))
            last = sid, rubric, digest


@dataclass(frozen=True)
class _StoredFailures:
    con: duckdb.DuckDBPyConnection
    surface: Surface
    rubric: RubricName
    cause: str

    def __iter__(self) -> Iterator[Failure]:
        last = -1
        while page := self.con.execute(
            "SELECT ordinal FROM proposal_failure_spool WHERE surface = ? "
            "AND path IS NOT DISTINCT FROM ? AND rubric = ? AND cause = ? AND ordinal > ? "
            "ORDER BY ordinal LIMIT ?",
            [self.surface.name, self.surface.path, self.rubric, self.cause, last, BATCH_ROWS],
        ).fetchall():
            for (ordinal,) in page:
                sid, label, score, reason, cited = analysis.physical_row(
                    self.con,
                    "proposal_failure_spool",
                    "session_id, label, score, reason, cited",
                    "ordinal = ?",
                    [ordinal],
                )
                yield Failure(sid, self.rubric, label, score, reason, tuple(cited))
                last = ordinal


@contextmanager
def stored_clusters(
    con: duckdb.DuckDBPyConnection, rubrics: Mapping[RubricName, Rubric]
) -> Iterator[list[Cluster]]:
    """Keep failure bodies on disk; retain only cluster identities in Python."""
    con.execute(
        "CREATE TEMP TABLE proposal_failure_spool (ordinal BIGINT, surface VARCHAR, "
        "path VARCHAR, rubric VARCHAR, cause VARCHAR, session_id VARCHAR, "
        "label VARCHAR, score DOUBLE, reason VARCHAR, cited VARCHAR[])"
    )
    try:
        skills = _skills_by_name(con)
        ordinal = 0
        for failure in _failure_rows(con, rubrics):
            cause = root_cause(failure.reason, failure.event_ids)
            for surface in surfaces(con, failure, skills):
                con.execute(
                    "INSERT INTO proposal_failure_spool VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ordinal,
                        surface.name,
                        surface.path,
                        failure.rubric,
                        cause,
                        failure.session_id,
                        failure.label,
                        failure.score,
                        failure.reason,
                        list(failure.event_ids),
                    ],
                )
                ordinal += 1
        # Only normalized cluster identities are grouped/sorted, never reasons.
        found = [
            Cluster(
                Surface(name, path),
                rubric,
                cause,
                _StoredFailures(con, Surface(name, path), rubric, cause),
            )
            for name, path, rubric, cause, _ in con.execute(
                "SELECT surface, path, rubric, cause, min(ordinal) AS first "
                "FROM proposal_failure_spool "
                "GROUP BY surface, path, rubric, cause "
                "ORDER BY surface||'|'||rubric||'|'||cause, first"
            ).fetchall()
        ]
        yield found
    finally:
        con.execute("DROP TABLE proposal_failure_spool")


# Skill lookup is bounded by the installed inventory, independent of transcript history.
def _skills_by_name(con: duckdb.DuckDBPyConnection) -> dict[str, list[tuple[str, str, str | None]]]:
    found: dict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
    for name, path, scope, repo in con.execute(
        "SELECT name, path, scope, repo FROM skills ORDER BY path"
    ).fetchall():
        found[name].append((path, scope, repo))
    return found


def _pick(candidates: list[tuple[str, str, str | None]], cwd: str | None) -> str:
    def rank(candidate: tuple[str, str, str | None]) -> tuple[int, str]:
        path, scope, repo = candidate
        if scope == "project" and not (repo and cwd and Path(cwd).is_relative_to(repo)):
            return len(_SCOPE_ORDER), path
        return _SCOPE_ORDER[scope], path

    return min(candidates, key=rank)[0]


def surfaces(
    con: duckdb.DuckDBPyConnection,
    failure: Failure,
    skills: Mapping[str, list[tuple[str, str, str | None]]],
) -> list[Surface]:
    # Each evidence lookup is scoped to this one failure session; paths/ids only.
    cwd_row = con.execute(
        "SELECT cwd FROM sessions WHERE session_id = ?", [failure.session_id]
    ).fetchone()
    cwd = cwd_row[0] if cwd_row else None
    invoked = [
        (name, event_id)
        for name, event_id in con.execute(
            "SELECT skill_name, event_id FROM skill_invocations WHERE session_id = ? "
            "ORDER BY skill_name, event_id",
            [failure.session_id],
        ).fetchall()
        if name in skills
    ]
    cited = [name for name, event_id in invoked if event_id in failure.event_ids]
    names = list(dict.fromkeys(cited or [name for name, _ in invoked]))
    if names:
        return [Surface(name, _pick(skills[name], cwd)) for name in names]
    rules = con.execute(
        "SELECT DISTINCT c.source_path FROM context_blocks c JOIN events e USING (event_id) "
        "WHERE e.session_id = ? AND c.kind = 'claude_md' AND c.source_path IS NOT NULL "
        "ORDER BY 1",
        [failure.session_id],
    ).fetchall()
    return [Surface(path, path) for (path,) in rules] or [Surface(UNATTRIBUTED, None)]


def clusters(con: duckdb.DuckDBPyConnection, rubrics: Mapping[RubricName, Rubric]) -> list[Cluster]:
    grouped: dict[tuple[Surface, RubricName, str], list[Failure]] = defaultdict(list)
    skills = _skills_by_name(con)
    for failure in failures(con, rubrics):
        cause = root_cause(failure.reason, failure.event_ids)
        for surface in surfaces(con, failure, skills):
            grouped[(surface, failure.rubric, cause)].append(failure)
    found = [Cluster(s, r, c, tuple(f)) for (s, r, c), f in grouped.items()]
    return sorted(found, key=lambda cluster: cluster.key)
