"""Public synthetic contracts for grouping, repository identity and skill examples."""

import json
import re
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest

from slopsniffer import report, repositories, status
from slopsniffer.cli import build_parser, main
from slopsniffer.grading.rubrics import load_rubrics
from slopsniffer.ingest.skills import frontmatter
from slopsniffer.report import findings, html
from slopsniffer.signals import runner
from tests.helpers import REPO_ROOT
from tests.signals import support
from tests.test_value_contracts import database, event

SKILL = REPO_ROOT / "skills/slopsniffer/SKILL.md"
QUERIES = SKILL.parent / "references/queries.md"


def test_skill_frontmatter_and_every_documented_command_parse() -> None:
    text = SKILL.read_text()
    fields = frontmatter(text)
    assert fields["name"] == "slopsniffer"
    assert "agent" in fields["description"]
    assert "Do not use" in fields["description"]
    parser = build_parser()
    commands = []
    for document in (text, QUERIES.read_text()):
        commands.extend(re.findall(r"`(slopsniffer [^`\n]+)`", document))
        for block in re.findall(r"```bash\n(.*?)```", document, re.DOTALL):
            commands.extend(line for line in block.splitlines() if line.startswith("slopsniffer "))
    for command in commands:
        words = shlex.split(command)
        stop = next((i for i, word in enumerate(words) if word in (">", "2>", "&")), len(words))
        parser.parse_args(words[1:stop])
    assert {shlex.split(command)[1] for command in commands} == {
        "ingest",
        "signals",
        "report",
        "status",
        "query",
        "rehydrate",
        "run",
        "grade",
        "propose",
    }


def test_skill_queries_run_on_fixture_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    support.corpus(tmp_path, monkeypatch, capsys)
    support.run(capsys)
    queries = re.findall(r"```sql\n(.*?)```", QUERIES.read_text(), re.DOTALL)
    results = []
    for sql in queries:
        assert re.search(r"\bLIMIT \d+", sql)
        assert main(["query", "--json", sql]) == 0
        results.append(json.loads(capsys.readouterr().out))
    assert {row[0] for row in results[0]["rows"]} <= support.session_ids()
    assert results[0]["rows"]
    assert results[1]["rows"]
    assert results[2]["rows"]
    assert results[3]["rows"]
    assert results[4]["rows"]
    # Grading isn't run without approval; an empty receipt query is expected.
    assert results[5]["columns"] == [
        "call_id",
        "backend",
        "input_tokens",
        "output_tokens",
        "status",
        "error",
    ]


@pytest.mark.parametrize(
    "command",
    [
        "echo ready",
        "echo 'literal $VALUE'",
        'echo "literal > text"',
        "true",
        ":",
        "sleep 0.2",
        "false",
        "sleep 2s",
    ],
)
def test_literal_noops_are_state_independent(command: str) -> None:
    assert re.fullmatch(runner.NOOP_COMMAND, command)
    assert findings.subject("rerun_loop", {"command": command}) == "idle no-op polling"


@pytest.mark.parametrize(
    "command",
    [
        "echo $VALUE",
        'echo "$VALUE"',
        "echo > file",
        "echo ok >> file",
        "echo $(date)",
        "echo `date`",
        "echo *",
        "echo ~",
        "true && rm file",
        "sleep $DELAY",
        "git status",
        "printf -v output x",
        "printf '%(%s)T' -1",
        "printf '%n' variable",
        "echo ready; :",
        "echo ready\nrm file",
        "cat state",
        "echo ${VALUE}",
    ],
)
def test_state_reads_writes_and_real_work_are_not_noops(command: str) -> None:
    assert not re.fullmatch(runner.NOOP_COMMAND, command)
    assert findings.subject("rerun_loop", {"command": command}) == command


def signal(con: Any, identity: str, sid: str, kind: str, severity: float, details: object) -> None:
    con.execute(
        "INSERT INTO signals VALUES (?, ?, ?, ?, [], ?, now())",
        [identity, kind, sid, severity, json.dumps(details)],
    )


