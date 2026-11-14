import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from slopsniffer.cli import main
from slopsniffer.outcomes.github import MAX_BATCH
from tests.helpers import CLI_SCHEMAS_DIR, load_schema
from tests.ingest.support import query
from tests.outcomes.support import (
    FakeGh,
    PrKey,
    outcomes,
    pr_node,
    pr_session,
    write_config,
)

# The per-query PR bound the PRD sets; a spec limit, not a count of anything.
PRD_BATCH_LIMIT = 50
REPO = "acme/widgets"
EARLY, LATE = "2026-11-01T10:00:00Z", "2026-11-05T10:00:00Z"


def stored() -> set[PrKey]:
    return {(repo, n) for repo, n in query("SELECT repo, pr_number FROM pr_outcomes")}


def fetched_prs(fake: FakeGh) -> set[PrKey]:
    return set().union(*fake.queried_prs())


def nodes(prs: dict[PrKey, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f"{repo}#{n}": node for (repo, n), node in prs.items()}


def test_batches_stay_within_the_limit_and_cover_the_requested_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: FakeGh,
) -> None:
    write_config(tmp_path, monkeypatch, pr_batch_size=PRD_BATCH_LIMIT * 10)
    requested = {(REPO, n) for n in range(1, 2 * MAX_BATCH + 2)} | {("acme/gadgets", 7)}
    pr_session(tmp_path, monkeypatch, capsys, sorted(requested))
    fake_gh.scenario(prs=nodes({key: pr_node(key[1]) for key in requested}))

    code, doc = outcomes(capsys)

    assert code == 0, doc
    queried = [q for q in fake_gh.queried_prs() if q]
    assert all(len(q) <= PRD_BATCH_LIMIT for q in queried)
    assert set().union(*queried) == requested == stored()
    assert doc["prs_fetched"] == len(requested)
    calls = [c["argv"][:2] for c in fake_gh.calls()]
    graphql = [i for i, argv in enumerate(calls) if argv == ["api", "graphql"]]
    assert graphql
    assert all(i > 0 and calls[i - 1] == ["api", "rate_limit"] for i in graphql)


def test_fetch_stops_cleanly_when_the_quota_falls_below_the_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: FakeGh,
) -> None:
    reserve = 100
    write_config(tmp_path, monkeypatch, pr_batch_size=2, rate_reserve=reserve)
    requested = {(REPO, n) for n in range(1, 6)}
    pr_session(tmp_path, monkeypatch, capsys, sorted(requested))
    merged = {k: pr_node(k[1], state="MERGED", merged_at=EARLY) for k in requested}
    fake_gh.scenario(prs=nodes(merged), rate=[reserve, reserve - 1])

    code, doc = outcomes(capsys)

    assert code == 0, doc
    assert (doc["rate_limited"], doc["rate_remaining"]) == (True, reserve - 1)
    first, *rest = fake_gh.queried_prs()
    assert rest == []
    assert first and first < requested
    assert stored() == first
    assert doc["prs_fetched"] == len(first)
    assert fake_gh.calls()[-1]["argv"][:2] == ["api", "rate_limit"]


def test_nothing_is_fetched_when_the_quota_starts_below_the_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: FakeGh,
) -> None:
    write_config(tmp_path, monkeypatch, rate_reserve=100)
    pr_session(tmp_path, monkeypatch, capsys, [(REPO, 1)])
    fake_gh.scenario(prs=nodes({(REPO, 1): pr_node(1)}), rate=[99])

    code, doc = outcomes(capsys)

    assert code == 0, doc
    assert doc["rate_limited"] is True
    assert fake_gh.graphql_queries() == []
    assert stored() == set()


