"""Grading through fake ``codex`` and ``claude`` executables on PATH, and the reduce step."""

import itertools
import json
import statistics
from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from slopsniffer.cli import main
from slopsniffer.config import load_config
from slopsniffer.contracts import ChunkVerdict
from slopsniffer.contracts import Rubric as RubricName
from slopsniffer.grading import backends, grader
from slopsniffer.grading.rubrics import RUBRIC_NAMES, Rubric
from tests.grading.backend_fixtures import (
    OK_LABELS,
    RUBRICS,
    SESSIONS,
    Env,
    Task,
    call_rows,
    every_session_rubric,
    grade_rows,
    logged_tasks,
    ordered_tasks,
)
from tests.grading.backend_fixtures import (
    db as db,
)
from tests.grading.backend_fixtures import (
    env as env,
)
from tests.grading.fake_grader import INVALID
from tests.helpers import CLI_SCHEMAS_DIR, SCHEMAS_DIR, load_schema


def test_the_fixture_has_a_multi_chunk_session_and_renders_null_text_events(env: Env) -> None:
    del env
    tasks = ordered_tasks()
    assert len({chunk for sid, _, chunk in tasks if sid == "s-multi"}) > 1
    null_text = {
        f"{sid}-e{seq}"
        for sid, texts in SESSIONS.items()
        for seq, t in enumerate(texts)
        if t is None
    }
    assert null_text and null_text <= {e for _, _, chunk in tasks for e in chunk}


def test_success_stores_codex_verdicts(
    env: Env, db: Callable[[], duckdb.DuckDBPyConnection]
) -> None:
    env.scenario(codex=["ok"], claude=["fail"], labels=OK_LABELS)
    code, summary, _ = env.grade()
    tasks = ordered_tasks()
    calls = env.calls()
    assert code == 0
    assert logged_tasks(calls) == tasks
    assert {c["backend"] for c in calls} == {backends.CODEX}
    con = db()
    expected = {(sid, r, "graded", OK_LABELS[r]) for sid, r in every_session_rubric()}
    assert grade_rows(con) == expected
    hashes = set(con.execute("SELECT rubric, rubric_hash FROM grades").fetchall())
    assert hashes == {(r, RUBRICS[r].sha256) for r in RUBRIC_NAMES}
    scores = set(con.execute("SELECT rubric, score FROM grades").fetchall())
    assert scores == {
        (r, grader.chunk_score(RUBRICS[r], verdict(r, OK_LABELS[r]))) for r in RUBRIC_NAMES
    }
    usage = [
        (c["backend"], "ok", c["usage"]["input_tokens"], c["usage"]["output_tokens"]) for c in calls
    ]
    assert sorted(call_rows(con)) == sorted(usage)
    assert summary == {
        "selected_session_ids": list(SESSIONS),
        "calls": len(tasks),
        "calls_by_backend": {backends.CODEX: len(tasks)},
        "chunks_errored": 0,
        "sessions_graded": len(SESSIONS),
    }
    Draft202012Validator(load_schema(CLI_SCHEMAS_DIR / "grade.schema.json")).validate(summary)


def test_codex_runs_read_only_ephemeral_with_the_rubric_schema(env: Env) -> None:
    env.scenario(codex=["ok"], claude=["fail"])
    env.grade()
    for call in env.calls():
        argv = call["argv"]
        assert argv[0] == "exec"
        assert argv[argv.index("-s") + 1] == "read-only"
        assert {"--ephemeral", "--output-schema", "-o", "--json"} <= set(argv)


