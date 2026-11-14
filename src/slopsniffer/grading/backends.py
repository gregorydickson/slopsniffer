"""Grading backends: one non-interactive model call that returns a ChunkVerdict.

``codex`` runs ``codex exec`` with the verdict schema as ``--output-schema`` and reads the last
message from ``-o``; ``claude`` runs ``claude -p --output-format json`` with the same schema as
``--json-schema``. Neither may persist a session of its own: those would land in the ingest
source roots and be graded as fleet work. Each call is classified into one ``Outcome``; only
``OK`` carries a verdict.
"""

import json
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from slopsniffer.contracts import ChunkVerdict
from slopsniffer.grading.rubrics import RUBRIC_NAMES, Rubric

CODEX = "codex"
CLAUDE = "claude"
CALL_TIMEOUT_S = 900
# Lower-cased phrases that mark a failed call as an exhausted plan or rate limit.
QUOTA_MARKERS = ("usage limit", "quota", "rate limit", "rate_limit", "too many requests")

# Mirrors schemas/grader/chunk_verdict.schema.json (the schemas/ tree is not packaged).
VERDICT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rubric", "label", "score", "reason", "cited_event_ids"],
    "properties": {
        "rubric": {"enum": list(RUBRIC_NAMES)},
        "label": {"type": "string"},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
        "cited_event_ids": {"type": "array", "items": {"type": "string"}},
    },
}


class Outcome(StrEnum):
    OK = "ok"
    QUOTA = "quota"
    FAILED = "failed"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    MISSING = "missing"


@dataclass(frozen=True)
class Request:
    rubric: Rubric
    prompt: str
    event_ids: frozenset[str]

    @property
    def schema(self) -> dict[str, Any]:
        return verdict_schema(self.rubric)


@dataclass(frozen=True)
class Invocation:
    backend: str
    outcome: Outcome
    started_at: datetime
    duration_ms: int
    exit_code: int | None
    input_tokens: int | None
    output_tokens: int | None
    output: object | None
    verdict: ChunkVerdict | None
    error: str | None


Backend = Callable[[Request, Path], Invocation]


def verdict_schema(rubric: Rubric) -> dict[str, Any]:
    """The ChunkVerdict schema narrowed to one rubric and its frontmatter labels."""
    schema = json.loads(json.dumps(VERDICT_SCHEMA))
    schema["properties"]["rubric"] = {"enum": [rubric.name]}
    schema["properties"]["label"] = {"enum": [label.value for label in rubric.labels]}
    return cast(dict[str, Any], schema)


def validate(request: Request, candidate: object) -> tuple[ChunkVerdict | None, str | None]:
    """The candidate as a verdict, or why it is not one for this request."""
    errors = sorted(
        Draft202012Validator(request.schema).iter_errors(candidate), key=lambda e: e.json_path
    )
    if errors:
        return None, f"schema: {errors[0].json_path}: {errors[0].message}"
    verdict = cast(ChunkVerdict, candidate)
    cited = [_canonical_event(event, request.event_ids) for event in verdict["cited_event_ids"]]
    foreign = sorted(set(cited) - request.event_ids)
    if foreign:
        return None, f"cited event ids outside the chunk: {', '.join(foreign)}"
    return {**verdict, "cited_event_ids": cited}, None


def _canonical_event(event: str, allowed: frozenset[str]) -> str:
    if event in allowed:
        return event
    displayed = event[1:-1] if event.startswith("[e:") and event.endswith("]") else event
    return displayed[2:] if displayed.startswith("e:") and displayed[2:] in allowed else event


@dataclass(frozen=True)
class _Run:
    started_at: datetime
    duration_ms: int
    exit_code: int | None
    stdout: str
    stderr: str
    error: Outcome | None


def _run(argv: list[str], prompt: str, cwd: Path) -> _Run:
    started_at = datetime.now(UTC)
    clock = time.monotonic()
    try:
        done = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=CALL_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        return _Run(started_at, _elapsed(clock), None, "", str(exc), Outcome.MISSING)
    except subprocess.TimeoutExpired as exc:
        out, err = (_text(s) for s in (exc.stdout, exc.stderr))
        return _Run(started_at, _elapsed(clock), None, out, err, Outcome.TIMEOUT)
    return _Run(started_at, _elapsed(clock), done.returncode, done.stdout, done.stderr, None)


def _elapsed(clock: float) -> int:
    return round((time.monotonic() - clock) * 1000)


def _text(raw: str | bytes | None) -> str:
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw or ""


