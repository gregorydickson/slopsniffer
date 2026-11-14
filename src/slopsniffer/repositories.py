"""Repository identities derived on read; no migration or history rewrite required.

Only session-meta bodies are decoded, one shared row/byte batch at a time. Git
resolution retains at most one page of paths, and SQL aggregates narrow identities.
"""

import json
import re
import subprocess
from importlib import resources
from pathlib import Path

import duckdb

from slopsniffer.ingest.reader import BATCH_BYTES, BATCH_ROWS


def normalize(url: str) -> str:
    """Unify HTTPS, SSH and scp remote spellings without retaining credentials."""
    value = re.sub(r"^[\w+.-]+://(?:[^/@]+@)?", "", url.strip()).rstrip("/")
    value = re.sub(r"^[^/@]+@([^/:]+):", r"\1/", value)
    return value.removesuffix(".git")


def from_path(cwd: str) -> tuple[str, str] | None:
    path = Path(cwd)
    if not path.is_dir():
        return None
    try:
        common = subprocess.run(
            [
                "git",
                "-C",
                cwd,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                "--show-toplevel",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        common, worktree = common.splitlines()
        remote = subprocess.run(
            ["git", "--git-dir", common, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except OSError, ValueError, subprocess.SubprocessError:
        return None
    return (normalize(remote) if remote else str(Path(common).resolve()), worktree)


def install(con: duckdb.DuckDBPyConnection) -> None:
    """Refresh the shared project view for every reader, including raw SQL queries.

    Recorded Codex repository URLs win over filesystem resolution. Unknown paths
    retain their full cwd, so unrelated repositories with the same basename never
    merge. Resolved common directories unify worktrees even without a remote.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE repository_paths "
        "(cwd VARCHAR, identity VARCHAR, worktree VARCHAR)"
    )
    last = ""
    while page := con.execute(
        "SELECT DISTINCT cwd FROM sessions WHERE cwd > ? ORDER BY cwd LIMIT ?",
        [last, BATCH_ROWS],
    ).fetchall():
        values = [(cwd, *(from_path(cwd) or (None, None))) for (cwd,) in page]
        con.executemany("INSERT INTO repository_paths VALUES (?, ?, ?)", values)
        last = page[-1][0]
    con.execute(
        "CREATE OR REPLACE TEMP TABLE repository_metadata "
        "(session_id VARCHAR, identity VARCHAR, seq BIGINT)"
    )
    last_row = -1
    while positions := con.execute(
        "SELECT rowid, byte_length FROM events WHERE rowid > ? "
        "AND record_type='session_meta' ORDER BY rowid LIMIT ?",
        [last_row, BATCH_ROWS],
    ).fetchall():
        size = 0
        bounded: list[int] = []
        for position, length in positions:
            if bounded and size + length > BATCH_BYTES:
                break
            bounded.append(position)
            size += length
        values_meta = []
        for sid, seq, raw in con.sql(
            "SELECT session_id, seq, parsed::VARCHAR FROM events "
            "WHERE rowid IN (SELECT unnest($1::BIGINT[]))",
            params=[bounded],
        ).iter_rows(batch_size=1):
            record = json.loads(raw or "{}")
            payload = record.get("payload", {})
            git = payload.get("git", {}) if isinstance(payload, dict) else {}
            url = git.get("repository_url") if isinstance(git, dict) else None
            if isinstance(url, str) and url.strip() and seq is not None:
                values_meta.append((sid, normalize(url), seq))
        if values_meta:
            con.executemany("INSERT INTO repository_metadata VALUES (?, ?, ?)", values_meta)
        last_row = bounded[-1]
    con.execute("""
CREATE OR REPLACE TEMP VIEW session_projects AS
SELECT s.* EXCLUDE(project),
       coalesce(m.identity, p.identity, s.cwd, s.project) AS project,
       coalesce(p.worktree, s.cwd, s.project) AS worktree
FROM sessions s
LEFT JOIN (SELECT session_id, arg_min(identity, seq) AS identity
           FROM repository_metadata GROUP BY session_id) m USING(session_id)
LEFT JOIN repository_paths p USING(cwd)
""")
    # Older databases retain the prior rollup definitions. Shadow them in this
    # connection so read-only reports and SQL queries use the same current policy.
    if con.execute(
        "SELECT 1 FROM duckdb_functions() WHERE function_name='rollup' LIMIT 1"
    ).fetchone():
        sql = resources.files("slopsniffer.signals").joinpath("rollups.sql").read_text("utf-8")
        con.execute(
            sql.replace("CREATE OR REPLACE VIEW", "CREATE OR REPLACE TEMP VIEW").replace(
                "CREATE OR REPLACE MACRO", "CREATE OR REPLACE TEMP MACRO"
            )
        )
