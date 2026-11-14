"""PR outcomes from GitHub through ``gh``, spending as little of the GraphQL quota as possible.

Every linked ``(repo, pr_number)`` is fetched at most once per state: merged, closed and
not-found PRs are final and never refetched; open PRs refetch once their row is older than
``open_pr_ttl_hours`` (or on ``--refresh``). Due PRs go out in aliased GraphQL queries of at
most :data:`MAX_BATCH` PRs, and ``gh api rate_limit`` is read before every query: when the
GraphQL quota left is below ``rate_reserve`` the fetch stops cleanly with ``rate_limited``.

A merged PR is ``reverted_by_pr`` the lowest-numbered later-merged PR in the same repo whose
title is ``Revert "<its title>"`` or whose body says it reverts its number. Candidates are the
PRs fetched in this run plus one ``revert`` search per repo with unreverted merged PRs.
"""

import json
import re
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import duckdb

from slopsniffer.config import OutcomesConfig

MAX_BATCH = 50
SEARCH_FIRST = 100
GH_TIMEOUT_SECONDS = 120
NOT_FOUND = "NOT_FOUND"
FINAL_STATES = frozenset({"MERGED", "CLOSED", NOT_FOUND})

_PR_FIELDS = (
    "number title body state mergedAt reviewDecision headRefOid "
    "reviews(first: 100) { nodes { author { login } state submittedAt } } "
    "commits(last: 1) { nodes { commit { oid statusCheckRollup { state } } } }"
)
_REVERT_REF = re.compile(
    r"\breverts?:?\s+(?:https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/pull/"
    r"|(?P<ref_repo>[\w.-]+/[\w.-]+)?#)(?P<number>\d+)\b",
    re.IGNORECASE,
)

# Due work is narrow PR identities/state/timestamps; never reviews or PR bodies.
_REQUESTED = """
SELECT l.repo, l.pr_number, o.state, epoch(o.fetched_at)
FROM (SELECT DISTINCT repo, pr_number FROM session_links
      WHERE repo IS NOT NULL AND pr_number IS NOT NULL) l
LEFT JOIN pr_outcomes o USING (repo, pr_number)
ORDER BY l.repo, l.pr_number
"""

_UPSERT = """
MERGE INTO pr_outcomes AS stored
USING (VALUES (?, ?, ?, ?, CAST(? AS TIMESTAMPTZ), ?, CAST(? AS JSON), ?, ?, NULL,
               CAST(? AS TIMESTAMPTZ))) AS incoming (repo, pr_number, title, state, merged_at,
    review_decision, reviews, ci_rollup_at_head, head_sha, reverted_by_pr, fetched_at)
ON stored.repo = incoming.repo AND stored.pr_number = incoming.pr_number
WHEN MATCHED THEN UPDATE SET title = incoming.title, state = incoming.state,
    merged_at = incoming.merged_at, review_decision = incoming.review_decision,
    reviews = incoming.reviews, ci_rollup_at_head = incoming.ci_rollup_at_head,
    head_sha = incoming.head_sha, reverted_by_pr = incoming.reverted_by_pr,
    fetched_at = incoming.fetched_at
WHEN NOT MATCHED THEN INSERT BY NAME
"""

_UNREVERTED = """
SELECT repo, pr_number, title, epoch(merged_at) FROM pr_outcomes
WHERE state = 'MERGED' AND merged_at IS NOT NULL AND reverted_by_pr IS NULL
ORDER BY repo, pr_number
"""

type PrKey = tuple[str, int]


class GhUnavailable(Exception):
    """``gh`` is missing or not authenticated; nothing can be fetched."""


class GhFailed(Exception):
    """A ``gh`` call failed or returned something unreadable."""