def test_grouped_findings_diversify_kinds_deduplicate_cost_and_link_worst_sessions() -> None:
    with database() as con:
        costs = {"busy-a": 2.0, "busy-b": 3.0, "other": 9.0}
        for sid, cost in costs.items():
            con.execute(
                "INSERT INTO sessions (session_id,harness,title,project) "
                "VALUES (?, 'claude', ?, 'synthetic')",
                [sid, sid],
            )
            con.execute("INSERT INTO session_cost VALUES (?, ?, 'price_table')", [sid, cost])
        polls = [
            ("a1", "busy-a", 100, "echo waiting", 300),
            ("a2", "busy-a", 90, "true", 270),
            ("b1", "busy-b", 80, "sleep 1", 240),
        ]
        for identity, sid, severity, command, runs in polls:
            signal(con, identity, sid, "rerun_loop", severity, {"command": command, "runs": runs})
        affected = {sid for _, sid, _, _, _ in polls}
        total_calls = sum(runs for _, _, _, _, runs in polls)
        context_cost = sum(costs[sid] for sid in affected)
        signal(con, "c1", "other", "user_correction", 1, {"phrases": ["that's wrong"]})
        signal(con, "c2", "other", "orphaned_session", 2, {"tools": ["Bash"]})
        signal(con, "zero", "other", "edit_churn", 0, {"path": "informational"})
        signal(con, "ghost", "unknown", "edit_churn", 1e30, {"path": "unknown owner"})
        # Competing rerun groups must not displace the first group of another kind.
        signal(con, "verify", "busy-a", "rerun_loop", 50, {"command": "pytest", "runs": 150})
        rows = findings.grouped(con, 4)
        assert [(r["kind"], r["subject"]) for r in rows] == [
            ("rerun_loop", "idle no-op polling"),
            ("orphaned_session", "orphaned_session"),
            ("user_correction", "user correction"),
            ("rerun_loop", "pytest"),
        ]
        polling = rows[0]
        assert {
            k: polling[k]
            for k in ("call_count", "session_count", "finding_count", "affected_sessions_cost_usd")
        } == {
            "call_count": total_calls,
            "session_count": len(affected),
            "finding_count": len(polls),
            "affected_sessions_cost_usd": context_cost,
        }
        members: Any = polling["worst_sessions"]
        assert [(m["session_id"], m["open_commands"]) for m in members] == [
            ("busy-a", ["claude --resume busy-a"]),
            ("busy-b", ["claude --resume busy-b"]),
        ]
        assert "each call is a full model turn" in str(polling["explanation"])
        page = html.render(report.build(con, load_rubrics(), datetime.now(UTC)), {})
        assert "Worst affected sessions</summary>" in page
        assert f"{len(polls)} findings · {len(affected)} sessions · {total_calls} calls" in page
        assert f"Affected sessions: ${context_cost:.2f} known cost (context, not waste)" in page
        assert (
            con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE table_name='finding_subjects'"
            ).fetchall()
            == []
        )


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def test_repositories_prefer_recorded_identity_and_share_common_directory(tmp_path: Path) -> None:
    repo, worktree = tmp_path / "repo", tmp_path / "arbitrary-worktree"
    git("init", str(repo))
    git(
        "-C",
        str(repo),
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=test@example.test",
        "commit",
        "--allow-empty",
        "-m",
        "Synthetic base",
    )
    git("-C", str(repo), "worktree", "add", str(worktree), "-b", "synthetic-work")
    common = str((repo / ".git").resolve())
    assert repositories.from_path(str(repo)) == (common, str(repo))
    assert repositories.from_path(str(worktree)) == (common, str(worktree))
    git("-C", str(repo), "remote", "add", "origin", "git@example.test:team/widgets.git")
    (repo / "subdir").mkdir()
    with database() as con:
        for sid, cwd in [
            ("main", repo),
            ("subdir", repo / "subdir"),
            ("work", worktree),
            ("recorded", repo),
            ("missing", tmp_path / "missing"),
            ("unknown", tmp_path / "other"),
        ]:
            con.execute(
                "INSERT INTO sessions(session_id,harness,cwd,project) "
                "VALUES (?, 'codex', ?, 'raw-name')",
                [sid, str(cwd)],
            )
        event(
            con,
            "meta",
            "recorded",
            0,
            "session_meta",
            {"payload": {"git": {"repository_url": "https://example.test/team/other.git"}}},
        )
        # A copied parent meta with no own sequence cannot override own metadata.
        event(
            con,
            "inherited",
            "recorded",
            1,
            "session_meta",
            {"payload": {"git": {"repository_url": "https://example.test/wrong.git"}}},
        )
        con.execute("UPDATE events SET seq=NULL WHERE event_id='inherited'")
        repositories.install(con)
        assert dict(con.execute("SELECT session_id,project FROM session_projects").fetchall()) == {
            "main": "example.test/team/widgets",
            "subdir": "example.test/team/widgets",
            "work": "example.test/team/widgets",
            "recorded": "example.test/team/other",
            "missing": str(tmp_path / "missing"),
            "unknown": str(tmp_path / "other"),
        }
        con.execute(runner._sql("rollups"))
        row = con.execute(
            "SELECT key,worktree_count FROM rollup_by_project WHERE key='example.test/team/widgets'"
        ).fetchone()
        assert row == ("example.test/team/widgets", len({repo, worktree}))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/team/widgets.git/",
        "ssh://git@example.test/team/widgets.git",
        "git@example.test:team/widgets.git",
    ],
)
def test_remote_spellings_share_identity(url: str) -> None:
    assert repositories.normalize(url) == "example.test/team/widgets"


