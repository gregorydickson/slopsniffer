from dataclasses import replace

import duckdb
import pytest

from slopsniffer.config import BadnessWeights, load_config
from slopsniffer.grading import rubrics, selection
from tests.grading.support import Fixture, Pr, load, memory_db

WEIGHTS = load_config().grading.badness_weights
RUBRICS = rubrics.load_rubrics()

FIXTURES = [
    Fixture("sig-heavy", severities=(2.0, 1.5), cost=1.0),
    Fixture("ci-red", prs=(Pr(1, ci="FAILURE"),), cost=2.0),
    Fixture("ci-error", prs=(Pr(2, ci="ERROR"),), cost=2.0),
    Fixture("ci-green", prs=(Pr(3, ci="SUCCESS"),)),
    Fixture("ci-null", prs=(Pr(4, ci=None),)),
    Fixture("changes", prs=(Pr(5, review_decision="CHANGES_REQUESTED"),), cost=3.0),
    Fixture("approved", prs=(Pr(6, review_decision="APPROVED"),)),
    Fixture("reverted", prs=(Pr(7, state="MERGED", merged=True, reverted_by=70),), cost=4.0),
    Fixture("closed", prs=(Pr(8, state="CLOSED"),), cost=5.0),
    Fixture("merged-clean", prs=(Pr(9, state="MERGED", merged=True),)),
    Fixture("not-found", prs=(Pr(10, state="NOT_FOUND"),)),
    Fixture(
        "everything",
        severities=(0.5,),
        prs=(Pr(11, state="CLOSED", ci="FAILURE", review_decision="CHANGES_REQUESTED"),),
    ),
    Fixture("same-pr-twice", prs=(Pr(12, ci="FAILURE"),), links=(12, 12)),
    Fixture("quiet"),
]


def expected_badness(weights: BadnessWeights) -> dict[str, float]:
    """Badness of each fixture session, worked out from the fixture data and the weights."""
    out: dict[str, float] = {}
    for fx in FIXTURES:
        score = weights.signal_severity * sum(fx.severities)
        for pr in {p.number: p for p in fx.prs}.values():
            score += weights.ci_red_at_head * (pr.ci in ("FAILURE", "ERROR"))
            score += weights.changes_requested * (pr.review_decision == "CHANGES_REQUESTED")
            score += weights.reverted * (pr.reverted_by is not None)
            score += weights.closed_unmerged * (pr.state == "CLOSED" and not pr.merged)
        out[fx.session_id] = score
    return out


def top(weights: BadnessWeights, count: int) -> list[str]:
    cost = {fx.session_id: fx.cost for fx in FIXTURES}
    scores = expected_badness(weights)
    order = sorted(
        (sid for sid, score in scores.items() if score > 0),
        key=lambda sid: (-scores[sid], cost[sid] is None, -(cost[sid] or 0.0), sid),
    )
    return order[:count]


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = memory_db()
    load(con, FIXTURES)
    return con


def mark_graded(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    *,
    hash_of: str | None = None,
    status: str = "graded",
) -> None:
    for name, rubric in RUBRICS.items():
        con.execute(
            "INSERT INTO grades (session_id, rubric, rubric_hash, status, graded_at) "
            "VALUES (?, ?, ?, ?, now())",
            [session_id, name, hash_of or rubric.sha256, status],
        )


def test_worst_two_equal_the_top_two_computed_from_the_config_weights(
    con: duckdb.DuckDBPyConnection,
) -> None:
    chosen = selection.select_worst(con, WEIGHTS, RUBRICS, 2)
    assert set(chosen) == set(top(WEIGHTS, 2))
    assert chosen == top(WEIGHTS, 2)


def test_ranking_follows_the_weights_not_a_fixed_order(con: duckdb.DuckDBPyConnection) -> None:
    flipped = replace(
        WEIGHTS, signal_severity=50.0, ci_red_at_head=0.0, reverted=0.0, closed_unmerged=0.0
    )
    assert top(flipped, 2) != top(WEIGHTS, 2)
    assert set(selection.select_worst(con, flipped, RUBRICS, 2)) == set(top(flipped, 2))


def test_every_session_with_badness_ranks_and_the_rest_never_do(
    con: duckdb.DuckDBPyConnection,
) -> None:
    ranked = {c.session_id: c.badness for c in selection.rank(con, WEIGHTS)}
    positive = {sid: s for sid, s in expected_badness(WEIGHTS).items() if s > 0}
    assert ranked.keys() == positive.keys()
    assert ranked == pytest.approx(positive)
    assert {"quiet", "ci-green", "ci-null", "approved", "merged-clean", "not-found"}.isdisjoint(
        ranked
    )


def test_ties_break_by_cost_then_unpriced_last_then_session_id() -> None:
    con = memory_db()

    def pr(number: int) -> tuple[Pr, ...]:
        return (Pr(number, ci="FAILURE"),)

    load(
        con,
        [
            Fixture("t-null", prs=pr(1)),
            Fixture("t-cheap", prs=pr(2), cost=1.0),
            Fixture("t-dear-b", prs=pr(3), cost=9.0),
            Fixture("t-dear-a", prs=pr(4), cost=9.0),
        ],
    )
    assert selection.select_worst(con, WEIGHTS, RUBRICS, 10) == [
        "t-dear-a",
        "t-dear-b",
        "t-cheap",
        "t-null",
    ]


def test_graded_session_is_skipped_and_regrade_includes_it(con: duckdb.DuckDBPyConnection) -> None:
    worst = top(WEIGHTS, 2)
    mark_graded(con, worst[0])
    skipped = selection.select_worst(con, WEIGHTS, RUBRICS, 2)
    assert worst[0] not in skipped
    assert skipped == top(WEIGHTS, 3)[1:]
    assert selection.select_worst(con, WEIGHTS, RUBRICS, 2, regrade=True) == worst


@pytest.mark.parametrize("variant", ["stale-hash", "error-status", "one-rubric-missing"])
def test_only_a_complete_current_successful_grading_skips_a_session(
    con: duckdb.DuckDBPyConnection, variant: str
) -> None:
    target = top(WEIGHTS, 1)[0]
    match variant:
        case "stale-hash":
            mark_graded(con, target, hash_of="0" * 64)
        case "error-status":
            mark_graded(con, target, status="error")
        case _:
            mark_graded(con, target)
            con.execute("DELETE FROM grades WHERE rubric = 'verbosity'")
    assert selection.select_worst(con, WEIGHTS, RUBRICS, 1) == [target]


def test_a_pr_linked_twice_is_penalised_once(con: duckdb.DuckDBPyConnection) -> None:
    ranked = {c.session_id: c.badness for c in selection.rank(con, WEIGHTS)}
    assert ranked["same-pr-twice"] == pytest.approx(WEIGHTS.ci_red_at_head)