@pytest.mark.parametrize("backend", [backends.CODEX, backends.CLAUDE])
@pytest.mark.parametrize(
    "behaviour,template", [("display_event", "e:{}"), ("bracket_event", "[e:{}]")]
)
def test_display_citations_preserve_raw_calls_and_store_canonical_event_ids(
    env: Env,
    db: Callable[[], duckdb.DuckDBPyConnection],
    backend: str,
    behaviour: str,
    template: str,
) -> None:
    config_path = load_config().path
    assert config_path is not None
    config_path.write_text("[grading]\nchunk_tokens = 10000\nchunk_overlap = 10\n")
    env.scenario(codex=[behaviour], claude=[behaviour], labels=OK_LABELS)
    code, summary, _ = env.grade("--backend", backend)
    assert code == 0
    assert summary["selected_session_ids"] == list(SESSIONS)
    con = db()
    expected = {(sid, rubric, (f"{sid}-e0",)) for sid in SESSIONS for rubric in RUBRIC_NAMES}
    assert {
        (sid, rubric, tuple(cited))
        for sid, rubric, cited in con.execute(
            "SELECT session_id,rubric,cited_event_ids FROM grades WHERE status='graded'"
        ).fetchall()
    } == expected
    raw = {
        (sid, verdict_["rubric"], tuple(verdict_["cited_event_ids"]))
        for sid, output in con.execute("SELECT session_id,output FROM grade_calls").fetchall()
        for verdict_ in [json.loads(output)]
    }
    assert raw == {(sid, rubric, (template.format(ids[0]),)) for sid, rubric, ids in expected}


def test_codex_quota_falls_back_to_claude_and_codex_is_not_retried(
    env: Env, db: Callable[[], duckdb.DuckDBPyConnection]
) -> None:
    env.scenario(codex=["quota"], claude=["ok"], labels=OK_LABELS)
    code, summary, _ = env.grade()
    tasks = ordered_tasks()
    calls = env.calls()
    assert code == 0
    assert [c for c in calls if c["backend"] == backends.CODEX] == calls[:1]
    claude_calls = [c for c in calls if c["backend"] == backends.CLAUDE]
    assert logged_tasks(claude_calls) == tasks
    con = db()
    assert grade_rows(con) == {
        (sid, r, "graded", OK_LABELS[r]) for sid, r in every_session_rubric()
    }
    statuses = {(backend, status) for backend, status, _, _ in call_rows(con)}
    assert statuses == {(backends.CODEX, "quota"), (backends.CLAUDE, "ok")}
    claude_usage = {
        (
            u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"],
            u["output_tokens"],
        )
        for u in (c["usage"] for c in claude_calls)
    }
    assert {(i, o) for b, _, i, o in call_rows(con) if b == backends.CLAUDE} == claude_usage
    assert summary["calls_by_backend"] == {backends.CODEX: 1, backends.CLAUDE: len(tasks)}


def test_codex_only_quota_marks_chunks_error_without_calling_claude(
    env: Env, db: Callable[[], duckdb.DuckDBPyConnection]
) -> None:
    env.scenario(codex=["quota"], claude=["ok"])
    code, summary, _ = env.grade("--backend", "codex")
    calls = env.calls()
    assert code == 3
    assert {c["backend"] for c in calls} == {backends.CODEX}
    assert calls == calls[:1]
    con = db()
    assert grade_rows(con) == {(sid, r, "error", None) for sid, r in every_session_rubric()}
    assert summary["chunks_errored"] == len(SESSIONS)
    assert summary["sessions_graded"] == 0


@pytest.mark.parametrize(
    ("behaviour", "status"), [*((kind, "invalid") for kind in INVALID), ("fail", "failed")]
)
def test_unusable_output_from_both_marks_the_chunk_error_and_writes_no_verdict(
    env: Env, db: Callable[[], duckdb.DuckDBPyConnection], behaviour: str, status: str
) -> None:
    env.scenario(codex=[behaviour], claude=[behaviour])
    code, summary, _ = env.grade()
    con = db()
    assert code == 3
    assert grade_rows(con) == {(sid, r, "error", None) for sid, r in every_session_rubric()}
    nulls = con.execute(
        "SELECT count(*) FROM grades WHERE score IS NOT NULL OR reason IS NOT NULL "
        "OR cited_event_ids IS NOT NULL"
    ).fetchone()
    assert nulls == (0,)
    assert {(b, s) for b, s, _, _ in call_rows(con)} == {
        (backends.CODEX, status),
        (backends.CLAUDE, status),
    }
    tasks = ordered_tasks()
    first_chunks = {(sid, next(c for s, _, c in tasks if s == sid)) for sid in SESSIONS}
    assert {(t[0], t[2]) for t in logged_tasks(env.calls())} == first_chunks
    assert summary["chunks_errored"] == len(first_chunks)


