"""Worst-N session selection: composite badness from signals and PR outcomes.

``badness = signal_severity * sum(signal severity) + one penalty per distinct linked PR for
each outcome it shows`` (CI red at head, changes requested, reverted, closed unmerged), with
the weights from the config. Ties are broken by cost (dearer first, unpriced last), then by
session id so the order is total.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import duckdb

from slopsniffer.config import BadnessWeights
from slopsniffer.contracts import Rubric as RubricName
from slopsniffer.grading.rubrics import Rubric

# GitHub StatusState values of the head commit's rollup that count as a red build.
CI_RED = ("FAILURE", "ERROR")
CHANGES_REQUESTED = "CHANGES_REQUESTED"

# These history-wide joins/aggregates carry only scalar session/PR identities and
# numeric totals (one result per session). They can spill; no wide event columns.
_FACTS = """
WITH prs AS (
    SELECT DISTINCT l.session_id, l.repo, l.pr_number
    FROM session_links l
    WHERE l.pr_number IS NOT NULL
),
outcome AS (
    SELECT prs.session_id,
           count(*) FILTER (WHERE o.ci_rollup_at_head IN (SELECT unnest($ci_red))) AS ci_red,
           count(*) FILTER (WHERE o.review_decision = $changes_requested) AS changes_requested,
           count(*) FILTER (WHERE o.reverted_by_pr IS NOT NULL) AS reverted,
           count(*) FILTER (WHERE o.state = 'CLOSED' AND o.merged_at IS NULL) AS closed_unmerged
    FROM prs JOIN pr_outcomes o ON o.repo = prs.repo AND o.pr_number = prs.pr_number
    GROUP BY prs.session_id
),
severity AS (
    SELECT session_id, sum(severity) AS total FROM signals GROUP BY session_id
)
SELECT s.session_id,
       coalesce(severity.total, 0), coalesce(outcome.ci_red, 0),
       coalesce(outcome.changes_requested, 0), coalesce(outcome.reverted, 0),
       coalesce(outcome.closed_unmerged, 0), c.cost_usd
FROM sessions s
LEFT JOIN severity ON severity.session_id = s.session_id
LEFT JOIN outcome ON outcome.session_id = s.session_id
LEFT JOIN session_cost c ON c.session_id = s.session_id
WHERE EXISTS (SELECT 1 FROM events e WHERE e.session_id = s.session_id)
"""

# Successful-grade lookup retains narrow session/rubric identities only.
_GRADED = """
SELECT session_id, rubric FROM grades
WHERE status = 'graded' AND rubric_hash = ?
"""


@dataclass(frozen=True)
class Candidate:
    session_id: str
    badness: float
    cost_usd: float | None


def rank(con: duckdb.DuckDBPyConnection, weights: BadnessWeights) -> list[Candidate]:
    """Every session with positive badness, worst first."""
    rows = con.execute(_FACTS, {"ci_red": list(CI_RED), "changes_requested": CHANGES_REQUESTED})
    candidates = [
        Candidate(
            session_id,
            weights.signal_severity * float(severity)
            + weights.ci_red_at_head * ci_red
            + weights.changes_requested * changes
            + weights.reverted * reverted
            + weights.closed_unmerged * closed,
            None if cost is None else float(cost),
        )
        for session_id, severity, ci_red, changes, reverted, closed, cost in rows.fetchall()
    ]
    worst = [c for c in candidates if c.badness > 0]
    worst.sort(key=lambda c: (-c.badness, c.cost_usd is None, -(c.cost_usd or 0.0), c.session_id))
    return worst


def graded_sessions(
    con: duckdb.DuckDBPyConnection, rubrics: Mapping[RubricName, Rubric]
) -> frozenset[str]:
    """Sessions holding a successful grade, at the current rubric hash, for every rubric."""
    per_rubric = [
        {sid for (sid, rubric) in con.execute(_GRADED, [r.sha256]).fetchall() if rubric == name}
        for name, r in rubrics.items()
    ]
    return frozenset(set.intersection(*per_rubric)) if per_rubric else frozenset()


def select_worst(
    con: duckdb.DuckDBPyConnection,
    weights: BadnessWeights,
    rubrics: Mapping[RubricName, Rubric],
    count: int,
    *,
    regrade: bool = False,
) -> list[str]:
    """Session ids of the ``count`` worst sessions still worth grading."""
    # Rank scalar per-session facts in DuckDB, and fetch only the requested N.
    # No message text, raw JSON, tool bodies or evidence arrays enter this plan.
    params: dict[str, object] = {
        "ci_red": list(CI_RED),
        "changes_requested": CHANGES_REQUESTED,
        "signal_weight": weights.signal_severity,
        "ci_weight": weights.ci_red_at_head,
        "changes_weight": weights.changes_requested,
        "reverted_weight": weights.reverted,
        "closed_weight": weights.closed_unmerged,
        "limit": count,
        "rubrics": list(rubrics),
        "hashes": [r.sha256 for r in rubrics.values()],
        "regrade": regrade,
    }
    # Name scalar columns before wrapping the facts; its aggregate expressions
    # otherwise inherit engine-generated names.
    sql = (
        """
    WITH facts (session_id, severity, ci_red, changes, reverted, closed, cost) AS (
    """
        + _FACTS
        + """
    ), current_rubrics AS (SELECT unnest($rubrics::VARCHAR[]) AS rubric,
                                 unnest($hashes::VARCHAR[]) AS rubric_hash),
    skipped AS (
        SELECT g.session_id FROM grades g JOIN current_rubrics r USING (rubric, rubric_hash)
        WHERE g.status = 'graded' GROUP BY g.session_id
        HAVING count(DISTINCT g.rubric) = len($rubrics::VARCHAR[])
    ), scored AS (
        SELECT session_id, cost, severity * $signal_weight + ci_red * $ci_weight
               + changes * $changes_weight + reverted * $reverted_weight
               + closed * $closed_weight AS badness FROM facts
    )
    SELECT session_id FROM scored WHERE badness > 0 AND
        ($regrade OR session_id NOT IN (SELECT session_id FROM skipped))
    ORDER BY badness DESC, cost DESC NULLS LAST, session_id LIMIT $limit
    """
    )
    return [sid for (sid,) in con.execute(sql, params).fetchall()]
