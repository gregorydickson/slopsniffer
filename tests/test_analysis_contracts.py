"""History-wide wide transforms must stay behind shared session fences."""

import hashlib
import io
import json
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import cast

import duckdb
import pytest

from slopsniffer import analysis, proposals, report, status
from slopsniffer.cli import COMMANDS, HARNESSES
from slopsniffer.config import load_config
from slopsniffer.db import apply_schema
from slopsniffer.grading import selection
from slopsniffer.grading.rubrics import load_rubrics
from slopsniffer.ingest.reader import BATCH_BYTES, BATCH_ROWS
from slopsniffer.outcomes import linkage
from slopsniffer.signals import runner
from slopsniffer.signals import text as signal_text
from tests.ingest.deadlines import deadline

NOW = datetime(2026, 11, 12, tzinfo=UTC)


def test_resource_policy_membership() -> None:
    assert set(analysis.COMMAND_POLICIES) == set(COMMANDS)
    assert set(runner.KINDS) == runner.SESSION_KINDS | runner.GLOBAL_KINDS
    assert runner.SESSION_KINDS.isdisjoint(runner.GLOBAL_KINDS)
    assert load_config().ingest.batch_rows == BATCH_ROWS


def seed(con: duckdb.DuckDBPyConnection) -> list[str]:
    ids = [f"session-{n:03}" for n in range(96)]
    turns = 128
    for sid in ids:
        con.execute("INSERT INTO sessions (session_id, harness) VALUES (?, 'claude')", [sid])
        con.execute(
            "INSERT INTO events SELECT $1||'-'||i,$1,i,'user','message',NULL,1,i,8192, "
            "json_object('padding', repeat(md5(i::VARCHAR),256)) FROM range($2) t(i)",
            [sid, turns],
        )
        con.execute(
            "INSERT INTO messages SELECT $1||'-'||i, "
            "CASE WHEN i%2=0 THEN 'assistant' ELSE 'user' END, "
            "repeat(md5(i::VARCHAR),256)||' try again ACME-123' FROM range($2) t(i)",
            [sid, turns],
        )
        con.execute(
            "INSERT INTO tool_calls (call_id, session_id, use_event_id, result_event_id, "
            "tool_name, input, result_text, is_error) SELECT $1||'-call-'||i, $1, $1||'-'||i, "
            "$1||'-'||i, 'Bash', '{\"command\":\"gh pr create && git commit\"}', "
            "repeat(md5(i::VARCHAR),256)||chr(10)||'https://github.com/acme/test/pull/17' "
            "||chr(10)||'[topic abcd123] test', false FROM range($2) t(i)",
            [sid, turns],
        )
    return ids


def signal_rows(con: duckdb.DuckDBPyConnection) -> list[tuple[object, ...]]:
    return con.execute(
        "SELECT * EXCLUDE(computed_at), computed_at::VARCHAR FROM signals ORDER BY signal_id"
    ).fetchall()