def test_a_valid_answer_from_a_failed_codex_exit_is_not_trusted(
    env: Env, db: Callable[[], duckdb.DuckDBPyConnection]
) -> None:
    env.scenario(codex=["ok_nonzero"], claude=["ok"], labels=OK_LABELS)
    code, _, _ = env.grade()
    con = db()
    assert code == 0
    assert {(b, s) for b, s, _, _ in call_rows(con)} == {
        (backends.CODEX, "failed"),
        (backends.CLAUDE, "ok"),
    }
    claude_calls = [c for c in env.calls() if c["backend"] == backends.CLAUDE]
    assert logged_tasks(claude_calls) == ordered_tasks()


def test_a_verdict_in_claude_result_text_is_accepted(
    env: Env, db: Callable[[], duckdb.DuckDBPyConnection]
) -> None:
    env.scenario(codex=["fail"], claude=["ok_fenced"], labels=OK_LABELS)
    code, _, _ = env.grade("--backend", "claude")
    assert code == 0
    assert grade_rows(db()) == {
        (sid, r, "graded", OK_LABELS[r]) for sid, r in every_session_rubric()
    }


def completed_rubrics(done: list[Task]) -> set[tuple[str, str]]:
    remaining = {(sid, r) for sid, r, _ in ordered_tasks()[len(done) :]}
    return {(sid, r) for sid, r, _ in done} - remaining


@pytest.mark.parametrize("position", ["first", "middle", "last", "beyond"])
def test_max_calls_stops_exactly_at_the_cap(
    env: Env, db: Callable[[], duckdb.DuckDBPyConnection], position: str
) -> None:
    tasks = ordered_tasks()
    cap = {"first": 1, "middle": len(tasks) // 2, "last": len(tasks) - 1, "beyond": len(tasks) + 1}[
        position
    ]
    env.scenario(codex=["ok"], claude=["fail"], labels=OK_LABELS)
    code, summary, err = env.grade("--max-calls", str(cap))
    calls = env.calls()
    made = min(cap, len(tasks))
    assert code == 0
    assert logged_tasks(calls) == tasks[:made]
    assert summary["calls"] == made
    con = db()
    assert len(call_rows(con)) == made
    graded = {(sid, r) for sid, r, status, _ in grade_rows(con) if status == "graded"}
    assert graded == completed_rubrics(tasks[:made])
    assert {s for _, _, s, _ in grade_rows(con)} <= {"graded"}
    assert ("--max-calls" in err) == (cap < len(tasks))


def test_a_capped_run_is_finished_by_the_next_run_without_regrading(env: Env) -> None:
    tasks = ordered_tasks()
    cap = len(tasks) // 2
    env.scenario(codex=["ok"], claude=["fail"], labels=OK_LABELS)
    env.grade("--max-calls", str(cap))
    finished = completed_rubrics(tasks[:cap])
    env.grade()
    second = logged_tasks(env.calls())[cap:]
    assert second == [t for t in tasks if (t[0], t[1]) not in finished]


def test_graded_sessions_are_skipped_unless_regrade(
    env: Env, db: Callable[[], duckdb.DuckDBPyConnection]
) -> None:
    env.scenario(codex=["ok"], claude=["fail"], labels=OK_LABELS)
    env.grade()
    first = env.calls()
    code, summary, _ = env.grade()
    assert (code, summary["selected_session_ids"], env.calls()) == (0, [], first)
    env.grade("--regrade")
    assert logged_tasks(env.calls()[len(first) :]) == ordered_tasks()
    assert (
        db()
        .execute(
            "SELECT session_id, rubric, rubric_hash FROM grades "
            "GROUP BY session_id, rubric, rubric_hash HAVING count(*) <> 1"
        )
        .fetchall()
        == []
    )
    assert grade_rows(db()) == {
        (sid, r, "graded", OK_LABELS[r]) for sid, r in every_session_rubric()
    }