def test_status_and_rollups_count_the_same_session_tree_and_label_both_denominators() -> None:
    with database() as con:
        con.execute(
            "INSERT INTO sessions(session_id,harness,parent_session_id,project) VALUES "
            "('root','claude',NULL,'project'), ('child','codex','root','project'), "
            "('grandchild','claude','child','project'), "
            "('orphan','codex','missing','other')"
        )
        con.execute(runner._sql("rollups"))
        counts = status.session_counts(con)
        trees = {"root": {"root", "child", "grandchild"}, "orphan": {"orphan"}}
        lanes = {"claude": {"root"}, "codex": {"orphan"}}
        assert set(
            con.execute("SELECT root_session_id,session_id FROM session_tree").fetchall()
        ) == {(root, member) for root, members in trees.items() for member in members}
        assert counts == [
            {
                "lane": lane,
                "top_level_sessions": len(roots),
                "sessions_including_subagents": len(set().union(*(trees[root] for root in roots))),
            }
            for lane, roots in lanes.items()
        ]
        from slopsniffer.config import load_config

        assert (
            status.collect(con, load_config(), ("claude", "codex"), ())["session_counts"] == counts
        )
        assert con.execute(
            "SELECT key, sessions, top_level_sessions, sessions_including_subagents "
            "FROM rollup_by_lane ORDER BY key"
        ).fetchall() == [
            (
                r["lane"],
                r["top_level_sessions"],
                r["top_level_sessions"],
                r["sessions_including_subagents"],
            )
            for r in counts
        ]
        document = report.build(con, load_rubrics(), datetime.now(UTC))
        assert "top_level_sessions" in html.render(document, {})


