"""GitHub transport, parsing and freshness contracts with small synthetic inputs."""

import json
import subprocess
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from slopsniffer import outcomes
from slopsniffer.config import load_config
from slopsniffer.outcomes import github
from tests.grading.support import memory_db
from tests.outcomes.support import pr_node

NOW = datetime(2026, 11, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ("code", "stderr", "unavailable"),
    [
        (4, "login required", True),
        (1, "run gh auth login", True),
        (5, "other failure", False),
        (0, "", False),
    ],
)
def test_client_classifies_authentication_without_conflating_other_exits(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    stderr: str,
    unavailable: bool,
) -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if kwargs.get("check") and code:
            raise subprocess.CalledProcessError(code, command)
        return subprocess.CompletedProcess(command, code, "answer", stderr)

    monkeypatch.setattr(subprocess, "run", run)
    client = github.Gh(timeout=0.01)
    if unavailable:
        with pytest.raises(github.GhUnavailable, match="not authenticated"):
            client._run(["api", "graphql"])
    else:
        assert client._run(["api", "graphql"]).returncode == code


def test_client_uses_its_instance_deadline_and_reports_timeout_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        if timeout is not None and timeout < 1:
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, 0, "late output", "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(github.GhFailed) as caught:
        github.Gh(executable="fake-gh", timeout=0.01)._run(["api", "graphql", "--input", "-"])
    assert str(caught.value) == "fake-gh api graphql timed out after 0.01s"


@pytest.mark.parametrize(
    ("body", "code", "expected"),
    [
        ('{"resources":{"graphql":{"remaining":73}}}', 0, 73),
        ('{"resources":{"graphql":{"remaining":73}}}', 1, None),
        ('{"resources":{"graphql":{"remaining":true}}}', 0, None),
        ('{"resources":{"graphql":{"remaining":"73"}}}', 0, None),
        ('{"resources":null}', 0, None),
        ("not JSON", 0, None),
    ],
)
def test_rate_limit_accepts_only_an_integer_from_a_successful_response(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    code: int,
    expected: int | None,
) -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, code, body, "upstream error")

    monkeypatch.setattr(subprocess, "run", run)
    client = github.Gh(timeout=0.01)
    if expected is None:
        with pytest.raises(github.GhFailed) as caught:
            client.rate_remaining()
        assert "upstream error" in str(caught.value)
    else:
        assert client.rate_remaining() == expected


