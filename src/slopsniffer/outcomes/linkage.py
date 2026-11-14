"""Session linkage: tie each session to the repo, branch, PRs, commits and tickets it touched.

``session_links`` is rebuilt from scratch on every run by ``linkage.sql``; ``method`` names the
extractor that produced each row: ``git_branch`` and ``ticket_branch`` (the session's git branch),
``gh_pr_create`` and ``gt_submit`` (PR URLs printed by the command that opened the PR),
``git_commit`` (``[branch sha]`` lines printed by ``git commit``) and ``ticket_mention`` (ids
matching ``outcomes.ticket_pattern`` in user and assistant text, injected context excluded).
The repo of branch and commit links is the GitHub ``owner/name`` of the session cwd's
``origin`` remote, when the cwd still exists.
"""

import json
import re
import subprocess
from importlib import resources
from pathlib import Path

import duckdb

from slopsniffer import analysis
from slopsniffer.ingest.reader import BATCH_ROWS

GIT_TIMEOUT_SECONDS = 10
_GITHUB_REMOTE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def origin_repo(cwd: str) -> str | None:
    """``owner/name`` (lower case) of the GitHub ``origin`` remote of the checkout at ``cwd``."""
    if not Path(cwd).is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    match = _GITHUB_REMOTE.search(proc.stdout.strip()) if proc.returncode == 0 else None
    return f"{match[1]}/{match[2]}".lower() if match else None


def link(
    con: duckdb.DuckDBPyConnection, ticket_pattern: str, *, batch_rows: int = BATCH_ROWS
) -> int:
    """Rebuild ``session_links``; returns the number of links written."""
    cwds = con.execute("SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL").fetchall()
    repos = [(cwd, origin_repo(cwd)) for (cwd,) in cwds]
    sql = resources.files("slopsniffer.outcomes").joinpath("linkage.sql").read_text("utf-8")
    con.execute("CREATE OR REPLACE TEMP TABLE link_repos (cwd VARCHAR, repo VARCHAR)")
    if repos:
        con.executemany("INSERT INTO link_repos VALUES (?, ?)", repos)
    analysis.create_replacement(con, "session_links", "analysis_links")
    try:
        statement = sql.replace("INSERT INTO session_links", "INSERT INTO analysis_links")
        # Branch-only facts need no bodies and are emitted once, including sessions
        # without events. Event-local extractors use physical row/byte spans.
        con.execute(
            analysis.event_scoped(con, statement, 0, 0, sessions_all=True),
            {"ticket_pattern": ticket_pattern},
        )
        batches = analysis.event_batches(con, batch_rows)
        while True:
            with analysis.event_transaction(con, batches) as span:
                if span is None:
                    break
                start, end = span
                _direct_prs(con, start, end)
                con.execute(
                    analysis.event_scoped(
                        con,
                        statement + " HAVING method NOT IN ('git_branch', 'ticket_branch')",
                        start,
                        end,
                    ),
                    {"ticket_pattern": ticket_pattern},
                )
        # Dedupe cross-batch mentions by the original earliest sequence. This
        # spillable aggregate contains scalar identities only, never tool/text bodies.
        analysis.create_replacement(con, "session_links", "analysis_links_final")
        con.execute(
            "INSERT INTO analysis_links_final "
            "SELECT l.session_id, l.repo, l.branch, l.pr_number, l.commit_sha, l.ticket_id, "
            "arg_min(l.source_event_id, (CASE WHEN l.method = 'pr_link' THEN 0 ELSE 1 END, "
            "e.seq)), arg_min(l.method, (CASE WHEN l.method = 'pr_link' THEN 0 ELSE 1 END, "
            "e.seq)) "
            "FROM analysis_links l LEFT JOIN events e ON e.event_id = l.source_event_id "
            "GROUP BY l.session_id, l.repo, l.branch, l.pr_number, l.commit_sha, l.ticket_id, "
            "CASE WHEN l.pr_number IS NULL THEN l.method ELSE 'pr' END"
        )
        # Publish by table swap, so even the final transaction has no body buffers.
        con.begin()
        try:
            con.execute("DROP TABLE session_links")
            con.execute("ALTER TABLE analysis_links_final RENAME TO session_links")
            con.commit()
        except BaseException:
            con.rollback()
            raise
        row = con.execute("SELECT count(*) FROM session_links").fetchone()
    finally:
        con.execute(
            "DROP TABLE IF EXISTS analysis_links; DROP TABLE IF EXISTS analysis_links_final"
        )
        con.execute("DROP TABLE link_repos")
    return int(row[0]) if row else 0


def _direct_prs(con: duckdb.DuckDBPyConnection, start: int, end: int) -> None:
    """Only explicit harness metadata can link directly; pasted URLs cannot."""
    positions = con.execute(
        "SELECT rowid FROM events WHERE rowid >= ? AND rowid < ? AND record_type = 'pr-link'",
        [start, end],
    ).fetchall()
    rows = []
    for (position,) in positions:
        sid, eid, raw = con.execute(
            "SELECT session_id, event_id, parsed::VARCHAR FROM events WHERE rowid = ?",
            [position],
        ).fetchall()[0]
        record = json.loads(raw)
        url = record.get("prUrl")
        if not isinstance(url, str):
            continue
        match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([0-9]+)/?", url)
        if match and sid is not None:
            rows.append((sid, match[1].lower(), int(match[2]), eid))
    if rows:
        con.executemany(
            "INSERT INTO analysis_links (session_id, repo, pr_number, source_event_id, method) "
            "VALUES (?, ?, ?, ?, 'pr_link')",
            rows,
        )
