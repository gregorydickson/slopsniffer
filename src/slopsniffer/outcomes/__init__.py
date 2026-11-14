"""L3 outcomes: link sessions to repos, branches, PRs, commits and tickets, then fetch the
linked PRs' outcomes from GitHub."""

from datetime import datetime

import duckdb

from slopsniffer.config import Config
from slopsniffer.contracts import OutcomesSummary
from slopsniffer.outcomes import github, linkage
from slopsniffer.outcomes.github import Gh, GhUnavailable


def run(
    con: duckdb.DuckDBPyConnection,
    config: Config,
    now: datetime,
    *,
    refresh_open: bool = False,
    gh: Gh | None = None,
) -> tuple[OutcomesSummary, str | None]:
    """Rebuild links, refresh PR outcomes; returns the summary and the failure, if a ``gh``
    call failed part-way. Raises :class:`GhUnavailable` when ``gh`` cannot be used at all."""
    links = linkage.link(con, config.outcomes.ticket_pattern, batch_rows=config.ingest.batch_rows)
    fetched = github.refresh(con, config.outcomes, gh or Gh(), now, refresh_open=refresh_open)
    summary: OutcomesSummary = {
        "links_written": links,
        "prs_fetched": fetched.prs_fetched,
        "prs_cached": fetched.prs_cached,
        "rate_limited": fetched.rate_limited,
        "rate_remaining": fetched.rate_remaining,
    }
    return summary, fetched.failure


__all__ = ["GhUnavailable", "run"]