@pytest.mark.parametrize("body", ['{"data":{"p0":null}}', '{"data":[]}', "invalid JSON"])
def test_graphql_transmits_the_query_on_stdin_and_uses_data_even_on_error_exit(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # A fake implementing gh's actual --input - contract, including the failure path.
        valid = command == ["fake-gh", "api", "graphql", "--input", "-"]
        valid = valid and kwargs.get("input") == json.dumps({"query": "query { sample }"})
        return subprocess.CompletedProcess(command, 1, body if valid else "", "request failed")

    monkeypatch.setattr(subprocess, "run", run)
    client = github.Gh(executable="fake-gh", timeout=0.01)
    if body.startswith('{"data":{"'):
        assert client.graphql("query { sample }") == {"p0": None}
    else:
        with pytest.raises(github.GhFailed) as caught:
            client.graphql("query { sample }")
        assert str(caught.value) == "gh api graphql failed: request failed"


def test_search_query_contains_case_sensitive_graphql_fields_for_every_repo() -> None:
    assert github.revert_search_query(["acme/widgets", "acme/gadgets"]) == (
        "query { s0: search(type: ISSUE, first: 100, "
        'query: "repo:acme/widgets is:pr is:merged revert") '
        "{ nodes { ... on PullRequest { number title body mergedAt } } } "
        's1: search(type: ISSUE, first: 100, query: "repo:acme/gadgets is:pr is:merged revert") '
        "{ nodes { ... on PullRequest { number title body mergedAt } } } }"
    )
    assert github.revert_search_query([]) == "query {  }"


@pytest.mark.parametrize("state", ["SUCCESS", 42, None])
def test_outcome_rows_preserve_review_timestamps_and_validate_ci_states(state: object) -> None:
    node: dict[str, Any] = {
        "title": "Change",
        "state": "OPEN",
        "mergedAt": None,
        "reviewDecision": "APPROVED",
        "headRefOid": "head",
        "commits": {"nodes": [{"commit": {"oid": "head", "statusCheckRollup": {"state": state}}}]},
        "reviews": {
            "nodes": [
                {
                    "author": {"login": "alice"},
                    "state": "APPROVED",
                    "submittedAt": "2026-11-09T12:00:00Z",
                },
                None,
            ]
        },
    }
    row = github.outcome_row(("acme/widgets", 17), node, NOW.isoformat())
    assert row[:6] == ("acme/widgets", 17, "Change", "OPEN", None, "APPROVED")
    assert json.loads(str(row[6])) == [
        {"author": "alice", "state": "APPROVED", "submitted_at": "2026-11-09T12:00:00Z"}
    ]
    assert row[7:] == (state if isinstance(state, str) else None, "head", NOW.isoformat())


def test_revert_parsing_rejects_nontext_bodies_and_equal_merge_times() -> None:
    target = "Add widgets"
    assert github.reverts({"body": "Reverts #17"}, "acme/widgets", 17, target)
    assert not github.reverts({"body": None}, "acme/widgets", 17, target)
    assert not github.reverts({"body": "Reverts other/repo#17"}, "acme/widgets", 17, target)
    candidates = [pr_node(18, f'Revert "{target}"', "MERGED", merged_at=NOW.isoformat())]
    assert github.reverted_by("acme/widgets", 17, target, NOW.timestamp(), candidates) is None
    assert github.reverted_by("acme/widgets", 17, target, NOW.timestamp() - 1, candidates) == 18


@dataclass(frozen=True)
class StubGh(github.Gh):
    queries: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def rate_remaining(self) -> int:
        return 5000

    def graphql(self, query: str) -> dict[str, Any]:
        self.queries.append(query)
        return self.data


def test_open_prs_expire_at_the_exact_ttl_and_public_defaults_keep_fresh_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config()
    config = replace(config, outcomes=replace(config.outcomes, open_pr_ttl_hours=2))
    keys = {("acme/widgets", n) for n in (1, 2)}
    client = StubGh(timeout=0.01, data={"p0": {"pullRequest": pr_node(1)}})
    with memory_db() as con:
        for repo, number in keys:
            con.execute(
                "INSERT INTO session_links (session_id, repo, pr_number, method) "
                "VALUES ('s', ?, ?, 'synthetic')",
                [repo, number],
            )
            fetched = NOW - timedelta(hours=2) if number == 1 else NOW
            con.execute(
                "INSERT INTO pr_outcomes (repo, pr_number, state, fetched_at) "
                "VALUES (?, ?, 'OPEN', ?)",
                [repo, number, fetched],
            )
        fetcher = github.Fetcher(con, config.outcomes, client, NOW)
        assert set(fetcher.due(False)) == {("acme/widgets", 1)}
        con.execute("UPDATE pr_outcomes SET fetched_at = ?", [NOW])
        assert github.refresh(con, config.outcomes, client, NOW).prs_cached == len(keys)
        assert client.queries == []

        def linked(connection: object, ticket_pattern: str, *, batch_rows: int) -> int:
            assert batch_rows == config.ingest.batch_rows
            return len(keys)

        monkeypatch.setattr(outcomes.linkage, "link", linked)
        summary, failure = outcomes.run(con, config, NOW, gh=client)
        assert summary["prs_cached"] == len(keys)
        assert failure is None
        assert client.queries == []
        assert set(github.Fetcher(con, config.outcomes, client, NOW).due(True)) == keys


def test_fetch_rejects_malformed_pr_nodes_and_preserves_upstream_failure_text() -> None:
    client = StubGh(timeout=0.01, data={"p0": {"pullRequest": "bad node"}})
    with memory_db() as con:
        con.execute(
            "INSERT INTO session_links (session_id, repo, pr_number, method) "
            "VALUES ('s', 'acme/widgets', 1, 'synthetic')"
        )
        result = github.refresh(con, load_config().outcomes, client, NOW)
        assert result.failure is None
        assert con.execute("SELECT repo, pr_number, state FROM pr_outcomes").fetchall() == [
            ("acme/widgets", 1, "NOT_FOUND")
        ]

    class BrokenGh(StubGh):
        def rate_remaining(self) -> int:
            raise github.GhFailed("upstream unavailable")

    with memory_db() as con:
        con.execute(
            "INSERT INTO session_links (session_id, repo, pr_number, method) "
            "VALUES ('s', 'acme/widgets', 1, 'synthetic')"
        )
        result = github.refresh(con, load_config().outcomes, BrokenGh(timeout=0.01), NOW)
        assert result.failure == "upstream unavailable"
        assert con.execute("SELECT repo, pr_number FROM pr_outcomes").fetchall() == []


def test_spooled_reverts_choose_lowest_number_across_candidates() -> None:
    client = StubGh(
        timeout=0.01,
        data={
            "s0": {
                "nodes": [
                    {"number": number, "body": "Reverts #17", "mergedAt": NOW.isoformat()}
                    for number in (24, 19, 22)
                ]
            }
        },
    )
    with memory_db() as con:
        con.execute(
            "INSERT INTO pr_outcomes (repo,pr_number,state,merged_at,fetched_at) "
            "VALUES ('acme/widgets',17,'MERGED',?,?)",
            [NOW - timedelta(days=1), NOW],
        )
        fetcher = github.Fetcher(con, load_config().outcomes, client, NOW)
        fetcher.detect_reverts()
        assert con.execute("SELECT reverted_by_pr FROM pr_outcomes").fetchall() == [(19,)]


def test_successful_fetches_spool_complete_candidates_and_keep_batch_totals() -> None:
    config = replace(load_config().outcomes, pr_batch_size=1)
    candidate = pr_node(
        22, 'Revert "Target"', "MERGED", merged_at=NOW.isoformat(), body="Reverts #17"
    )
    client = StubGh(timeout=0.01, data={"p0": {"pullRequest": candidate}})
    with memory_db() as con:
        fetcher = github.Fetcher(con, config, client, NOW)
        requested = [("acme/widgets", 22), ("acme/other", 22)]
        fetcher.fetch(requested)
        assert fetcher.result.prs_fetched == len(requested)
        assert {
            tuple(row)
            for row in con.execute("SELECT repo,pr_number,state FROM pr_outcomes").fetchall()
        } == {(repo, number, "MERGED") for repo, number in requested}
        assert [
            (repo, json.loads(raw))
            for repo, raw in con.execute(
                "SELECT repo,node FROM github_candidates ORDER BY repo"
            ).fetchall()
        ] == [
            (repo, {key: candidate[key] for key in ("number", "title", "body", "mergedAt")})
            for repo, _ in sorted(requested)
        ]
        assert all("pullRequest(number: 22)" in query for query in client.queries)