def _finish(backend: str, request: Request, raw: Raw) -> Invocation:
    run = raw.run
    verdict, problem = (None, "no output") if raw.output is None else validate(request, raw.output)
    if run.error is None and run.exit_code == 0 and verdict is not None:
        outcome, error = Outcome.OK, None
    else:
        verdict = None
        outcome = failure(run)
        error = describe(outcome, run, problem)
    return Invocation(
        backend,
        outcome,
        run.started_at,
        run.duration_ms,
        run.exit_code,
        *raw.tokens,
        raw.output,
        verdict,
        error,
    )


def failure(run: _Run) -> Outcome:
    """Why a process whose answer is unusable failed; INVALID when it exited cleanly."""
    if run.error is not None:
        return run.error
    text = f"{run.stdout}\n{run.stderr}".lower()
    if any(marker in text for marker in QUOTA_MARKERS):
        return Outcome.QUOTA
    return Outcome.FAILED if run.exit_code != 0 else Outcome.INVALID


def describe(outcome: Outcome, run: _Run, problem: str | None) -> str:
    if outcome is Outcome.INVALID:
        return f"invalid output: {problem}"
    detail = (run.stderr.strip() or run.stdout.strip())[-2000:]
    return f"{outcome.value} (exit {run.exit_code}): {detail}"


def _parse_json(text: str) -> object | None:
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed


@dataclass(frozen=True)
class Raw:
    """One finished backend process: how it ran, its parsed answer and its token usage."""

    run: _Run
    output: object | None
    tokens: tuple[int | None, int | None]


def exec_codex(schema: Mapping[str, Any], prompt: str, workdir: Path) -> Raw:
    """``codex exec`` constrained to ``schema``; the answer is its last message, parsed."""
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        schema_path, out_path = Path(tmp, "schema.json"), Path(tmp, "last-message.txt")
        schema_path.write_text(json.dumps(schema), "utf-8")
        argv = [
            CODEX,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "-C",
            str(workdir),
            "--json",
            "--output-schema",
            str(schema_path),
            "-o",
            str(out_path),
            "-",
        ]
        run = _run(argv, prompt, workdir)
        last = out_path.read_text("utf-8") if out_path.is_file() else ""
    output = _parse_json(last.strip()) if last.strip() else None
    return Raw(run, output, _codex_tokens(run.stdout))


def run_codex(request: Request, workdir: Path) -> Invocation:
    return _finish(CODEX, request, exec_codex(request.schema, request.prompt, workdir))


def _codex_tokens(stdout: str) -> tuple[int | None, int | None]:
    tokens: tuple[int | None, int | None] = (None, None)
    for line in stdout.splitlines():
        event = _parse_json(line)
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            tokens = (_int(usage.get("input_tokens")), _int(usage.get("output_tokens")))
    return tokens


def exec_claude(schema: Mapping[str, Any], prompt: str, workdir: Path) -> Raw:
    """``claude -p`` constrained to ``schema``; the answer is its structured output, else the
    JSON in its result text."""
    argv = [
        CLAUDE,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--no-session-persistence",
        "--tools",
        "",
    ]
    run = _run(argv, prompt, workdir)
    result = _parse_json(run.stdout)
    output: object | None = None
    tokens: tuple[int | None, int | None] = (None, None)
    if isinstance(result, dict):
        output = result.get("structured_output")
        if output is None and isinstance(result.get("result"), str):
            output = _parse_json(_unfence(result["result"]))
        usage = result.get("usage") or {}
        parts = [
            _int(usage.get(k))
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        ]
        known = [p for p in parts if p is not None]
        tokens = (sum(known) if known else None, _int(usage.get("output_tokens")))
    return Raw(run, output, tokens)


def run_claude(request: Request, workdir: Path) -> Invocation:
    return _finish(CLAUDE, request, exec_claude(request.schema, request.prompt, workdir))


def _unfence(text: str) -> str:
    """The JSON inside a single fenced code block, or the text itself."""
    body = text.strip()
    if body.startswith("```") and body.endswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.removesuffix("```")
    return body.strip()


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


BACKENDS: Mapping[str, Backend] = {CODEX: run_codex, CLAUDE: run_claude}
ORDER: Mapping[str, tuple[str, ...]] = {
    "auto": (CODEX, CLAUDE),
    CODEX: (CODEX,),
    CLAUDE: (CLAUDE,),
}
EXECUTORS: Mapping[str, Callable[[Mapping[str, Any], str, Path], Raw]] = {
    CODEX: exec_codex,
    CLAUDE: exec_claude,
}
