"""Backend process contracts, including malformed output and partial timeout streams."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from slopsniffer.contracts import ChunkVerdict
from slopsniffer.grading import backends
from slopsniffer.grading.rubrics import Label, Rubric

RUBRIC = Rubric(
    "verbosity",
    "Synthetic",
    "A test rubric.",
    (
        Label("good", "Good", 1.0),
        Label("bad", "Bad", 0.0),
    ),
    "Body",
    "synthetic-hash",
)
CANDIDATE: ChunkVerdict = {
    "rubric": "verbosity",
    "label": "good",
    "score": 1.0,
    "reason": "Evidence supports this.",
    "cited_event_ids": ["e"],
}


@pytest.mark.parametrize(
    ("usage", "expected", "exit_code", "has_answer"),
    [
        ({"input_tokens": 13, "output_tokens": 7}, (13, 7), 0, True),
        ({"input_tokens": 13, "output_tokens": 7}, (13, 7), 1, True),
        ({"input_tokens": True, "output_tokens": False}, (None, None), 0, True),
        ({"input_tokens": "13", "output_tokens": 1.5}, (None, None), 0, True),
        ({"input_tokens": 0, "output_tokens": 0}, (0, 0), 0, True),
        (None, (None, None), 0, True),
        (None, (None, None), 2, False),
    ],
)
def test_codex_uses_last_completed_usage_and_preserves_the_valid_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_utf8_text_defaults: None,
    usage: object,
    expected: tuple[int | None, int | None],
    exit_code: int,
    has_answer: bool,
) -> None:
    request = backends.Request(RUBRIC, "Actual prompt café", frozenset({"e"}))
    times = iter([10.0, 12.3456])
    monkeypatch.setattr(backends, "time", SimpleNamespace(monotonic=lambda: next(times)))
    monkeypatch.setattr(backends, "CALL_TIMEOUT_S", 0.01)
    paths: list[Path] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        schema = Path(command[command.index("--output-schema") + 1])
        last = Path(command[command.index("-o") + 1])
        paths.extend((schema, last))
        assert json.loads(schema.read_text("utf-8")) == request.schema
        assert command == [
            "codex",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "-C",
            str(tmp_path),
            "--json",
            "--output-schema",
            str(schema),
            "-o",
            str(last),
            "-",
        ]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["input"] == request.prompt
        assert kwargs["cwd"] == tmp_path
        assert kwargs["timeout"] == 0.01
        assert all(path.is_relative_to(tmp_path) for path in paths)
        if has_answer:
            last.write_text(json.dumps(CANDIDATE), "utf-8")
        stream = "\n".join(
            [
                "non-JSON log",
                json.dumps(
                    {"type": "turn.completed", "usage": {"input_tokens": 99, "output_tokens": 98}}
                ),
                json.dumps({"type": "turn.completed", "usage": usage}),
                json.dumps({"type": "other", "usage": {"input_tokens": 97, "output_tokens": 96}}),
            ]
        )
        if kwargs.get("check") and exit_code:
            raise subprocess.CalledProcessError(exit_code, command)
        return subprocess.CompletedProcess(command, exit_code, stream, "")

    monkeypatch.setattr(subprocess, "run", run)
    before = datetime.now(UTC)
    result = backends.run_codex(request, tmp_path)
    after = datetime.now(UTC)
    assert result.outcome is (backends.Outcome.OK if exit_code == 0 else backends.Outcome.FAILED)
    assert result.verdict == (CANDIDATE if exit_code == 0 else None)
    assert result.output == (CANDIDATE if has_answer else None)
    assert (result.input_tokens, result.output_tokens) == expected
    assert result.duration_ms == 2346
    assert before <= result.started_at <= after
    assert (result.backend, result.exit_code) == ("codex", exit_code)
    assert (result.error is None) == (exit_code == 0)
    assert paths and all(not path.exists() for path in paths)


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {
                "input_tokens": 2,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 5,
                "output_tokens": 7,
            },
            (10, 7),
        ),
        (
            {
                "input_tokens": True,
                "cache_creation_input_tokens": "5",
                "cache_read_input_tokens": None,
                "output_tokens": False,
            },
            (None, None),
        ),
        ({"cache_creation_input_tokens": 3, "output_tokens": 7}, (3, 7)),
        ({"input_tokens": 0, "output_tokens": 0}, (0, 0)),
    ],
)
@pytest.mark.parametrize(
    "shape",
    [
        "structured",
        "fenced",
        "both",
        "empty",
        "empty-fence",
        "fenced-info",
        "open-only",
        "end-only",
    ],
)
def test_claude_sums_only_known_integer_usage_and_rejects_incomplete_fences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    usage: dict[str, object],
    expected: tuple[int | None, int | None],
    shape: str,
) -> None:
    monkeypatch.setattr(backends, "CALL_TIMEOUT_S", 0.01)
    request = backends.Request(RUBRIC, "Full prompt", frozenset({"e"}))
    fence = chr(96) * 3
    answer = json.dumps(CANDIDATE, indent=2)
    doc: dict[str, object] = {"usage": usage}
    if shape in ("structured", "both"):
        doc["structured_output"] = CANDIDATE
        if shape == "both":
            doc["result"] = "malformed fallback"
    elif shape == "empty":
        pass
    elif shape == "empty-fence":
        doc["result"] = fence * 2
    elif shape == "fenced-info":
        doc["result"] = f"{fence}json extra-info\n{answer}\n{fence}"
    elif shape == "fenced":
        doc["result"] = f" \n{fence}json\n{answer}\n{fence}\n "
    elif shape == "open-only":
        doc["result"] = f"{fence}json\n{answer}"
    else:
        doc["result"] = f"heading\n{json.dumps(CANDIDATE)}\n{fence}"

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command == [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(request.schema),
            "--no-session-persistence",
            "--tools",
            "",
        ]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 0.01
        assert kwargs["input"] == "Full prompt"
        assert kwargs["cwd"] == tmp_path
        return subprocess.CompletedProcess(command, 0, json.dumps(doc), "")

    monkeypatch.setattr(subprocess, "run", run)
    result = backends.run_claude(request, tmp_path)
    assert (result.input_tokens, result.output_tokens) == expected
    if shape in ("structured", "fenced", "both", "fenced-info"):
        assert result.outcome is backends.Outcome.OK
        assert result.verdict == CANDIDATE
        assert result.error is None
    else:
        assert result.outcome is backends.Outcome.INVALID
        assert result.verdict is None
        assert result.output is None


@pytest.mark.parametrize(
    ("stdout", "stderr", "out", "err"),
    [
        (b"partial \xff", b"error \xff", "partial \ufffd", "error \ufffd"),
        ("partial text", None, "partial text", ""),
        (None, "error text", "", "error text"),
        (None, None, "", ""),
    ],
)
def test_timeouts_preserve_partial_streams_as_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    out: str,
    err: str,
) -> None:
    monkeypatch.setattr(backends, "CALL_TIMEOUT_S", 0.01)
    times = iter([10.0, 12.3456])
    monkeypatch.setattr(backends, "time", SimpleNamespace(monotonic=lambda: next(times)))

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", run)
    before = datetime.now(UTC)
    raw = backends.exec_claude({}, "prompt", tmp_path)
    assert before <= raw.run.started_at <= datetime.now(UTC)
    assert raw.run.duration_ms == 2346
    assert (raw.run.stdout, raw.run.stderr, raw.run.exit_code, raw.run.error) == (
        out,
        err,
        None,
        backends.Outcome.TIMEOUT,
    )
    assert raw.output is None
    assert raw.tokens == (None, None)


def test_missing_process_retains_its_error_time_and_empty_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter([10.0, 12.3456])
    monkeypatch.setattr(backends, "time", SimpleNamespace(monotonic=lambda: next(times)))
    monkeypatch.setattr(backends, "CALL_TIMEOUT_S", 0.01)

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("missing fake CLI")

    monkeypatch.setattr(subprocess, "run", run)
    before = datetime.now(UTC)
    raw = backends.exec_claude({}, "prompt", tmp_path)
    assert before <= raw.run.started_at <= datetime.now(UTC)
    assert (raw.run.stdout, raw.run.stderr, raw.run.exit_code, raw.run.error) == (
        "",
        "missing fake CLI",
        None,
        backends.Outcome.MISSING,
    )
    assert raw.run.duration_ms == 2346
    assert raw.output is None
    assert raw.tokens == (None, None)


@pytest.mark.parametrize("valid", [True, False])
def test_multiple_schema_errors_produce_an_invalid_invocation_instead_of_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    valid: bool,
) -> None:
    candidate: object = CANDIDATE if valid else {**CANDIDATE, "label": "wrong", "score": 2.0}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"structured_output": candidate}), ""
        )

    monkeypatch.setattr(backends, "CALL_TIMEOUT_S", 0.01)
    monkeypatch.setattr(subprocess, "run", run)
    request = backends.Request(RUBRIC, "prompt", frozenset({"e"}))
    result = backends.run_claude(request, tmp_path)
    assert result.outcome is (backends.Outcome.OK if valid else backends.Outcome.INVALID)
    assert result.verdict == (CANDIDATE if valid else None)
    if not valid:
        assert result.error is not None and result.error.startswith("invalid output: schema:")