@pytest.mark.slow
def test_signals_small_pool_matches_unbounded_computation(tmp_path: Path) -> None:
    config = load_config()
    config = replace(config, ingest=replace(config.ingest, batch_rows=128))
    with duckdb.connect(
        str(tmp_path / "history.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        con.execute("SET memory_limit='512MB'; SET threads=2")
        apply_schema(con)
        seed(con)
        # An independent unscoped execution of the original SQL definitions is the
        # oracle. Old c6c865e fails this history at 64MB (recorded in the audit).
        values: dict[str, object] = {
            "rerun_min": config.signals.rerun_min,
            "churn_min": config.signals.churn_min,
            "verification_command": runner.VERIFICATION_COMMAND,
            "noop_command": runner.NOOP_COMMAND,
            "own_text_pattern": signal_text.OWN_TEXT_PATTERN,
            "clause_phrases": list(signal_text.CLAUSE_PHRASES),
            "correction_clause": signal_text.CORRECTION_CLAUSE,
            "recovery_retry": signal_text.RECOVERY_RETRY,
            "abandon_window": config.signals.abandon_window,
            "orphan_hours": config.signals.orphan_hours,
            "correction_lexicon": list(config.signals.correction_lexicon),
            "since": None,
            "now": NOW,
            "computed_at": NOW,
        }
        con.execute(runner._RESULT_DDL)
        for start, end in analysis.event_batches(con, 128):
            runner._result_flags(con, start, end)
        con.execute(runner._sql("prelude"))
        runner._execute(
            con, "CREATE TEMP TABLE signal_turns AS " + runner._sql("turn_inputs"), values
        )
        con.execute("CREATE TEMP TABLE signal_compactions AS " + runner._sql("compaction_inputs"))
        for kind in runner.KINDS:
            runner._execute(
                con, runner._INSERT.format(select=runner._sql(kind)), values | {"kind": kind}
            )
        expected = signal_rows(con)
        con.execute(
            "DROP TABLE signal_turns; DROP TABLE signal_compactions; DROP TABLE signal_result_flags"
        )
        assert expected
        con.execute("SET memory_limit='64MB'")
        runner.compute(con, config, None, NOW)
        assert signal_rows(con) == expected


@pytest.mark.slow
def test_outcomes_small_pool_matches_unbounded_linkage(tmp_path: Path) -> None:
    with duckdb.connect(
        str(tmp_path / "links.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        con.execute("SET memory_limit='512MB'; SET threads=2")
        apply_schema(con)
        seed(con)
        con.execute("CREATE TEMP TABLE link_repos(cwd VARCHAR, repo VARCHAR)")
        sql = resources.files("slopsniffer.outcomes").joinpath("linkage.sql").read_text("utf-8")
        con.execute(sql, {"ticket_pattern": "ACME-[0-9]+"})
        expected = con.execute("SELECT * FROM session_links ORDER BY ALL").fetchall()
        con.execute("DROP TABLE link_repos")
        assert expected
        con.execute("SET memory_limit='64MB'")
        linkage.link(con, "ACME-[0-9]+", batch_rows=128)
        assert con.execute("SELECT * FROM session_links ORDER BY ALL").fetchall() == expected


@pytest.mark.slow
@pytest.mark.parametrize("command", ["report", "grade", "propose", "status"])
def test_summary_commands_small_pool_matches_default(tmp_path: Path, command: str) -> None:
    config = load_config()
    config = replace(config, ingest=replace(config.ingest, batch_rows=2))
    rubrics = load_rubrics()
    with duckdb.connect(
        str(tmp_path / "summaries.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        con.execute("SET memory_limit='512MB'; SET threads=2")
        apply_schema(con)
        ids = seed(con)
        con.execute(runner._sql("rollups"))
        for sid in ids:
            con.execute(
                "INSERT INTO signals SELECT $1||'-signal-'||i, 'user_correction', $1, 1, "
                "[]::VARCHAR[], json_object('body', repeat(md5(i::VARCHAR),256)), $2 "
                "FROM range($3) t(i)",
                [sid, NOW, 128 if command == "report" else 1],
            )
        if command == "report":
            for sid in ids:
                con.execute(
                    "INSERT INTO proposals SELECT $1||'-proposal-'||i, 'cluster', 'review', "
                    "'code-quality','proposed',NULL,'draft/SKILL.md','draft/change.diff', "
                    "json_object('session_ids',[$1], 'failures', "
                    "[json_object('reason',repeat(md5(i::VARCHAR),256))]), $2 FROM range(128) t(i)",
                    [sid, NOW],
                )
        if command == "propose":
            # Stale hashes may accumulate across rubric edits; their wide reasons
            # must be filtered in SQL before Python ever receives them.
            for sid in ids:
                con.execute(
                    "INSERT INTO grades SELECT $1, 'code-quality', 'obsolete-'||i, 'graded', "
                    "'block', 0, repeat(md5(i::VARCHAR),256), []::VARCHAR[], $2 "
                    "FROM range(128) t(i)",
                    [sid, NOW],
                )
            for sid in ids:
                con.execute(
                    "INSERT INTO sessions (session_id,harness) "
                    "SELECT $1||'-graded-'||i, 'claude' FROM range(128) t(i)",
                    [sid],
                )
                con.execute(
                    "INSERT INTO grades SELECT $1||'-graded-'||i, 'code-quality', $2, "
                    "'graded', 'block', 0, 'Skipped tests '||"
                    "repeat(lpad(i::VARCHAR,8,'0')||' ',1000), "
                    "[]::VARCHAR[], $3 FROM range(128) t(i)",
                    [sid, rubrics["code-quality"].sha256, NOW],
                )

        def run() -> object:
            match command:
                case "report":
                    return report.build(con, rubrics, NOW)
                case "grade":
                    return selection.select_worst(con, config.grading.badness_weights, rubrics, 7)
                case "propose":
                    result = proposals.propose(
                        con, config, rubrics, backend=config.grading.backend, executors={}
                    )
                    digest = hashlib.sha256()
                    persisted: list[str] = []
                    for (pid,) in con.execute(
                        "SELECT proposal_id FROM proposals ORDER BY proposal_id"
                    ).fetchall():
                        (raw,) = analysis.physical_row(
                            con, "proposals", "evidence::VARCHAR", "proposal_id = ?", [pid]
                        )
                        evidence = json.loads(raw)
                        persisted.extend(f["session_id"] for f in evidence["failures"])
                        digest.update(json.dumps([pid, evidence], sort_keys=True).encode())
                    current = [
                        sid
                        for (sid,) in con.execute(
                            "SELECT session_id FROM grades WHERE status = 'graded' "
                            "AND rubric_hash = ?",
                            [rubrics["code-quality"].sha256],
                        ).fetchall()
                    ]
                    assert sorted(persisted) == sorted(current)
                    return result.summary, digest.hexdigest()
                case "status":
                    return status.collect(con, config, HARNESSES, COMMANDS)
            raise AssertionError(command)

        expected = run()
        con.execute("SET memory_limit='64MB'")
        assert run() == expected


@pytest.mark.parametrize("budget", ["rows", "bytes"])
def test_session_batches_preserve_boundaries_and_cleanup(tmp_path: Path, budget: str) -> None:
    with duckdb.connect(
        str(tmp_path / "scopes.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        sizes = {"a": 2, "b": 3, "c": 1}
        length = 1 if budget == "rows" else BATCH_BYTES
        cap = 2 if budget == "rows" else load_config().ingest.batch_rows
        for sid, size in sizes.items():
            con.execute(
                "INSERT INTO events SELECT $1||i,$1,i,'user','message',NULL,1,i,$2,NULL "
                "FROM range($3) t(i)",
                [sid, length, size],
            )
        with deadline(2, "analysis session queue"), analysis.session_batches(con, cap) as batches:
            found = list(batches)
        assert found == [[sid] for sid in sizes]
        assert (
            con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE temporary "
                "AND table_name NOT IN ('repository_paths','repository_metadata')"
            ).fetchall()
            == []
        )


@pytest.mark.parametrize("budget", ["rows", "bytes"])
def test_event_spans_obey_each_budget(tmp_path: Path, budget: str) -> None:
    config = load_config()
    count = 7
    cap = 2 if budget == "rows" else config.ingest.batch_rows
    length = 1 if budget == "rows" else BATCH_BYTES
    with duckdb.connect(
        str(tmp_path / "events.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO events SELECT 'e'||i,'s',i,'user','message',NULL,1,i,$1,NULL "
            "FROM range($2) t(i)",
            [length, count],
        )
        with deadline(2, "analysis event spans"):
            found = list(analysis.event_batches(con, cap))
        width = cap if budget == "rows" else 1
        assert found == [(start, min(start + width, count)) for start in range(0, count, width)]


def test_later_signal_failure_preserves_published_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config()
    config = replace(config, ingest=replace(config.ingest, batch_rows=1))
    with duckdb.connect(
        str(tmp_path / "failure.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        for sid in ("a", "b"):
            con.execute(
                "INSERT INTO events SELECT $1||i,$1,i,'user','message',NULL,1,i,1,NULL "
                "FROM range(2) t(i)",
                [sid],
            )
            con.execute(
                "INSERT INTO messages VALUES (?,'assistant','hello'),(?,'user','try again')",
                [sid + "0", sid + "1"],
            )
        con.execute("INSERT INTO signals VALUES ('old','user_correction','a',1,[],NULL,?)", [NOW])
        expected = signal_rows(con)
        execute = runner._execute
        completed: list[str] = []

        def fail_later(
            connection: duckdb.DuckDBPyConnection, sql: str, values: dict[str, object]
        ) -> duckdb.DuckDBPyConnection:
            if values.get("kind") == "user_correction":
                if completed:
                    observer = connection.cursor()
                    try:
                        assert observer.execute(
                            "SELECT session_id FROM analysis_signals WHERE kind = 'user_correction'"
                        ).fetchall() == [("a",)]
                    finally:
                        observer.close()
                    raise RuntimeError("second session fails")
                completed.append(str(values["kind"]))
            return execute(connection, sql, values)

        monkeypatch.setattr(runner, "_execute", fail_later)
        with pytest.raises(RuntimeError, match="second session fails"):
            runner.compute(con, config, None, NOW)
        assert completed == ["user_correction"]
        assert signal_rows(con) == expected
        assert (
            con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE "
                "(temporary AND table_name NOT IN ('repository_paths','repository_metadata')) "
                "OR table_name LIKE 'analysis_%'"
            ).fetchall()
            == []
        )


def test_replacements_preserve_non_index_constraints(tmp_path: Path) -> None:
    with duckdb.connect(
        str(tmp_path / "constraints.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        for table in ("signals", "session_links", "proposals"):

            def constraints(name: str) -> list[tuple[object, ...]]:
                return con.execute(
                    "SELECT constraint_type, constraint_text, constraint_column_names "
                    "FROM duckdb_constraints() WHERE table_name = ? ORDER BY ALL",
                    [name],
                ).fetchall()

            expected = constraints(table)
            assert expected
            analysis.create_replacement(con, table, "staged")
            con.execute(f"DROP TABLE {table}")
            con.execute(f"ALTER TABLE staged RENAME TO {table}")
            assert constraints(table) == expected


def test_shared_scope_contains_only_selected_narrow_events(tmp_path: Path) -> None:
    with duckdb.connect(
        str(tmp_path / "narrow.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        for sid in ("a", "b"):
            con.execute(
                "INSERT INTO events VALUES (?, ?, 1, 'user', 'message', NULL, 1, 0, 1, '{}')",
                [sid + "-event", sid],
            )
        with analysis.scope(con, ["a"]):
            rows = con.execute("SELECT event_id, session_id FROM analysis_events").fetchall()
            assert rows == [("a-event", "a")]
            columns = {column[0] for column in con.execute("DESCRIBE analysis_events").fetchall()}
            assert columns == {
                "event_id",
                "session_id",
                "seq",
                "kind",
                "source_file_id",
                "byte_offset",
            }
        assert (
            con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE temporary "
                "AND table_name NOT IN ('repository_paths','repository_metadata')"
            ).fetchall()
            == []
        )


def test_body_span_selection_excludes_unrelated_physical_rows(tmp_path: Path) -> None:
    with duckdb.connect(
        str(tmp_path / "bodies.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        for eid in ("a0", "a1", "a2", "b0"):
            con.execute(
                "INSERT INTO events VALUES (?, ?, 1, 'user', 'message', NULL, 1, 0, 1, '{}')",
                [eid, eid[0]],
            )
        for eid in ("b0", "a0", "unrelated", "a1", "a2", "tail"):
            con.execute("INSERT INTO messages VALUES (?, 'user', 'body')", [eid])
            con.execute(
                "INSERT INTO tool_calls "
                "(call_id,session_id,use_event_id,result_event_id,tool_name,input) "
                "VALUES (?, ?, ?, ?, 'Bash', '{}')",
                [eid, eid[0], eid, eid],
            )
        statement = analysis.event_scoped(
            con, "SELECT event_id FROM messages ORDER BY event_id", 0, 3
        )
        assert con.execute(statement).fetchall() == [("a0",), ("a1",), ("a2",)]
        tools = analysis.event_scoped(con, "select call_id from TOOL_CALLS order by call_id", 0, 3)
        assert con.execute(tools).fetchall() == [("a0",), ("a1",), ("a2",)]


def test_compact_flags_preserve_duckdb_json_decisions(tmp_path: Path) -> None:
    unicode_record = json.dumps({"isMeta": "é" * 100}, ensure_ascii=False)
    records = [
        unicode_record,
        '{"isMeta":true,"isMeta":false,"body":{"isMeta":false}}',
        '{"isCompactSummary":"yes","isCompactSummary":false}',
        '{"subtype":"compact_boundary","subtype":"other"}',
        '{"isMeta":{},"isCompactSummary":[],"subtype":null}',
        '{"isMeta":false,"isCompactSummary":0,"subtype":"other"}',
        '[{"isMeta":true}]',
        "null",
        None,
    ]
    decisions = (
        "coalesce(TRY_CAST(json_extract_string(parsed, '$.isMeta') AS BOOLEAN),false), "
        "coalesce(TRY_CAST(json_extract_string(parsed, '$.isCompactSummary') AS BOOLEAN),false), "
        "coalesce(json_extract_string(parsed, '$.subtype') = 'compact_boundary',false)"
    )
    with duckdb.connect(
        str(tmp_path / "flags.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        for n, raw in enumerate(records):
            con.execute(
                "INSERT INTO events VALUES (?, 's', ?, 'user', 'message', NULL, 1, 0, 1, ?)",
                [str(n), n, raw],
            )
        expected = con.execute(
            "SELECT event_id, " + decisions + " FROM events ORDER BY event_id"
        ).fetchall()
        runner._event_flags(con, 0, len(records))
        reduced = dict(
            con.execute("SELECT event_id, parsed::VARCHAR FROM signal_event_flags").fetchall()
        )
        assert all(
            not isinstance(value, (dict, list))
            for raw in reduced.values()
            for value in json.loads(raw).values()
        )
        assert len(reduced["0"].encode("utf-8")) <= len(unicode_record.encode("utf-8"))
        assert (
            con.execute(
                "SELECT event_id, " + decisions + " FROM signal_event_flags ORDER BY event_id"
            ).fetchall()
            == expected
        )


def test_signal_output_pages_narrow_keys_and_streams_identical_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [
        ("z", "rerun_loop", "a"),
        ("d", "user_correction", "b"),
        ("c", "user_correction", "a"),
        ("a", "edit_churn", "b"),
        ("b", "edit_churn", "a"),
    ]
    with duckdb.connect(
        str(tmp_path / "listing.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        for sid in {sid for _, _, sid in records}:
            con.execute("INSERT INTO sessions (session_id, harness) VALUES (?, 'claude')", [sid])
        con.execute(runner._sql("rollups"))
        for signal_id, kind, sid in [*records, ("unknown", "edit_churn", "missing")]:
            con.execute(
                "INSERT INTO signals VALUES (?, ?, ?, 1, ['e'], '{\"body\":\"wide\"}', ?)",
                [signal_id, kind, sid, NOW],
            )
        page_cap = 2
        monkeypatch.setattr(runner, "BATCH_ROWS", page_cap)
        execute = runner._execute

        def bounded_page(
            connection: duckdb.DuckDBPyConnection, sql: str, values: Mapping[str, object]
        ) -> duckdb.DuckDBPyConnection:
            cursor = execute(connection, sql, values)
            if "page_size" not in values:
                return cursor
            rows = cursor.fetchall()
            assert len(rows) <= page_cap
            assert "evidence_event_ids" not in sql and "s.details" not in sql

            class Page:
                def fetchall(self) -> list[tuple[object, ...]]:
                    return rows

            return cast(duckdb.DuckDBPyConnection, Page())

        monkeypatch.setattr(runner, "_execute", bounded_page)
        with deadline(2, "signal listing keyset"):
            doc = runner.report(con, runner.Filters(), NOW)
            output = io.StringIO()
            runner.write_report(con, runner.Filters(), NOW, output)
        assert json.loads(output.getvalue()) == doc
        assert [(s["signal_id"], s["kind"], s["session_id"]) for s in doc["signals"]] == sorted(
            records, key=lambda r: (r[1], r[2], r[0])
        )
        assert all(
            s["evidence_event_ids"] == ["e"] and s["details"] == {"body": "wide"}
            for s in doc["signals"]
        )

        monkeypatch.setattr(report, "TOP_FINDINGS", 2)
        with deadline(2, "report finding bound"):
            found, _ = report._signals(con, NOW)
        expected_top = {row[0] for row in sorted(records)[: report.TOP_FINDINGS]}
        assert {signal["signal_id"] for signal in found} == expected_top


def test_event_transaction_contains_selection_and_body_writes(tmp_path: Path) -> None:
    with duckdb.connect(
        str(tmp_path / "transactions.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        con.execute("CREATE TABLE batch_log (value VARCHAR)")
        observer = con.cursor()
        committed: set[str] = set()

        def stored(connection: duckdb.DuckDBPyConnection) -> set[str]:
            return {
                value for (value,) in connection.execute("SELECT value FROM batch_log").fetchall()
            }

        def selections() -> Iterator[tuple[int, int]]:
            for name, span in (("a", (0, 1)), ("b", (1, 2))):
                con.execute("INSERT INTO batch_log VALUES (?)", ["metadata-" + name])
                assert stored(observer) == committed
                yield span

        batches = selections()
        try:
            with analysis.event_transaction(con, batches) as span:
                assert span == (0, 1)
                con.execute("INSERT INTO batch_log VALUES ('body-a')")
                assert stored(observer) == committed
            committed = {"metadata-a", "body-a"}
            assert stored(observer) == committed
            with (
                pytest.raises(RuntimeError, match="batch fails"),
                analysis.event_transaction(con, batches) as span,
            ):
                assert span == (1, 2)
                con.execute("INSERT INTO batch_log VALUES ('body-b')")
                raise RuntimeError("batch fails")
            assert stored(con) == stored(observer) == committed
            con.begin()
            con.rollback()
        finally:
            observer.close()


@pytest.mark.parametrize("budget", ["rows", "bytes"])
def test_proposals_batch_current_evidence_without_dropping_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    import importlib
    import re
    from typing import Any

    from slopsniffer.grading.backends import Raw, _Run
    from slopsniffer.proposals.clusters import stored_clusters

    config = load_config()
    config = replace(config, ingest=replace(config.ingest, batch_rows=2))
    monkeypatch.setattr(
        importlib.import_module("slopsniffer.proposals.clusters"),
        "BATCH_ROWS",
        config.ingest.batch_rows,
    )
    rubrics = load_rubrics()
    reason = "Skipped tests " + "é" * 200
    byte_cap = BATCH_BYTES
    if budget == "bytes":
        from slopsniffer.proposals.clusters import Cluster, Failure, Surface

        singleton = Cluster(
            Surface("review", "fixture"),
            "code-quality",
            "skipped tests",
            (Failure("s-a", "code-quality", "block", 0.0, reason, ("s-a-skill",)),),
        )
        byte_cap = len(json.dumps(proposals._cluster_evidence(singleton))) * 2
        monkeypatch.setattr(analysis, "BATCH_BYTES", byte_cap)
        config = replace(config, ingest=replace(config.ingest, batch_rows=BATCH_ROWS))
    real = tmp_path / "SKILL.md"
    current = "Original instructions.\n"
    real.write_text(current, "utf-8")
    ids = {"s-a", "s-b", "s-c", "s-d", "s-e"}
    prompts: list[str] = []

    def execute(schema: Mapping[str, Any], prompt: str, cwd: Path) -> Raw:
        del schema, cwd
        if prompts:
            assert f"Improved batch {len(prompts)}\n" in prompt
        members = re.findall(r"(?m)^  - (\S+) `", prompt)
        fact_bytes = sum(
            len(
                json.dumps(
                    {
                        "session_id": sid,
                        "label": "block",
                        "score": 0.0,
                        "reason": reason,
                        "event_ids": [sid + "-skill"],
                    }
                )
            )
            for sid in members
        )
        assert fact_bytes <= byte_cap or len(members) == 1
        prompts.append(prompt)
        output = {
            "propose": True,
            "reason": "Repeated skipped tests",
            "skill_md": f"Improved batch {len(prompts)}\n",
        }
        return Raw(_Run(NOW, 0, 0, "", "", None), output, (None, None))

    from slopsniffer.proposals.writer import Writer, Written

    original_write = Writer.write

    def observe_write(
        self: Writer,
        surface: str,
        real_path: str,
        current: str,
        improved: str,
        evidence: object,
    ) -> Written:
        assert isinstance(evidence, Mapping)

        def checked() -> Iterator[object]:
            for cluster in evidence["clusters"]:
                assert isinstance(cluster["failures"], Iterator)
                yield cluster

        outgoing = dict(evidence)
        outgoing["clusters"] = checked()
        return original_write(self, surface, real_path, current, improved, outgoing)

    monkeypatch.setattr(Writer, "write", observe_write)
    with duckdb.connect(
        str(tmp_path / "drafts.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO skills (path,name,scope,content_hash) "
            "VALUES (?, 'review','global','hash')",
            [str(real)],
        )
        for sid in sorted(ids):
            con.execute("INSERT INTO sessions (session_id,harness) VALUES (?, 'claude')", [sid])
            con.execute(
                "INSERT INTO skill_invocations VALUES (?, ?, 'review','skill_tool',NULL)",
                [sid + "-skill", sid],
            )
            con.execute(
                "INSERT INTO grades VALUES (?, 'code-quality', ?, 'graded','block',0,?, ?, ?)",
                [sid, rubrics["code-quality"].sha256, reason, [sid + "-skill"], NOW],
            )
        with deadline(2, "proposal spool pagination"), stored_clusters(con, rubrics) as grouped:
            observed = [f for cluster in grouped for f in cluster.failures]
            assert [f.session_id for f in observed] == sorted(ids)
            assert all(
                f.reason == reason and f.event_ids == (f.session_id + "-skill",) for f in observed
            )
        with deadline(2, "proposal evidence queues"):
            result = proposals.propose(
                con, config, rubrics, backend="codex", executors={"codex": execute}
            )
        seen: list[str] = []
        for prompt in prompts:
            members = re.findall(r"(?m)^  - (\S+) `", prompt)
            assert len(members) <= config.ingest.batch_rows
            seen.extend(members)
        assert sorted(seen) == sorted(ids)
        assert result.draft_failures == []
        assert result.summary["rejected"] == []
        assert result.summary["proposals"][0]["evidence_sessions"] == sorted(ids)
        evidence = json.loads(
            (config.data_dir / "proposals/review/evidence.json").read_text("utf-8")
        )
        assert {f["session_id"] for c in evidence["clusters"] for f in c["failures"]} == ids
        fragments = con.execute("SELECT proposal_id, evidence::VARCHAR FROM proposals").fetchall()
        assert len({pid for pid, _ in fragments}) == len(fragments)
        persisted: list[str] = []
        for _, raw in fragments:
            assert len(raw) <= byte_cap or len(json.loads(raw)["failures"]) == 1
            members = json.loads(raw)["failures"]
            assert len(members) <= config.ingest.batch_rows
            persisted.extend(f["session_id"] for f in members)
        assert sorted(persisted) == sorted(ids)
        assert real.read_text("utf-8") == current
        assert (config.data_dir / "proposals/review/SKILL.md").read_text(
            "utf-8"
        ) == f"Improved batch {len(prompts)}\n"
        assert (
            con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE table_name LIKE '%spool%' "
                "OR table_name LIKE 'analysis_%'"
            ).fetchall()
            == []
        )


@pytest.mark.parametrize("budget", ["rows", "bytes"])
def test_shared_evidence_batches_enforce_each_budget(
    budget: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = ["aa", "bb", "cc", "dd", "oversized"]
    rows = 2 if budget == "rows" else BATCH_ROWS
    byte_cap = BATCH_BYTES if budget == "rows" else 4
    monkeypatch.setattr(analysis, "BATCH_BYTES", byte_cap)
    batches = list(analysis.sized_batches(((item, len(item)) for item in items), rows))
    assert [item for batch in batches for item in batch] == items
    expected = [["aa", "bb"], ["cc", "dd"], ["oversized"]]
    assert batches == expected
    assert all(len(batch) <= rows for batch in batches)
    assert all(sum(map(len, batch)) <= byte_cap or len(batch) == 1 for batch in batches)


def test_evidence_json_consumes_iterators_incrementally() -> None:
    from slopsniffer.proposals.writer import _write_json

    items = ["first é", "second", "third"]
    output = io.StringIO()

    def values() -> Iterator[str]:
        for index, item in enumerate(items):
            if index:
                assert json.dumps(items[index - 1]) in output.getvalue()
            yield item

    _write_json({"evidence": values()}, output)
    assert json.loads(output.getvalue()) == {"evidence": items}


def test_neighbor_windows_partition_sessions_and_preserve_batch_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, non_utf8_text_defaults: None
) -> None:
    import inspect
    from importlib.resources.abc import Traversable

    files = resources.files

    class CaseSensitiveResources:
        def __init__(self, root: Traversable) -> None:
            self.root = root

        def joinpath(self, name: str) -> Traversable:
            children = {child.name: child for child in self.root.iterdir()}
            if name not in children:
                raise FileNotFoundError(name)
            return children[name]

    def strict_files(anchor: str | None = None) -> CaseSensitiveResources:
        package = anchor
        if package is None:
            frame = inspect.currentframe()
            assert frame is not None and frame.f_back is not None
            package = frame.f_back.f_globals["__package__"]
            del frame
        return CaseSensitiveResources(files(package))

    config = load_config()
    monkeypatch.setattr(resources, "files", strict_files)
    with duckdb.connect(
        str(tmp_path / "neighbors.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        for sid in ("a", "b", "c", "f", "g"):
            con.execute("INSERT INTO sessions (session_id,harness) VALUES (?, 'claude')", [sid])

        def event(eid: str, sid: str, seq: int, kind: str) -> None:
            con.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, NULL, 1, 0, 1, '{}')",
                [eid, sid, seq, kind, kind],
            )

        for eid, sid, seq, role, text in (
            ("a-msg", "a", 1, "assistant", "hello"),
            ("b-msg", "b", 1, "user", "try again"),
            ("c-0", "c", 1, "assistant", "hello"),
            ("c-1", "c", 2, "user", "try again"),
        ):
            event(eid, sid, seq, "message")
            con.execute("INSERT INTO messages VALUES (?, ?, ?)", [eid, role, text])
        for sid, error in (("a", True), ("b", False)):
            eid = sid + "-tool"
            event(eid, sid, 2, "tool_call")
            con.execute(
                "INSERT INTO tool_calls (call_id,session_id,use_event_id,result_event_id,"
                "tool_name,input,is_error) VALUES (?, ?, ?, ?, 'Bash',"
                ' \'{"command":"echo retry"}\', ?)',
                [eid, sid, eid, eid, error],
            )
        for n in range(config.signals.rerun_min):
            eid = "f-" + str(n)
            event(eid, "f", n * 2 + 1, "tool_call")
            con.execute(
                "INSERT INTO tool_calls (call_id,session_id,use_event_id,result_event_id,"
                "tool_name,input,is_error) VALUES (?, 'f', ?, ?, 'Bash',"
                ' \'{"command":"echo repeated"}\', false)',
                [eid, eid, eid],
            )
        con.execute("UPDATE tool_calls SET result_text = 'repeated' WHERE session_id = 'f'")
        event("g-edit", "g", 2, "tool_call")
        con.execute(
            "INSERT INTO tool_calls (call_id,session_id,use_event_id,result_event_id,"
            "tool_name,input,is_error) VALUES ('g','g','g-edit','g-edit','Edit',"
            '\'{"file_path":"x"}\',false)'
        )
        config = replace(config, ingest=replace(config.ingest, batch_rows=BATCH_ROWS))
        with deadline(10, "neighbor window computation"):
            runner.compute(con, config, None, NOW)
        assert set(con.execute("SELECT kind,session_id FROM signals").fetchall()) == {
            ("user_correction", "c"),
            ("rerun_loop", "f"),
        }
        expected = signal_rows(con)
        config = replace(config, ingest=replace(config.ingest, batch_rows=3))
        with deadline(10, "neighbor window batches"):
            runner.compute(con, config, None, NOW)
        assert signal_rows(con) == expected
        with deadline(2, "bounded command progress"):
            linkage.link(con, "", batch_rows=config.ingest.batch_rows)
        assert con.execute("SELECT * FROM session_links").fetchall() == []


def test_report_leaderboard_preserves_unbounded_float_sums(tmp_path: Path) -> None:
    from slopsniffer.contracts import Signal

    values = [0.1, 0.2, 1 / 3] * 113
    found: list[Signal] = [
        {
            "signal_id": f"{n:06}",
            "kind": "rerun_loop" if n % 2 else "edit_churn",
            "session_id": "a" if n % 3 else "b",
            "harness": "claude",
            "project": None,
            "severity": value,
            "evidence_event_ids": [],
            "details": {},
        }
        for n, value in enumerate(values)
    ]
    tie = cast(Signal, dict(found[0]))
    tie["signal_id"] = "tie"
    tie["kind"] = "user_correction"
    tie["severity"] = sum(
        item["severity"]
        for item in sorted(found, key=lambda x: (x["session_id"], x["signal_id"]))
        if item["kind"] == "edit_churn"
    )
    found.append(tie)
    with duckdb.connect(
        str(tmp_path / "leaderboard.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO sessions (session_id,harness) VALUES ('a','claude'),('b','claude')"
        )
        con.execute(
            "INSERT INTO signals SELECT unnest($1::VARCHAR[]), unnest($2::VARCHAR[]), "
            "unnest($3::VARCHAR[]), unnest($4::DOUBLE[]), []::VARCHAR[], '{}'::JSON, $5",
            [[s[key] for s in found] for key in ("signal_id", "kind", "session_id", "severity")]
            + [NOW],
        )
        con.execute(
            "INSERT INTO signals VALUES ('unknown','rerun_loop','missing',1e30,[],NULL,?)", [NOW]
        )
        ordered = sorted(found, key=lambda s: (s["kind"], s["session_id"], s["signal_id"]))
        assert report._stored_leaderboard(con) == report._leaderboard(ordered)


def test_proposal_report_fetches_fragments_without_sorting_bodies(
    tmp_path: Path,
) -> None:
    with duckdb.connect(
        str(tmp_path / "proposal-report.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        for sid in ("a", "b"):
            con.execute(
                "INSERT INTO proposals VALUES ('legacy-duplicate','cluster','review', "
                "'code-quality','proposed',NULL,'draft','diff',?,?)",
                [json.dumps({"session_ids": [sid], "failures": [{"reason": "body"}]}), NOW],
            )

        class Observed:
            def sql(self, statement: str) -> duckdb.DuckDBPyRelation:
                assert "evidence" not in statement.lower()
                result = con.sql(statement)
                assert result is not None
                return result

            def execute(self, statement: str, values: list[object]) -> duckdb.DuckDBPyConnection:
                assert "where rowid = ?" in statement.lower()
                return con.execute(statement, values)

        result = report._proposals(cast(duckdb.DuckDBPyConnection, Observed()))
        assert result == [
            {
                "skill": "review",
                "path": "draft",
                "diff_path": "diff",
                "evidence_sessions": ["a", "b"],
            }
        ]


def test_default_shared_budgets_do_not_admit_an_extra_row(tmp_path: Path) -> None:
    total = BATCH_ROWS + 1
    values = [str(n) for n in range(total)]
    with duckdb.connect(
        str(tmp_path / "defaults.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        con.execute(
            "INSERT INTO events SELECT 'e'||i, 's'||i, i, 'user','message',NULL,1,i,1,NULL "
            "FROM range(?) t(i)",
            [total],
        )
        with deadline(2, "default event budget"):
            assert list(analysis.event_batches(con)) == [
                (start, min(start + BATCH_ROWS, total)) for start in range(0, total, BATCH_ROWS)
            ]
        with deadline(2, "default session budget"), analysis.session_batches(con) as batches:
            sessions = list(batches)
        assert all(len(batch) <= BATCH_ROWS for batch in sessions)
        assert [sid for batch in sessions for sid in batch] == sorted(
            "s" + value for value in values
        )
    evidence_batches = list(analysis.sized_batches((value, len(value)) for value in values))
    assert [value for batch in evidence_batches for value in batch] == values
    assert all(len(batch) <= BATCH_ROWS for batch in evidence_batches)


def test_all_history_inputs_are_scoped_with_either_sql_case(tmp_path: Path) -> None:
    with duckdb.connect(
        str(tmp_path / "sources.duckdb"), config={"memory_limit": "512MB", "threads": 2}
    ) as con:
        apply_schema(con)
        for sid in ("a", "b"):
            con.execute("INSERT INTO sessions (session_id,harness) VALUES (?, 'claude')", [sid])
            con.execute(
                "INSERT INTO events VALUES (?, ?, 1,'user','message',NULL,1,0,1,'{}')",
                [sid + "-event", sid],
            )
            con.execute("INSERT INTO messages VALUES (?, 'user', 'body')", [sid + "-event"])
            con.execute(
                "INSERT INTO tool_calls (call_id,session_id,use_event_id,tool_name) "
                "VALUES (?, ?, ?, 'Bash')",
                [sid + "-call", sid, sid + "-event"],
            )
            con.execute(
                "INSERT INTO skill_invocations VALUES (?, ?, 'review','skill_tool',NULL)",
                [sid + "-event", sid],
            )
        con.execute("CREATE TEMP TABLE signal_turns AS SELECT session_id,seq,event_id FROM events")
        con.execute(
            "CREATE TEMP TABLE signal_compactions AS SELECT session_id,seq,event_id FROM events"
        )
        before = con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE temporary ORDER BY table_name"
        ).fetchall()
        with analysis.scope(con, ["a"]):
            for table in (
                "events",
                "sessions",
                "tool_calls",
                "skill_invocations",
                "signal_turns",
                "signal_compactions",
            ):
                for name in (table, table.upper()):
                    statement = analysis.scoped(
                        "-- with é SELECT is commentary"
                        + chr(10)
                        + f"select session_id from {name}"
                    )
                    assert con.execute(statement).fetchall() == [("a",)]
            for statement in (
                "with ids as (select event_id from messages) select * from ids",
                "WITH RECURSIVE ids AS (SELECT event_id FROM MESSAGES) SELECT * FROM ids",
                "WITH/* é */RECURSIVE ids(x) AS (SELECT 0 UNION ALL SELECT x+1 "
                "FROM ids WHERE x<1) SELECT event_id FROM messages WHERE "
                "(SELECT max(x) FROM ids)=1",
                "WITH RECURSIVE/*é*/ ids AS (SELECT event_id FROM messages) SELECT * FROM ids",
                "WITH/*é*/ ids AS (SELECT event_id FROM messages) SELECT * FROM ids",
            ):
                assert con.execute(analysis.scoped(statement)).fetchall() == [("a-event",)]
            assert con.execute(
                analysis.scoped("with ids as (select 1) select session_id from events")
            ).fetchall() == [("a",)]
        assert (
            con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE temporary ORDER BY table_name"
            ).fetchall()
            == before
        )


def test_prompt_budget_includes_each_clusters_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from slopsniffer.proposals.clusters import Cluster, Failure, Surface

    clusters = [
        Cluster(
            Surface("review", "fixture"),
            "code-quality",
            cause,
            (Failure(cause, "code-quality", "block", 0.0, "why", ()),),
        )
        for cause in ("first long cause", "second long cause")
    ]

    def cost(group: list[Cluster]) -> int:
        return sum(len(json.dumps(proposals._cluster_evidence(c))) for c in group)

    cap = cost(clusters) - 1
    monkeypatch.setattr(analysis, "BATCH_BYTES", cap)
    groups = list(proposals._groups(clusters, BATCH_ROWS))
    assert all(cost(group) <= cap for group in groups)
    assert [f.session_id for group in groups for c in group for f in c.failures] == [
        "first long cause",
        "second long cause",
    ]


@pytest.mark.parametrize("budget", ["rows", "bytes"])
def test_session_queue_accumulates_and_resets_exact_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    cap = 4
    monkeypatch.setattr(analysis, "BATCH_BYTES", cap if budget == "bytes" else BATCH_BYTES)
    with duckdb.connect(str(tmp_path / "queue.duckdb")) as con:
        apply_schema(con)
        for sid in ("", "b", "c", "d", "e", "f", "g", "h", "i"):
            con.execute(
                "INSERT INTO events SELECT $1||i,$1,i,'user','message',NULL,1,i,1,NULL "
                "FROM range(2) t(i)",
                [sid],
            )
        with deadline(2, "exact session budget"), analysis.session_batches(con, cap) as queue:
            assert list(queue) == [["", "b"], ["c", "d"], ["e", "f"], ["g", "h"], ["i"]]


def test_compute_uses_configured_event_cap_and_reduced_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config()
    config = replace(config, ingest=replace(config.ingest, batch_rows=2))
    original = analysis.event_scoped
    observed: set[str] = set()
    linked: set[str] = set()

    def inspect_scope(
        con: duckdb.DuckDBPyConnection,
        sql: str,
        start: int,
        end: int,
        *,
        sessions_all: bool = False,
        event_input: str | None = None,
    ) -> str:
        if "INSERT INTO signal_" in sql:
            assert event_input is not None
            rows = con.execute(f"SELECT event_id FROM {event_input}").fetchall()
            assert len(rows) <= config.ingest.batch_rows
            observed.update(eid for (eid,) in rows)
        elif start < end:
            rows = con.execute(
                "SELECT event_id FROM main.events WHERE rowid >= ? AND rowid < ?", [start, end]
            ).fetchall()
            assert len(rows) <= config.ingest.batch_rows
            linked.update(eid for (eid,) in rows)
        return original(con, sql, start, end, sessions_all=sessions_all, event_input=event_input)

    monkeypatch.setattr(analysis, "event_scoped", inspect_scope)
    with duckdb.connect(str(tmp_path / "compute-cap.duckdb")) as con:
        apply_schema(con)
        ids = ["a", "b", "c", "d", "e"]
        con.execute("INSERT INTO sessions (session_id,harness) VALUES ('s','claude')")
        for eid in ids:
            con.execute(
                "INSERT INTO events VALUES (?, 's', 1,'user','message',NULL,1,0,1,'{}')", [eid]
            )
        with deadline(2, "bounded command progress"):
            runner.compute(con, config, None, NOW)
        assert observed == set(ids)
        with deadline(2, "bounded command progress"):
            linkage.link(con, "", batch_rows=config.ingest.batch_rows)
        assert linked == set(ids)


def test_event_bytes_are_cumulative_inclusive_and_empty_id_is_not_an_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analysis, "BATCH_BYTES", 2)
    with duckdb.connect(str(tmp_path / "event-bytes.duckdb")) as con:
        apply_schema(con)
        for eid in ("", "b", "c", "d", "e"):
            con.execute(
                "INSERT INTO events VALUES (?, 's',1,'user','message',NULL,1,0,1,NULL)", [eid]
            )
        with deadline(2, "cumulative event bytes"):
            assert list(analysis.event_batches(con)) == [(0, 2), (2, 4), (4, 5)]
        for name in ("sessions", "SESSIONS"):
            con.execute("INSERT INTO sessions (session_id,harness) VALUES (?, 'claude')", [name])
        con.execute("UPDATE events SET session_id = 'sessions'")
        for name in ("sessions", "SESSIONS"):
            assert con.execute(
                analysis.event_scoped(con, f"SELECT session_id FROM {name}", 0, 1)
            ).fetchall() == [("sessions",)]
        con.execute("CREATE TEMP TABLE compact AS SELECT * FROM events LIMIT 1")
        compact = analysis.event_scoped(con, "SELECT * FROM events", 0, 1, event_input="compact")
        assert "main.events" not in compact