def test_final_prs_are_never_refetched_and_open_prs_wait_for_their_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: FakeGh,
) -> None:
    merged, closed, open_, missing = ((REPO, n) for n in (1, 2, 3, 4))
    requested = {merged, closed, open_, missing}
    pr_session(tmp_path, monkeypatch, capsys, sorted(requested))
    fake_gh.scenario(
        prs=nodes(
            {
                merged: pr_node(1, state="MERGED", merged_at=EARLY),
                closed: pr_node(2, state="CLOSED"),
                open_: pr_node(3, state="OPEN"),
            }
        )
    )
    assert outcomes(capsys)[0] == 0
    assert fetched_prs(fake_gh) == requested == stored()
    states = dict(query("SELECT pr_number, state FROM pr_outcomes"))
    assert states == {1: "MERGED", 2: "CLOSED", 3: "OPEN", 4: "NOT_FOUND"}

    fake_gh.clear_log()
    code, doc = outcomes(capsys)
    assert code == 0, doc
    assert fetched_prs(fake_gh) == set()
    assert (doc["prs_fetched"], doc["prs_cached"]) == (0, len(requested))

    fake_gh.clear_log()
    assert outcomes(capsys, "--refresh")[0] == 0
    assert fetched_prs(fake_gh) == {open_}
    assert (
        query(
            "SELECT repo, pr_number FROM pr_outcomes GROUP BY repo, pr_number HAVING count(*) <> 1"
        )
        == []
    )

    write_config(tmp_path, monkeypatch, open_pr_ttl_hours=0)
    fake_gh.clear_log()
    assert outcomes(capsys)[0] == 0
    assert fetched_prs(fake_gh) == {open_}
    assert (
        query(
            "SELECT repo, pr_number FROM pr_outcomes GROUP BY repo, pr_number HAVING count(*) <> 1"
        )
        == []
    )


def reverted_by() -> dict[int, int | None]:
    return dict(query("SELECT pr_number, reverted_by_pr FROM pr_outcomes"))


@pytest.fixture
def revert_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: FakeGh,
) -> dict[int, int | None]:
    """Linked merged PRs and the revert-search results that may or may not revert them."""
    linked = {
        10: pr_node(10, "Add widgets", "MERGED", merged_at=EARLY),
        11: pr_node(11, 'Revert "Add widgets"', "MERGED", merged_at=LATE),
        20: pr_node(20, "Tune gadget", "MERGED", merged_at=EARLY),
        30: pr_node(30, "Docs", "MERGED", merged_at=EARLY),
        40: pr_node(40, "Cache", "MERGED", merged_at=EARLY),
        50: pr_node(50, "Late change", "MERGED", merged_at=LATE),
        60: pr_node(60, "Open change", "OPEN"),
    }
    searched = [
        pr_node(21, "Undo gadget tuning", "MERGED", merged_at=LATE, body="Reverts acme/widgets#20"),
        pr_node(31, "Docs follow-up", "MERGED", merged_at=LATE, body="Follow-up to #30."),
        pr_node(41, "Undo cache", "MERGED", merged_at=LATE, body="Reverts other/repo#40"),
        pr_node(51, 'Revert "Late change"', "MERGED", merged_at=EARLY),
        pr_node(61, "Undo open change", "MERGED", merged_at=LATE, body="Reverts #60"),
    ]
    pr_session(tmp_path, monkeypatch, capsys, [(REPO, n) for n in linked])
    prs = nodes({(REPO, n): node for n, node in linked.items()})
    fake_gh.scenario(prs=prs, search={REPO: searched})
    code, doc = outcomes(capsys)
    assert code == 0, doc
    assert set(reverted_by()) == set(linked)
    return reverted_by()


def test_revert_by_title_and_by_body_reference_is_detected(
    revert_scenario: dict[int, int | None],
) -> None:
    assert {pr: by for pr, by in revert_scenario.items() if by is not None} == {10: 11, 20: 21}


def test_mentions_earlier_merges_other_repos_and_unmerged_prs_are_not_reverts(
    revert_scenario: dict[int, int | None],
) -> None:
    assert {pr for pr, by in revert_scenario.items() if by is None} == {11, 30, 40, 50, 60}


