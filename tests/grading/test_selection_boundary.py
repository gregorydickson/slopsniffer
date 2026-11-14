"""Selection keeps zero-cost and unknown-cost ties distinct and accepts an empty rubric map."""

from slopsniffer.config import load_config
from slopsniffer.grading.selection import rank, select_worst
from tests.grading.support import Fixture, load, memory_db


def test_equal_badness_orders_positive_zero_and_unknown_cost_and_empty_rubrics() -> None:
    fixtures = [
        Fixture("free", (1.0,), cost=0.0),
        Fixture("paid", (1.0,), cost=0.5),
        Fixture("unknown", (1.0,), cost=None),
    ]
    by_id = {fixture.session_id: fixture for fixture in fixtures}
    weights = load_config().grading.badness_weights
    with memory_db() as con:
        load(con, fixtures)
        assert [(c.session_id, c.cost_usd) for c in rank(con, weights)] == [
            (sid, by_id[sid].cost) for sid in ("paid", "free", "unknown")
        ]
        assert select_worst(con, weights, {}, len(fixtures)) == ["paid", "free", "unknown"]