def test_insufficient_evidence(env: Env, db: Callable[[], duckdb.DuckDBPyConnection]) -> None:
    labels = {**OK_LABELS, "code-quality": grader.INSUFFICIENT}
    env.scenario(codex=["ok"], claude=["fail"], labels=labels)
    code, summary, _ = env.grade()
    assert code == 0
    con = db()
    rows = set(
        con.execute("SELECT session_id, rubric, status, label, score FROM grades").fetchall()
    )
    insufficient = {(sid, "code-quality", "graded", grader.INSUFFICIENT, None) for sid in SESSIONS}
    assert {r for r in rows if r[1] == "code-quality"} == insufficient
    assert {r[3] for r in rows if r[1] != "code-quality"} == {
        OK_LABELS[r] for r in RUBRIC_NAMES
    } - {OK_LABELS["code-quality"]}
    assert summary["sessions_graded"] == len(SESSIONS)


def verdict(
    rubric: RubricName, label: str, cited: tuple[str, ...] = (), reason: str = ""
) -> ChunkVerdict:
    return {
        "rubric": rubric,
        "label": label,
        "score": 0.0,
        "reason": reason or f"{rubric}/{label}",
        "cited_event_ids": list(cited),
    }


def family(name: grader.Family) -> list[Rubric]:
    return [RUBRICS[r] for r in RUBRIC_NAMES if grader.FAMILIES[r] == name]


def graded_labels(rubric: Rubric) -> list[str]:
    return [label.value for label in rubric.labels if label.value != grader.INSUFFICIENT]


def score_of(rubric: Rubric, label: str) -> float:
    return {lb.value: lb.score for lb in rubric.labels}[label]


def test_rubric_families_partition_the_rubrics() -> None:
    assert set(grader.FAMILIES) == set(RUBRIC_NAMES)
    worst = {r.name for r in family("worst_label")}
    assert worst == {"code-quality", "procedure-compliance"}
    assert {r.name for r in family("mean_score")} == set(RUBRIC_NAMES) - worst


@pytest.mark.parametrize("rubric", family("worst_label"), ids=lambda r: r.name)
def test_worst_label_family_takes_the_worst_chunk(rubric: Rubric) -> None:
    for labels in itertools.permutations(graded_labels(rubric), 2):
        chunks = [verdict(rubric.name, lb, (f"e-{i}",)) for i, lb in enumerate(labels)]
        worst = min(chunks, key=lambda v: score_of(rubric, v["label"]))
        got = grader.reduce(rubric, chunks)
        assert (got.label, got.score, got.reason, got.cited_event_ids) == (
            worst["label"],
            score_of(rubric, worst["label"]),
            worst["reason"],
            worst["cited_event_ids"],
        )


@pytest.mark.parametrize("rubric", family("mean_score"), ids=lambda r: r.name)
def test_mean_score_family_takes_the_mean_chunk_score(rubric: Rubric) -> None:
    for labels in itertools.combinations_with_replacement(graded_labels(rubric), 3):
        chunks = [verdict(rubric.name, lb, (f"e-{lb}",)) for lb in labels]
        mean = statistics.fmean(score_of(rubric, lb) for lb in labels)
        got = grader.reduce(rubric, chunks)
        assert got.score == pytest.approx(mean)
        distance = min(abs(score_of(rubric, lb) - mean) for lb in graded_labels(rubric))
        assert got.label is not None
        assert abs(score_of(rubric, got.label) - mean) == pytest.approx(distance)
        assert got.cited_event_ids == list(dict.fromkeys(f"e-{lb}" for lb in labels))
        lowest = min(chunks, key=lambda v: score_of(rubric, v["label"]))
        assert got.reason == lowest["reason"]