@pytest.mark.parametrize("budget", ["rows", "bytes"])
def test_repository_and_grouping_reads_are_bounded_at_32mb(
    monkeypatch: pytest.MonkeyPatch,
    budget: str,
) -> None:
    # Byte boundaries must advance even when a single meta record exceeds the cap.
    monkeypatch.setattr(repositories, "BATCH_ROWS", 1 if budget == "rows" else 2)
    monkeypatch.setattr(repositories, "BATCH_BYTES", 100 if budget == "bytes" else 100000)
    with database() as con:
        for sid in ("alpha", "beta", "gamma"):
            con.execute(
                "INSERT INTO sessions(session_id,harness,cwd) VALUES (?, 'codex', ?)",
                [sid, f"/missing/{sid}"],
            )
            event(
                con,
                sid,
                sid,
                0,
                "session_meta",
                {
                    "payload": {"git": {"repository_url": f"https://example.test/team/{sid}.git"}},
                    "padding": "x" * 8192,
                },
            )
            signal(con, sid, sid, "rerun_loop", 2, {"command": "echo waiting", "runs": 6})
        # Most history is unrelated wide bodies and must never be decoded for identity.
        con.execute(
            "INSERT INTO events SELECT 'wide-'||i, 'alpha', i+1, 'user', 'message', "
            "NULL, 1, i+100, 8192, json_object('padding',repeat('x',8192)) "
            "FROM range(1024) t(i)"
        )
        requested = []

        class ObservedConnection:
            def __getattr__(self, name: str) -> Any:
                return getattr(con, name)

            def sql(self, statement: str, *, params: list[Any]) -> duckdb.DuckDBPyRelation:
                requested.append(params[0])
                return con.sql(statement, params=params)

        repositories.install(cast(duckdb.DuckDBPyConnection, ObservedConnection()))
        assert requested == [[0], [1], [2]]
        assert {row[0] for row in con.execute("SELECT cwd FROM repository_paths").fetchall()} == {
            f"/missing/{sid}" for sid in ("alpha", "beta", "gamma")
        }
        assert dict(con.execute("SELECT session_id,project FROM session_projects").fetchall()) == {
            sid: f"example.test/team/{sid}" for sid in ("alpha", "beta", "gamma")
        }
        rows = findings.grouped(con, 20)
        assert {(r["kind"], r["subject"]) for r in rows} == {("rerun_loop", "idle no-op polling")}
        members: Any = rows[0]["worst_sessions"]
        assert {m["session_id"] for m in members} == {"alpha", "beta", "gamma"}


