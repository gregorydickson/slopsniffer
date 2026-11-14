import json
from pathlib import Path

import pytest

from tests.ingest.support import query
from tests.signals import support

ROLLUP = "SELECT cost_usd, tokens FROM session_rollup WHERE session_id = ?"
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def last_cost(path: Path) -> float:
    costs = [r["totalCostUSD"] for r in records(path) if r["type"] == "cost-state"]
    cost = costs[-1]
    assert isinstance(cost, float)
    return cost


def tokens(path: Path) -> int:
    total = 0
    for r in records(path):
        message = r.get("message")
        usage = message.get("usage", {}) if isinstance(message, dict) else {}
        total += sum(usage.get(field, 0) for field in TOKEN_FIELDS)
    return total


def tree(root: Path, parent: str) -> tuple[Path, Path]:
    project = root / "claude" / "-work-sig"
    [child] = (project / parent / "subagents").glob("agent-*.jsonl")
    return project / f"{parent}.jsonl", child


def test_a_parent_rolls_up_its_subagents_cost_and_tokens(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    support.run(capsys)
    parent, child = tree(root, "roll-parent")
    [(cost, total)] = query(ROLLUP, ["roll-parent"])
    assert cost == pytest.approx(last_cost(parent) + last_cost(child))
    assert total == tokens(parent) + tokens(child)
    roots = {row[0] for row in query("SELECT session_id FROM session_rollup")}
    assert child.stem.removeprefix("agent-") not in roots


def test_one_unpriced_session_preserves_known_tree_cost(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    support.run(capsys)
    parent, child = tree(root, "roll-unpriced")
    assert not [r for r in records(parent) if r["type"] == "cost-state"]
    assert last_cost(child) > 0
    assert query(ROLLUP, ["roll-unpriced"])[0][0] >= last_cost(child)


def test_report_rollups_cover_every_dimension_and_root_lane(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    lanes = {
        row[0] for row in query("SELECT harness FROM sessions WHERE parent_session_id IS NULL")
    }
    assert {row["key"] for row in report["rollups"]["lane"]} == lanes
    invoked = {row[0] for row in query("SELECT skill_name FROM skill_invocations")}
    assert {row["key"] for row in report["rollups"]["skill"]} == invoked
    views = {row[0] for row in query("SELECT view_name FROM duckdb_views() WHERE NOT internal")}
    assert {f"rollup_by_{dim}" for dim in report["rollups"]} <= views
