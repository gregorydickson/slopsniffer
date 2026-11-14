"""Grading test helpers: an in-memory telemetry database filled with synthetic rows."""

from collections.abc import Iterator
from dataclasses import dataclass, field

import duckdb

from slopsniffer.db import apply_schema


def memory_db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    apply_schema(con)
    return con


@dataclass(frozen=True)
class Pr:
    """A pr_outcomes row as outcomes/github.py can write it; NULL columns are None."""

    number: int
    state: str = "OPEN"
    merged: bool = False
    review_decision: str | None = None
    ci: str | None = None
    reverted_by: int | None = None
    repo: str = "acme/widgets"


@dataclass(frozen=True)
class Fixture:
    session_id: str
    severities: tuple[float, ...] = ()
    prs: tuple[Pr, ...] = ()
    cost: float | None = None
    links: tuple[int, ...] = field(default=())


def load(con: duckdb.DuckDBPyConnection, fixtures: Iterator[Fixture] | list[Fixture]) -> None:
    seen_prs: set[tuple[str, int]] = set()
    for i, fx in enumerate(fixtures):
        sid = fx.session_id
        con.execute("INSERT INTO sessions (session_id, harness) VALUES (?, 'claude')", [sid])
        con.execute(
            "INSERT INTO events (event_id, session_id, seq, record_type, kind, source_file_id, "
            "byte_offset, byte_length) VALUES (?, ?, 1, 'user', 'message', 1, 0, 1)",
            [f"ev-{sid}", sid],
        )
        for j, severity in enumerate(fx.severities):
            con.execute(
                "INSERT INTO signals VALUES (?, 'rerun_loop', ?, ?, [], NULL, now())",
                [f"sig-{i}-{j}", sid, severity],
            )
        if fx.cost is not None:
            con.execute("INSERT INTO session_cost VALUES (?, ?, 'cost_state')", [sid, fx.cost])
        else:
            con.execute("INSERT INTO session_cost VALUES (?, NULL, 'unpriced')", [sid])
        for pr in fx.prs:
            for _ in range(1 + fx.links.count(pr.number)):
                con.execute(
                    "INSERT INTO session_links (session_id, repo, pr_number, method) "
                    "VALUES (?, ?, ?, 'branch')",
                    [sid, pr.repo, pr.number],
                )
            if (pr.repo, pr.number) not in seen_prs:
                seen_prs.add((pr.repo, pr.number))
                con.execute(
                    "INSERT INTO pr_outcomes (repo, pr_number, state, merged_at, review_decision,"
                    " ci_rollup_at_head, reverted_by_pr, fetched_at) VALUES "
                    "(?, ?, ?, CASE WHEN ? THEN now() END, ?, ?, ?, now())",
                    [
                        pr.repo,
                        pr.number,
                        pr.state,
                        pr.merged,
                        pr.review_decision,
                        pr.ci,
                        pr.reverted_by,
                    ],
                )
