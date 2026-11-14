"""The grading entry point persists the process facts and respects its defaults."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slopsniffer.config import load_config
from slopsniffer.contracts import ChunkVerdict
from slopsniffer.grading import grader
from slopsniffer.grading.backends import Invocation, Outcome, Request
from slopsniffer.grading.render import Chunk
from slopsniffer.grading.rubrics import Label, Rubric
from tests.grading.support import memory_db
from tests.proposals.support import install_fakes

RUBRIC = Rubric(
    "verbosity",
    "Synthetic",
    "A synthetic rubric.",
    (
        Label("good", "Good work.", 0.8),
        Label("bad", "Bad work.", 0.2),
    ),
    "\nRubric body.\n",
    "synthetic-hash",
)
NOW = datetime(2026, 11, 10, tzinfo=UTC)


@pytest.mark.parametrize("mode", ["ok", "failed", "no-output"])
def test_grade_persists_exact_call_facts_and_default_selection_skips_current_grades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    install_fakes(tmp_path, monkeypatch).scenario(codex=["fail"], claude=["fail"])
    sessions = {"best": 1.0, "worst": 3.0}
    tasks = [("worst", "verbosity", 0)]
    workdirs: list[Path] = []
    output: ChunkVerdict = {
        "rubric": "verbosity",
        "label": "good",
        "score": 0.1,
        "reason": "Actual reason.",
        "cited_event_ids": ["e-worst"],
    }
    invocation = Invocation(
        "codex",
        Outcome.OK if mode == "ok" else Outcome.FAILED,
        NOW,
        1234,
        0 if mode == "ok" else 1,
        11,
        7,
        None if mode == "no-output" else output,
        output if mode == "ok" else None,
        None if mode == "ok" else "transport broke",
    )

    def backend(request: Request, cwd: Path) -> Invocation:
        assert cwd.is_dir()
        assert "Hello" in request.prompt
        assert f"This is chunk 1 of {len(tasks)}; judge " in request.prompt
        assert request.rubric.name == "verbosity"
        workdirs.append(cwd)
        # Each successful answer cites the event in the actual request.
        if request.event_ids != frozenset({"e-worst"}):
            from dataclasses import replace

            verdict: ChunkVerdict = {**output, "cited_event_ids": sorted(request.event_ids)}
            return replace(invocation, verdict=verdict, output=verdict)
        return invocation

    config = load_config()
    with memory_db() as con:
        for sid, severity in sessions.items():
            con.execute("INSERT INTO sessions (session_id, harness) VALUES (?, 'claude')", [sid])
            con.execute(
                "INSERT INTO events (event_id, session_id, seq, record_type, kind, "
                "source_file_id, byte_offset, byte_length) "
                "VALUES (?, ?, 1, 'user', 'message', 1, 0, 1)",
                [f"e-{sid}", sid],
            )
            con.execute("INSERT INTO messages VALUES (?, 'user', 'Hello')", [f"e-{sid}"])
            con.execute(
                "INSERT INTO signals VALUES (?, 'rerun_loop', ?, ?, [], NULL, ?)",
                [f"sig-{sid}", sid, severity, NOW],
            )
        result = grader.grade(
            con,
            config,
            {"verbosity": RUBRIC},
            worst=1,
            backend="codex",
            backends={"codex": backend},
        )
        assert result.capped is False
        errored_tasks = tasks if mode != "ok" else []
        graded_sessions = {sid for sid, _, _ in tasks if mode == "ok"}
        assert result.summary == {
            "selected_session_ids": ["worst"],
            "calls": len(tasks),
            "calls_by_backend": {"codex": len(tasks)},
            "chunks_errored": len(errored_tasks),
            "sessions_graded": len(graded_sessions),
        }
        calls = con.execute(
            "SELECT session_id, chunk_index, backend, epoch(started_at), duration_ms, "
            "input_tokens, "
            "output_tokens, exit_code, status, output, error FROM grade_calls"
        ).fetchall()
        assert [
            (
                sid,
                index,
                name,
                started,
                duration,
                inp,
                out,
                exit_code,
                status,
                json.loads(payload) if payload is not None else None,
                error,
            )
            for (
                sid,
                index,
                name,
                started,
                duration,
                inp,
                out,
                exit_code,
                status,
                payload,
                error,
            ) in calls
        ] == [
            (
                "worst",
                0,
                "codex",
                NOW.timestamp(),
                1234,
                11,
                7,
                invocation.exit_code,
                invocation.outcome.value,
                invocation.output,
                None if mode == "ok" else "verbosity: transport broke",
            ),
        ]
        assert con.execute("SELECT output IS NULL FROM grade_calls").fetchall() == [
            (invocation.output is None,)
        ]
        rows = con.execute(
            "SELECT session_id, rubric, rubric_hash, status, label, score, reason, "
            "cited_event_ids FROM grades"
        ).fetchall()
        assert rows == [
            (
                "worst",
                "verbosity",
                "synthetic-hash",
                "graded" if mode == "ok" else "error",
                "good" if mode == "ok" else None,
                0.8 if mode == "ok" else None,
                "Actual reason." if mode == "ok" else None,
                ["e-worst"] if mode == "ok" else None,
            )
        ]
        if mode == "ok":
            following = grader.grade(
                con,
                config,
                {"verbosity": RUBRIC},
                worst=1,
                backend="codex",
                backends={"codex": backend},
            )
            assert following.summary["selected_session_ids"] == ["best"]
            previous = con.execute("SELECT call_id FROM grade_calls ORDER BY call_id").fetchall()
            cached = grader.grade(
                con,
                config,
                {"verbosity": RUBRIC},
                worst=1,
                backend="codex",
                backends={"codex": backend},
            )
            assert cached.summary["selected_session_ids"] == []
            assert (
                con.execute("SELECT call_id FROM grade_calls ORDER BY call_id").fetchall()
                == previous
            )
            con.execute("UPDATE grades SET rubric_hash = 'stale' WHERE session_id = 'worst'")
            refreshed = grader.grade(
                con,
                config,
                {"verbosity": RUBRIC},
                worst=1,
                backend="codex",
                backends={"codex": backend},
            )
            assert refreshed.summary["selected_session_ids"] == ["worst"]
            assert set(con.execute("SELECT session_id, rubric_hash FROM grades").fetchall()) == {
                (sid, RUBRIC.sha256) for sid in sessions
            } | {("worst", "stale")}
    assert all(not path.exists() for path in workdirs)


@pytest.mark.parametrize("header", ["Context in force.", ""])
def test_grading_prompt_contains_the_complete_rubric_labels_evidence_and_output_contract(
    header: str,
) -> None:
    chunks = [Chunk(i, header, "[e:e] user\nHello", 5, ("e",)) for i in range(3)]
    expected = (
        "You are grading part of an AI coding-agent session transcript against the "
        f"Synthetic rubric. This is chunk 2 of {len(chunks)}; judge only what this chunk shows."
        "\n\n# Rubric: verbosity\nA synthetic rubric.\nRubric body."
        "\n\n# Labels (choose exactly one)\n- `good` (score 0.8): Good work."
        "\n- `bad` (score 0.2): Bad work."
        '\n\nAnswer with one JSON object: "rubric" is "verbosity", "label" is one of the '
        'label values above, "score" is the score listed for that label, "reason" is one to '
        'three sentences, and "cited_event_ids" lists the ids of the events that support '
        'the verdict (the text after "[e:" in an event header). Cite only ids that appear '
        "in this chunk."
        + ("\n\nContext in force." if header else "")
        + "\n\n# Transcript\n[e:e] user\nHello"
    )
    assert grader.build_prompt(RUBRIC, chunks[1], len(chunks)) == expected