def test_a_later_revert_of_a_cached_merged_pr_is_detected_without_refetching_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: FakeGh,
) -> None:
    pr_session(tmp_path, monkeypatch, capsys, [(REPO, 10)])
    target = pr_node(10, "Add widgets", "MERGED", merged_at=EARLY)
    fake_gh.scenario(prs=nodes({(REPO, 10): target}))
    assert outcomes(capsys)[0] == 0
    assert reverted_by() == {10: None}

    fake_gh.clear_log()
    revert = pr_node(11, 'Revert "Add widgets"', "MERGED", merged_at=LATE)
    fake_gh.scenario(prs=nodes({(REPO, 10): target}), search={REPO: [revert]})
    assert outcomes(capsys)[0] == 0
    assert fetched_prs(fake_gh) == set()
    assert reverted_by() == {10: 11}


def test_ci_rollup_is_read_at_the_head_commit_and_reviews_are_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: FakeGh,
) -> None:
    reviews = [("alice", "CHANGES_REQUESTED"), ("bob", "APPROVED")]
    prs = {
        (REPO, 1): pr_node(
            1, head="h1", ci="FAILURE", reviews=reviews, review_decision="CHANGES_REQUESTED"
        ),
        (REPO, 2): pr_node(2, head="h2", last_commit="older", ci="SUCCESS"),
        (REPO, 3): pr_node(3, head="h3", ci=None),
    }
    pr_session(tmp_path, monkeypatch, capsys, sorted(prs))
    fake_gh.scenario(prs=nodes(prs))
    assert outcomes(capsys)[0] == 0

    rows = query(
        "SELECT pr_number, ci_rollup_at_head, head_sha, review_decision, CAST(reviews AS VARCHAR) "
        "FROM pr_outcomes"
    )
    got = {n: (ci, head, decision, json.loads(r)) for n, ci, head, decision, r in rows}
    assert {n: v[:3] for n, v in got.items()} == {
        1: ("FAILURE", "h1", "CHANGES_REQUESTED"),
        2: (None, "h2", None),
        3: (None, "h3", None),
    }
    assert {(r["author"], r["state"]) for r in got[1][3]} == set(reviews)
    assert got[2][3] == got[3][3] == []


def test_unauthenticated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: FakeGh,
) -> None:
    pr_session(tmp_path, monkeypatch, capsys, [(REPO, 1)])
    fake_gh.scenario(authenticated=False)

    code, error, message = outcomes_error(capsys)

    assert (code, error) == (3, "gh_unavailable")
    assert "not authenticated" in message
    assert stored() == set()


def test_missing_gh_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: FakeGh,
) -> None:
    pr_session(tmp_path, monkeypatch, capsys, [(REPO, 1)])
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    code, error, message = outcomes_error(capsys)
    assert (code, error) == (3, "gh_unavailable")
    assert "not installed" in message


def outcomes_error(capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Exit code, error code and message of a failed run; the message also reaches stderr."""
    code = main(["outcomes", "--json"])
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    Draft202012Validator(load_schema(CLI_SCHEMAS_DIR / "error.schema.json")).validate(doc)
    assert doc["message"] in captured.err
    return code, doc["error"], doc["message"]


def test_a_failed_query_is_a_partial_failure_with_a_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: FakeGh,
) -> None:
    pr_session(tmp_path, monkeypatch, capsys, [(REPO, 1)])
    fake_gh.scenario(graphql_fails=True)

    code, doc = outcomes(capsys)

    assert code == 3
    assert (doc["prs_fetched"], doc["rate_limited"]) == (0, False)
    assert stored() == set()


def test_no_links_means_no_gh_calls(capsys: pytest.CaptureFixture[str], fake_gh: FakeGh) -> None:
    fake_gh.scenario(authenticated=False)
    code, doc = outcomes(capsys)
    assert code == 0, doc
    assert (doc["links_written"], doc["prs_fetched"], doc["rate_remaining"]) == (0, 0, None)
    assert fake_gh.calls() == []