def test_run_forwards_approved_grading_cap_and_rejects_cap_without_grade(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from slopsniffer import cli

    calls = []

    def ingest(*args: Any, **kwargs: Any) -> tuple[dict[str, object], int]:
        return {}, 0

    def grade(*args: Any, **kwargs: Any) -> tuple[dict[str, object], int]:
        calls.append((args[2], kwargs["max_calls"]))
        return {}, 0

    monkeypatch.setattr(cli, "_ingest_step", ingest)
    monkeypatch.setattr(cli, "_grade_step", grade)
    monkeypatch.setattr(cli, "_outcomes_step", ingest)
    assert main(["run", "--grade", "--worst", "1", "--max-calls", "2", "--json"]) == 0
    capsys.readouterr()
    assert calls == [(1, 2)]
    assert main(["run", "--max-calls", "2", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["message"] == "run: --max-calls requires --grade"
    assert calls == [(1, 2)]


def test_v4_readers_refresh_old_rollup_definitions_without_writing_database(
    tmp_path: Path,
) -> None:
    from slopsniffer.config import load_config
    from slopsniffer.db import SCHEMA_VERSION, open_read, open_write

    config = load_config()
    with open_write(config) as con:
        assert SCHEMA_VERSION == 4
        con.execute(
            "INSERT INTO sessions(session_id,harness,cwd,project) "
            "VALUES ('recorded','codex','/missing/checkout','old-worktree-key')"
        )
        con.execute(
            "INSERT INTO source_files(source_file_id,path,harness) "
            "VALUES (1,'/synthetic/transcript.jsonl','codex')"
        )
        event(
            con,
            "recorded-meta",
            "recorded",
            0,
            "session_meta",
            {"payload": {"git": {"repository_url": "https://example.test/team/widgets.git"}}},
        )
        # Emulate v4's stored macro selecting raw project labels.
        old = runner._sql("rollups").replace("JOIN session_projects r ON", "JOIN sessions r ON")
        con.execute(old)
    before = config.db_path.read_bytes()
    with open_read(config) as con:
        assert con.execute("SELECT version FROM schema_version").fetchone() == (4,)
        assert con.execute("SELECT key FROM rollup_by_project").fetchall() == [
            ("example.test/team/widgets",)
        ]
        expected = {"project": "example.test/team/widgets"}
        for document in (
            runner.report(con, runner.Filters(), datetime.now(UTC)),
            report.build(con, load_rubrics(), datetime.now(UTC)),
        ):
            rollups: Any = document.get("rollups") or document["sections"]["rollups"]  # type: ignore[typeddict-item]
            assert [r["key"] for r in rollups["project"]] == [expected["project"]]
    assert config.db_path.read_bytes() == before
    with duckdb.connect(str(config.db_path), read_only=True) as raw:
        assert raw.execute("SELECT project FROM sessions").fetchall() == [("old-worktree-key",)]


@pytest.mark.parametrize("consumer", ["compute", "report", "write_report", "build"])
def test_every_analysis_reader_derives_new_repository_metadata(consumer: str) -> None:
    import io

    from slopsniffer.config import load_config

    with database() as con:
        con.execute(
            "INSERT INTO sessions(session_id,harness,cwd) VALUES ('fresh','codex','/missing/fresh')"
        )
        event(
            con,
            "fresh-meta",
            "fresh",
            0,
            "session_meta",
            {"payload": {"git": {"repository_url": "https://example.test/team/fresh.git"}}},
        )
        signal(con, "fresh-signal", "fresh", "rerun_loop", 1, {"command": "echo ready", "runs": 3})
        now = datetime.now(UTC)
        if consumer == "compute":
            runner.compute(con, load_config(), None, now)
        elif consumer == "build":
            assert (
                report.build(con, load_rubrics(), now)["sections"]["top_findings"][0]["project"]
                == "example.test/team/fresh"
            )
        else:
            con.execute(runner._sql("rollups"))
            if consumer == "report":
                document = runner.report(con, runner.Filters(), now)
            else:
                stream = io.StringIO()
                runner.write_report(con, runner.Filters(), now, stream)
                document = json.loads(stream.getvalue())
            assert [r["key"] for r in document["rollups"]["project"]] == ["example.test/team/fresh"]
        assert con.execute("SELECT project FROM session_projects").fetchall() == [
            ("example.test/team/fresh",)
        ]


def test_large_subjects_stay_bounded_without_merging_distinct_commands() -> None:
    left = findings.subject("rerun_loop", {"command": "verify " + "x" * 8192 + "a"})
    right = findings.subject("rerun_loop", {"command": "verify " + "x" * 8192 + "b"})
    assert left != right
    assert len(left) <= 256
    assert len(right) <= 256


def test_sql_noop_detection_keeps_state_reads_and_writes_informational() -> None:
    from slopsniffer.config import load_config
    from tests.test_value_precision import call

    commands = {
        "echo": "echo waiting",
        "sleep": "sleep 1",
        "true": "true",
        "colon": ":",
        "expand": "echo $VALUE",
        "redirect": "echo > file",
        "real": "git status",
        "newline": "echo ready\nrm file",
    }
    with database() as con:
        evidence = {}
        for sid, command in commands.items():
            con.execute("INSERT INTO sessions(session_id,harness) VALUES (?, 'claude')", [sid])
            evidence[sid] = {call(con, sid, n * 10, "Bash", {"command": command}) for n in range(3)}
        runner.compute(con, load_config(), None, datetime.now(UTC))
        rows = con.execute(
            "SELECT session_id,severity,evidence_event_ids,details->>'interpretation' "
            "FROM signals WHERE kind='rerun_loop'"
        ).fetchall()
        assert {sid: set(ids) for sid, _, ids, _ in rows} == evidence
        assert {
            sid: interpretation for sid, severity, _, interpretation in rows if severity > 0
        } == {sid: "idle_noop_polling" for sid in ("echo", "sleep", "true", "colon")}
        assert {sid for sid, severity, _, _ in rows if severity == 0} == {
            "expand",
            "redirect",
            "real",
            "newline",
        }
