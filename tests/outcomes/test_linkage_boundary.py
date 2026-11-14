"""Linkage subprocess failures and replacement atomicity."""

import subprocess
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import duckdb
import pytest

from slopsniffer.outcomes import linkage
from tests.grading.support import memory_db
from tests.helpers import REPO_ROOT


@pytest.mark.parametrize(
    ("stdout", "code", "hang", "expected"),
    [
        ("git@github.com:Acme/Widgets.git\n", 0, False, "acme/widgets"),
        ("git@github.com:Acme/Widgets.git\n", 1, False, None),
        ("git@github.com:Acme/Widgets.git\n", 0, True, None),
        ("https://gitlab.com/acme/widgets.git", 0, False, None),
    ],
)
def test_origin_lookup_handles_timeout_nonzero_and_non_github_remotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    code: int,
    hang: bool,
    expected: str | None,
) -> None:
    monkeypatch.setattr(linkage, "GIT_TIMEOUT_SECONDS", 0.01)

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # Model case-sensitive executable lookup on every host filesystem.
        if command[0] != "git":
            raise FileNotFoundError(command[0])
        if hang and kwargs.get("timeout") is not None:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if code and kwargs.get("check"):
            raise subprocess.CalledProcessError(code, command)
        return subprocess.CompletedProcess(command, code, stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    assert linkage.origin_repo(str(tmp_path)) == expected


@pytest.mark.parametrize("branch", ["feature/acme-17", None])
def test_link_rebuild_reads_the_exact_resource_and_returns_the_written_rows(
    monkeypatch: pytest.MonkeyPatch,
    non_utf8_text_defaults: None,
    branch: str | None,
) -> None:
    files = resources.files

    class StrictPackage:
        def __init__(self, root: Traversable) -> None:
            self.root = root

        def joinpath(self, name: str) -> Traversable:
            children = {p.name: p for p in self.root.iterdir()}
            if name not in children:
                raise FileNotFoundError(name)
            return children[name]

    def strict_files(anchor: str | None = None) -> StrictPackage:
        return StrictPackage(files(anchor or "slopsniffer.outcomes"))

    with memory_db() as con:
        monkeypatch.setattr(resources, "files", strict_files)
        monkeypatch.setattr(linkage, "origin_repo", lambda cwd: "acme/widgets")
        con.execute(
            "INSERT INTO sessions (session_id, harness, cwd, git_branch) "
            "VALUES ('s', 'claude', '/synthetic/repo', ?)",
            [branch],
        )
        con.execute("INSERT INTO session_links (session_id, method) VALUES ('stale', 'old')")
        written = linkage.link(con, r"ACME-\d+")
        expected = (
            {
                ("s", "acme/widgets", branch, None, "git_branch"),
                ("s", None, None, "ACME-17", "ticket_branch"),
            }
            if branch
            else set()
        )
        assert (
            set(
                con.execute(
                    "SELECT session_id, repo, branch, ticket_id, method FROM session_links"
                ).fetchall()
            )
            == expected
        )
        assert written == len(expected)


def test_linkage_sql_failure_keeps_the_old_links_and_closes_the_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenResource:
        def joinpath(self, name: str) -> Path:
            return REPO_ROOT / "tests/fixtures/outcomes/sql/invalid-linkage.sql"

    with memory_db() as con:
        con.execute("INSERT INTO session_links (session_id, method) VALUES ('old', 'synthetic')")
        previous = con.execute("SELECT session_id, method FROM session_links").fetchall()
        monkeypatch.setattr(resources, "files", lambda anchor: BrokenResource())
        with pytest.raises(duckdb.CatalogException, match="nonexistent_linkage_input"):
            linkage.link(con, "")
        assert con.execute("SELECT session_id, method FROM session_links").fetchall() == previous
        con.execute("BEGIN TRANSACTION")
        con.execute("ROLLBACK")