@dataclass(frozen=True)
class Gh:
    executable: str = "gh"
    timeout: float = GH_TIMEOUT_SECONDS

    def _run(
        self, args: Sequence[str], stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = [self.executable, *args]
        try:
            proc = subprocess.run(
                command,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError:
            raise GhUnavailable(f"{self.executable} is not installed or not on PATH") from None
        except subprocess.TimeoutExpired:
            raise GhFailed(f"{' '.join(command[:3])} timed out after {self.timeout}s") from None
        # gh exits 4 when the command needs authentication.
        if proc.returncode == 4 or "gh auth login" in proc.stderr:
            raise GhUnavailable(f"gh is not authenticated: {proc.stderr.strip()}")
        return proc

    def rate_remaining(self) -> int:
        """GraphQL points left in the current rate-limit window."""
        proc = self._run(["api", "rate_limit"])
        try:
            remaining = json.loads(proc.stdout)["resources"]["graphql"]["remaining"]
        except ValueError, KeyError, TypeError:
            raise GhFailed(f"unreadable gh api rate_limit output: {proc.stderr.strip()}") from None
        if proc.returncode != 0 or type(remaining) is not int:
            raise GhFailed(f"gh api rate_limit failed: {proc.stderr.strip()}")
        return remaining

    def graphql(self, query: str) -> dict[str, Any]:
        """The query's ``data``; aliases GitHub could not resolve are null. gh exits non-zero
        when the response carries errors, so a response with ``data`` is used regardless."""
        proc = self._run(["api", "graphql", "--input", "-"], json.dumps({"query": query}))
        try:
            doc = json.loads(proc.stdout)
        except ValueError:
            doc = None
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, dict):
            raise GhFailed(f"gh api graphql failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return data


@dataclass
class FetchResult:
    prs_fetched: int = 0
    prs_cached: int = 0
    rate_limited: bool = False
    rate_remaining: int | None = None
    failure: str | None = None


def pr_query(prs: Sequence[PrKey]) -> str:
    """One aliased query fetching every PR in ``prs``."""
    parts = []
    for i, (repo, number) in enumerate(prs):
        owner, _, name = repo.partition("/")
        parts.append(
            f"p{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
            f"{{ pullRequest(number: {number}) {{ ...pr }} }}"
        )
    return f"query {{ {' '.join(parts)} }} fragment pr on PullRequest {{ {_PR_FIELDS} }}"


def revert_search_query(repos: Sequence[str]) -> str:
    """One aliased query searching each repo's merged PRs that mention a revert."""
    parts = [
        f"s{i}: search(type: ISSUE, first: {SEARCH_FIRST}, "
        f"query: {json.dumps(f'repo:{repo} is:pr is:merged revert')}) "
        "{ nodes { ... on PullRequest { number title body mergedAt } } }"
        for i, repo in enumerate(repos)
    ]
    return f"query {{ {' '.join(parts)} }}"


def _chunks[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def ci_rollup_at_head(node: Mapping[str, Any]) -> str | None:
    """The status-check rollup of the PR's head commit; None when the last commit is not the
    head or has no checks."""
    last = ((node.get("commits") or {}).get("nodes") or [None])[-1] or {}
    commit = last.get("commit") or {}
    if commit.get("oid") is None or commit["oid"] != node.get("headRefOid"):
        return None
    state = (commit.get("statusCheckRollup") or {}).get("state")
    return state if isinstance(state, str) else None


def _reviews(node: Mapping[str, Any]) -> str:
    nodes = (node.get("reviews") or {}).get("nodes") or []
    return json.dumps(
        [
            {
                "author": (r.get("author") or {}).get("login"),
                "state": r.get("state"),
                "submitted_at": r.get("submittedAt"),
            }
            for r in nodes
            if isinstance(r, dict)
        ]
    )


def outcome_row(key: PrKey, node: Mapping[str, Any] | None, fetched_at: str) -> tuple[object, ...]:
    repo, number = key
    if node is None:
        return (repo, number, None, NOT_FOUND, None, None, "[]", None, None, fetched_at)
    return (
        repo,
        number,
        node.get("title"),
        node.get("state"),
        node.get("mergedAt"),
        node.get("reviewDecision"),
        _reviews(node),
        ci_rollup_at_head(node),
        node.get("headRefOid"),
        fetched_at,
    )


def _epoch(iso: object) -> float | None:
    if not isinstance(iso, str):
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def reverts(candidate: Mapping[str, Any], repo: str, number: int, title: str | None) -> bool:
    """True when ``candidate`` is, by title or body, a revert of PR ``repo#number``."""
    if title is not None and candidate.get("title") == f'Revert "{title}"':
        return True
    body = candidate.get("body")
    if not isinstance(body, str):
        return False
    for match in _REVERT_REF.finditer(body):
        ref_repo = match["url_repo"] or match["ref_repo"]
        if int(match["number"]) == number and (ref_repo is None or ref_repo.lower() == repo):
            return True
    return False


def reverted_by(
    repo: str,
    number: int,
    title: str | None,
    merged_at: float,
    candidates: Sequence[Mapping[str, Any]],
) -> int | None:
    """The lowest-numbered candidate merged after PR ``repo#number`` that reverts it."""
    found = [
        c["number"]
        for c in candidates
        if isinstance(c.get("number"), int)
        and c["number"] != number
        and (when := _epoch(c.get("mergedAt"))) is not None
        and when > merged_at
        and reverts(c, repo, number, title)
    ]
    return min(found, default=None)


class Fetcher:
    def __init__(
        self, con: duckdb.DuckDBPyConnection, config: OutcomesConfig, gh: Gh, now: datetime
    ) -> None:
        self.con, self.config, self.gh, self.now = con, config, gh, now
        self.result = FetchResult()
        # Candidate bodies live in a spillable temp table, never a run-sized dict.
        con.execute("CREATE OR REPLACE TEMP TABLE github_candidates (repo VARCHAR, node VARCHAR)")

    def _quota_ok(self) -> bool:
        remaining = self.gh.rate_remaining()
        self.result.rate_remaining = remaining
        if remaining < self.config.rate_reserve:
            self.result.rate_limited = True
        return not self.result.rate_limited

    def due(self, refresh: bool) -> list[PrKey]:
        ttl = self.config.open_pr_ttl_hours * 3600
        now = self.now.timestamp()
        due: list[PrKey] = []
        for repo, number, state, fetched_at in self.con.execute(_REQUESTED).fetchall():
            fresh_open = state == "OPEN" and not refresh and now - fetched_at < ttl
            if state in FINAL_STATES or fresh_open:
                self.result.prs_cached += 1
            else:
                due.append((repo, number))
        return due

    def fetch(self, due: Sequence[PrKey]) -> None:
        fetched_at = self.now.isoformat()
        for batch in _chunks(due, min(self.config.pr_batch_size, MAX_BATCH)):
            if not self._quota_ok():
                return
            data = self.gh.graphql(pr_query(batch))
            rows = []
            for i, key in enumerate(batch):
                node = (data.get(f"p{i}") or {}).get("pullRequest")
                node = node if isinstance(node, dict) else None
                rows.append(outcome_row(key, node, fetched_at))
                if node is not None and node.get("state") == "MERGED":
                    self.con.execute(
                        "INSERT INTO github_candidates VALUES (?, ?)",
                        [
                            key[0],
                            json.dumps(
                                {k: node.get(k) for k in ("number", "title", "body", "mergedAt")}
                            ),
                        ],
                    )
            self.con.executemany(_UPSERT, rows)
            self.result.prs_fetched += len(rows)

    def detect_reverts(self) -> None:
        # PR identities, title (GitHub caps its length), timestamp: narrow facts.
        targets = self.con.execute(_UNREVERTED).fetchall()
        repos = sorted({repo for repo, *_ in targets})
        for chunk in _chunks(repos, MAX_BATCH):
            if not self._quota_ok():
                return
            data = self.gh.graphql(revert_search_query(chunk))
            for i, repo in enumerate(chunk):
                nodes = (data.get(f"s{i}") or {}).get("nodes") or []
                for node in nodes:
                    if isinstance(node, dict):
                        self.con.execute(
                            "INSERT INTO github_candidates VALUES (?, ?)", [repo, json.dumps(node)]
                        )
        for repo, number, title, merged_at in targets:
            # Decode one candidate body at a time. The running minimum preserves
            # the original lowest-numbered revert across responses and batches.
            found: int | None = None
            for (raw,) in self.con.sql(
                "SELECT node FROM github_candidates WHERE repo = $1", params=[repo]
            ).iter_rows(batch_size=1):
                by = reverted_by(repo, number, title, merged_at, [json.loads(raw)])
                if by is not None:
                    found = by if found is None else min(found, by)
            if found is not None:
                self.con.execute(
                    "UPDATE pr_outcomes SET reverted_by_pr = ? WHERE repo = ? AND pr_number = ?",
                    [found, repo, number],
                )


def refresh(
    con: duckdb.DuckDBPyConnection,
    config: OutcomesConfig,
    gh: Gh,
    now: datetime,
    *,
    refresh_open: bool = False,
) -> FetchResult:
    """Fetch due PR outcomes and detect reverts. A failed ``gh`` call stops the run and is
    reported in ``failure``; :class:`GhUnavailable` propagates."""
    fetcher = Fetcher(con, config, gh, now)
    try:
        fetcher.fetch(fetcher.due(refresh_open))
        if not fetcher.result.rate_limited:
            fetcher.detect_reverts()
    except GhFailed as exc:
        fetcher.result.failure = str(exc)
    finally:
        con.execute("DROP TABLE github_candidates")
    return fetcher.result
