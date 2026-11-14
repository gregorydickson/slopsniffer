"""Resolve scope and normalize evidence through the cluster entry point."""

import pytest

from slopsniffer.grading.grader import SessionVerdict, _store_grade
from slopsniffer.grading.rubrics import load_rubrics
from slopsniffer.proposals.clusters import Cluster, Failure, Surface, clusters
from tests.grading.support import memory_db


@pytest.mark.parametrize(
    ("cwd", "project_repo", "expected"),
    [
        ("/repo/sub", "/repo", "/z-project"),
        ("/elsewhere", "/repo", "/b-global"),
        (None, "/repo", "/b-global"),
        ("/repo", None, "/b-global"),
    ],
)
def test_cluster_uses_the_narrowest_applicable_skill_scope(
    cwd: str | None,
    project_repo: str | None,
    expected: str,
) -> None:
    rubrics = load_rubrics()
    with memory_db() as con:
        con.execute(
            "INSERT INTO sessions (session_id, harness, cwd) VALUES ('s', 'claude', ?)", [cwd]
        )
        con.execute("INSERT INTO skill_invocations VALUES ('e', 's', 'review', 'skill_tool', NULL)")
        for path, scope, repo in [
            ("/a-plugin", "plugin", None),
            ("/b-global", "global", None),
            ("/z-project", "project", project_repo),
        ]:
            con.execute(
                "INSERT INTO skills (path, name, scope, repo, content_hash) "
                "VALUES (?, 'review', ?, ?, 'synthetic')",
                [path, scope, repo],
            )
        _store_grade(
            con, "s", rubrics["code-quality"], SessionVerdict("block", 0, "Skipped tests", [])
        )
        assert clusters(con, rubrics) == [
            Cluster(
                Surface("review", expected),
                "code-quality",
                "skipped tests",
                (Failure("s", "code-quality", "block", 0, "Skipped tests", ()),),
            )
        ]
        con.execute("DELETE FROM sessions")
        assert clusters(con, rubrics)[0].surface == Surface("review", "/b-global")
        con.execute("DELETE FROM skill_invocations")
        assert clusters(con, rubrics)[0].rejection() == (
            "no owning skill or rule file could be identified for these sessions"
        )


def test_cluster_root_causes_remove_cited_ids_but_preserve_other_words_and_null_reasons() -> None:
    rubrics = load_rubrics()
    with memory_db() as con:
        for sid, reason, cited in [
            ("a", "The agent skipped TESTS at EVIDENCE-A", ["evidence-a"]),
            ("b", "Tests skipped", []),
            ("c", None, []),
        ]:
            con.execute("INSERT INTO sessions (session_id, harness) VALUES (?, 'claude')", [sid])
            _store_grade(
                con, sid, rubrics["code-quality"], SessionVerdict("block", 0, reason, cited)
            )
        found = clusters(con, rubrics)
        assert {c.root_cause: c.session_ids for c in found} == {
            "skipped tests": ["a", "b"],
            "": ["c"],
        }
        assert {f.session_id: f.reason for c in found for f in c.failures} == {
            "a": "The agent skipped TESTS at EVIDENCE-A",
            "b": "Tests skipped",
            "c": "",
        }