@pytest.mark.parametrize("rubric", family("mean_score"), ids=lambda r: r.name)
def test_mean_score_ties_resolve_to_the_worse_label(rubric: Rubric) -> None:
    for low, high in itertools.combinations(graded_labels(rubric), 2):
        mean = (score_of(rubric, low) + score_of(rubric, high)) / 2
        distance = {lb: abs(score_of(rubric, lb) - mean) for lb in graded_labels(rubric)}
        nearest = [lb for lb, d in distance.items() if d == pytest.approx(min(distance.values()))]
        got = grader.reduce(rubric, [verdict(rubric.name, low), verdict(rubric.name, high)])
        assert got.label == min(nearest, key=lambda lb: score_of(rubric, lb))


def test_reduce_excludes_insufficient_evidence_chunks() -> None:
    rubric = RUBRICS["code-quality"]
    assert grader.INSUFFICIENT in rubric.label_values
    best = max(graded_labels(rubric), key=lambda lb: score_of(rubric, lb))
    assert score_of(rubric, grader.INSUFFICIENT) < score_of(rubric, best)
    got = grader.reduce(
        rubric, [verdict("code-quality", grader.INSUFFICIENT), verdict("code-quality", best)]
    )
    assert (got.label, got.score) == (best, score_of(rubric, best))
    only = grader.reduce(
        rubric, [verdict("code-quality", grader.INSUFFICIENT, reason="no diff")] * 2
    )
    assert (only.label, only.score, only.reason, only.cited_event_ids) == (
        grader.INSUFFICIENT,
        None,
        "no diff",
        [],
    )
    empty = grader.reduce(rubric, [])
    assert (empty.label, empty.score, empty.reason) == (
        grader.INSUFFICIENT,
        None,
        grader.NO_EVENTS_REASON,
    )


def test_verdict_schema_mirrors_the_contract_schema() -> None:
    contract = load_schema(SCHEMAS_DIR / "grader" / "chunk_verdict.schema.json")
    assert {k: v for k, v in contract.items() if k in backends.VERDICT_SCHEMA} == dict(
        backends.VERDICT_SCHEMA
    )
    assert set(contract) - set(backends.VERDICT_SCHEMA) == {"$schema", "title", "description"}


@pytest.mark.parametrize("rubric", RUBRICS.values(), ids=lambda r: r.name)
def test_verdict_schema_is_narrowed_to_the_rubric(rubric: Rubric) -> None:
    schema = backends.verdict_schema(rubric)
    assert schema["properties"]["rubric"]["enum"] == [rubric.name]
    assert set(schema["properties"]["label"]["enum"]) == rubric.label_values
    request = backends.Request(rubric, "", frozenset({"e-1"}))
    label = graded_labels(rubric)[0]
    ok, _ = backends.validate(request, verdict(rubric.name, label, ("e-1",)))
    assert ok is not None
    other = next(r for r in RUBRIC_NAMES if r != rubric.name)
    wrong_rubric = {**verdict(rubric.name, label), "rubric": other}
    assert backends.validate(request, wrong_rubric)[0] is None
    assert backends.validate(request, verdict(rubric.name, label, ("e-2",)))[0] is None
    assert backends.validate(request, verdict(rubric.name, label, ("e:e-2",)))[0] is None
    assert backends.validate(request, verdict(rubric.name, label, ("[e:e-2]",)))[0] is None


def test_unknown_configured_backend_is_a_usage_error(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "bad.toml"
    config.write_text('[grading]\nbackend = "gemini"\n')
    monkeypatch.setenv("SLOPSNIFFER_CONFIG", str(config))
    env.scenario(codex=["ok"], claude=["ok"])
    env.capsys.readouterr()
    assert main(["grade", "--worst", "1", "--json"]) == 2
    assert json.loads(env.capsys.readouterr().out)["error"] == "config"
    assert env.calls() == []
