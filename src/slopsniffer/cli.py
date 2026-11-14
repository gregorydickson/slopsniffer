"""``slopsniffer`` command line.

Every subcommand accepts ``--json`` (exactly one JSON document on stdout; logs
go to stderr) and ``--config``. Exit codes: 0 ok, 2 usage, 3 partial failure,
4 database locked. Subcommands that write take the single-writer lock before
their handler runs.
"""

import argparse
import base64
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import NoReturn, get_args

import duckdb

from slopsniffer import outcomes, proposals, query, report, signals, status
from slopsniffer.config import Config, load_config
from slopsniffer.contracts import (
    CliError,
    GradeSummary,
    Harness,
    IngestSummary,
    OutcomesSummary,
    RehydrateResult,
    RunSummary,
)
from slopsniffer.db import open_read, open_write
from slopsniffer.errors import ConfigError, DatabaseLocked, ExitCode, UsageError
from slopsniffer.grading import backends, grader
from slopsniffer.grading.rubrics import load_rubrics
from slopsniffer.ingest import claude, codex, crush, droid, engine, pickle, skills
from slopsniffer.report import html

PROG = "slopsniffer"
DESCRIPTION = "Local digest of agent transcripts: ingest, signals, outcomes, grading, proposals."
HARNESSES: tuple[Harness, ...] = get_args(Harness)
# Harness sources ``ingest`` can run; harnesses without one are skipped.
engine.REGISTRY[claude.HARNESS] = claude.source()
engine.REGISTRY[codex.HARNESS] = codex.source()
engine.REGISTRY[droid.HARNESS] = droid.source()
engine.REGISTRY[pickle.HARNESS] = pickle.source()
engine.REGISTRY[skills.HARNESS] = skills.source()
engine.REGISTRY[crush.HARNESS] = crush.source()
GRADE_BACKENDS = tuple(backends.ORDER)


@dataclass(frozen=True)
class Context:
    config: Config
    con: duckdb.DuckDBPyConnection | None
    json: bool

    def emit(self, doc: Mapping[str, object]) -> None:
        indent = None if self.json else 2
        sys.stdout.write(json.dumps(doc, indent=indent, default=str) + "\n")


Handler = Callable[[argparse.Namespace, Context], int]


@dataclass(frozen=True)
class Command:
    help: str
    writes: bool
    arguments: Callable[[argparse.ArgumentParser], None]
    handler: Handler


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        subcommand = self.prog.removeprefix(PROG).strip()
        raise UsageError(f"{subcommand}: {message}" if subcommand else message)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1: {value}")
    return value


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO date (YYYY-MM-DD): {text!r}") from None


def _harness_list(text: str) -> tuple[Harness, ...]:
    names = tuple(part.strip() for part in text.split(",") if part.strip())
    unknown = sorted(set(names) - set(HARNESSES))
    if unknown or not names:
        raise argparse.ArgumentTypeError(
            f"unknown harness {', '.join(unknown) or text!r}; choose from {','.join(HARNESSES)}"
        )
    return tuple(h for h in HARNESSES if h in names)


def _ingest_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--harness", type=_harness_list, default=HARNESSES, help="comma list")
    p.add_argument("--since", type=_iso_date)
    p.add_argument("--workers", type=_positive_int)


def _signals_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--kind", action="append", default=[], choices=signals.KINDS, metavar="K")
    p.add_argument("--project")
    p.add_argument("--lane", choices=HARNESSES, metavar="L")
    p.add_argument("--since", type=_iso_date)


def _outcomes_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--refresh", action="store_true")


def _grade_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--worst", type=_positive_int, required=True, metavar="N")
    p.add_argument("--backend", choices=GRADE_BACKENDS, help="default: grading.backend in config")
    p.add_argument("--max-calls", type=_positive_int, metavar="M")
    p.add_argument("--regrade", action="store_true")


def _propose_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", choices=GRADE_BACKENDS, help="default: grading.backend in config")


def _no_args(p: argparse.ArgumentParser) -> None:
    del p


def _report_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--html", metavar="PATH", help="write a self-contained HTML report")


def _query_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("sql", metavar="SQL")


def _rehydrate_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("event_id", metavar="EVENT_ID")


def _run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--grade", action="store_true", help="also grade the worst sessions")
    p.add_argument("--worst", type=_positive_int, metavar="N")


def _ingest_step(
    con: duckdb.DuckDBPyConnection,
    config: Config,
    harnesses: Sequence[Harness],
    since: date | None = None,
    workers: int | None = None,
) -> tuple[IngestSummary, int]:
    summary = engine.ingest(con, config, harnesses, since=since, workers=workers)
    failed = any(h["files_failed"] for h in summary["harnesses"].values())
    return summary, int(ExitCode.PARTIAL if failed else ExitCode.OK)


def _ingest(args: argparse.Namespace, ctx: Context) -> int:
    assert ctx.con is not None
    summary, code = _ingest_step(ctx.con, ctx.config, args.harness, args.since, args.workers)
    ctx.emit(summary)
    return code


def _signals(args: argparse.Namespace, ctx: Context) -> int:
    assert ctx.con is not None
    now = datetime.now(UTC)
    signals.compute(ctx.con, ctx.config, args.since, now)
    filters = signals.Filters(tuple(args.kind), args.project, args.lane, args.since)
    signals.write_report(ctx.con, filters, now, sys.stdout)
    return int(ExitCode.OK)


def _outcomes_step(
    con: duckdb.DuckDBPyConnection, config: Config, refresh: bool = False
) -> tuple[OutcomesSummary, int]:
    """Raises :class:`outcomes.GhUnavailable` when ``gh`` cannot be used at all."""
    summary, failure = outcomes.run(con, config, datetime.now(UTC), refresh_open=refresh)
    if failure is not None:
        sys.stderr.write(f"{PROG}: outcomes stopped early: {failure}\n")
    return summary, int(ExitCode.OK if failure is None else ExitCode.PARTIAL)


def _outcomes(args: argparse.Namespace, ctx: Context) -> int:
    assert ctx.con is not None
    try:
        summary, code = _outcomes_step(ctx.con, ctx.config, args.refresh)
    except outcomes.GhUnavailable as exc:
        return _fail(ExitCode.PARTIAL, "gh_unavailable", str(exc), args.command, ctx.json)
    ctx.emit(summary)
    return code


def _backend(choice: str | None, config: Config) -> str:
    backend: str = choice or config.grading.backend
    if backend not in GRADE_BACKENDS:
        raise ConfigError(f"grading.backend {backend!r} is not one of {', '.join(GRADE_BACKENDS)}")
    return backend


def _grade_step(
    con: duckdb.DuckDBPyConnection,
    config: Config,
    worst: int,
    backend: str | None = None,
    max_calls: int | None = None,
    regrade: bool = False,
) -> tuple[GradeSummary, int]:
    run = grader.grade(
        con,
        config,
        load_rubrics(),
        worst=worst,
        backend=_backend(backend, config),
        max_calls=max_calls,
        regrade=regrade,
    )
    if run.capped:
        sys.stderr.write(f"{PROG}: grade stopped at --max-calls {max_calls}\n")
    return run.summary, int(ExitCode.PARTIAL if run.summary["chunks_errored"] else ExitCode.OK)


def _grade(args: argparse.Namespace, ctx: Context) -> int:
    assert ctx.con is not None
    summary, code = _grade_step(
        ctx.con, ctx.config, args.worst, args.backend, args.max_calls, args.regrade
    )
    ctx.emit(summary)
    return code


NO_OUTCOMES: OutcomesSummary = {
    "links_written": 0,
    "prs_fetched": 0,
    "prs_cached": 0,
    "rate_limited": False,
    "rate_remaining": None,
}


def _run(args: argparse.Namespace, ctx: Context) -> int:
    """ingest, signals, outcomes, then grade when asked; each step is logged as its own
    command run and the exit code is the worst step's."""
    assert ctx.con is not None
    if args.grade != (args.worst is not None):
        raise UsageError("run: --grade and --worst N go together")
    con, config = ctx.con, ctx.config
    codes: list[int] = []

    def step[T](name: str, body: Callable[[], tuple[T, int]]) -> T:
        started = datetime.now(UTC)
        doc, code = body()
        status.record_run(con, name, started, datetime.now(UTC), code)
        codes.append(code)
        return doc

    def outcomes_step() -> tuple[OutcomesSummary, int]:
        try:
            return _outcomes_step(con, config)
        except outcomes.GhUnavailable as exc:
            sys.stderr.write(f"{PROG}: outcomes skipped: {exc}\n")
            return NO_OUTCOMES, int(ExitCode.PARTIAL)

    summary: RunSummary = {
        "ingest": step("ingest", lambda: _ingest_step(con, config, HARNESSES)),
        "signals": step(
            "signals",
            lambda: (signals.compute(con, config, None, datetime.now(UTC)), int(ExitCode.OK)),
        ),
        "outcomes": step("outcomes", outcomes_step),
    }
    if args.grade:
        summary["grade"] = step("grade", lambda: _grade_step(con, config, args.worst))
    ctx.emit(summary)
    return max(codes)


def _query(args: argparse.Namespace, ctx: Context) -> int:
    with open_read(ctx.config) as con:
        query.harden(con)
        result = query.run(con, args.sql)
    ctx.emit(result)
    return int(ExitCode.OK)


def _status(args: argparse.Namespace, ctx: Context) -> int:
    del args
    writers = [name for name, command in COMMANDS.items() if command.writes]
    with open_read(ctx.config) as con:
        doc = status.collect(con, ctx.config, HARNESSES, writers)
    ctx.emit(doc)
    return int(ExitCode.OK)


def _report(args: argparse.Namespace, ctx: Context) -> int:
    target = None if args.html is None else Path(args.html)
    if target is not None and not target.parent.is_dir():
        raise UsageError(f"report: no directory for --html {target}")
    with open_read(ctx.config) as con:
        doc = report.build(con, load_rubrics(), datetime.now(UTC), ctx.config)
    if target is not None:
        try:
            html.write(target, doc)
        except OSError as exc:
            return _fail(
                ExitCode.USAGE,
                "write_failed",
                f"report: cannot write {target}: {exc}",
                args.command,
                ctx.json,
            )
        sys.stderr.write(f"{PROG}: wrote {target}\n")
        if not ctx.json:
            return int(ExitCode.OK)
    ctx.emit(doc)
    return int(ExitCode.OK)


def _propose(args: argparse.Namespace, ctx: Context) -> int:
    assert ctx.con is not None
    try:
        run = proposals.propose(
            ctx.con, ctx.config, load_rubrics(), backend=_backend(args.backend, ctx.config)
        )
    except proposals.UnsafeTarget as exc:
        return _fail(ExitCode.USAGE, "unsafe_target", str(exc), args.command, ctx.json)
    for failure in run.draft_failures:
        sys.stderr.write(f"{PROG}: propose: {failure}\n")
    ctx.emit(run.summary)
    return int(ExitCode.PARTIAL if run.draft_failures else ExitCode.OK)


def _rehydrate(args: argparse.Namespace, ctx: Context) -> int:
    with open_read(ctx.config) as con:
        loc = engine.locate_event(con, args.event_id)
    if loc is None:
        raise UsageError(f"unknown event id {args.event_id}", code="unknown_event")
    try:
        data = engine.read_event(loc)
    except engine.SourceUnavailable as exc:
        return _fail(ExitCode.PARTIAL, exc.code, str(exc), args.command, ctx.json)
    if ctx.json:
        result: RehydrateResult = {
            "event_id": loc.event_id,
            "source_path": loc.source_path,
            "byte_offset": loc.byte_offset,
            "byte_length": loc.byte_length,
            "raw_base64": base64.b64encode(data).decode("ascii"),
        }
        ctx.emit(result)
    else:
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    return int(ExitCode.OK)


COMMANDS: dict[str, Command] = {
    "ingest": Command("ingest new or changed transcripts", True, _ingest_args, _ingest),
    "signals": Command("compute and list waste signals and rollups", True, _signals_args, _signals),
    "outcomes": Command(
        "link sessions to PRs and refresh PR outcomes", True, _outcomes_args, _outcomes
    ),
    "grade": Command("grade the worst sessions against the rubrics", True, _grade_args, _grade),
    "propose": Command("draft evidence-gated skill edits", True, _propose_args, _propose),
    "report": Command("analysis report as JSON or HTML", False, _report_args, _report),
    "query": Command("run read-only SQL", False, _query_args, _query),
    "rehydrate": Command(
        "print the raw source bytes of an event", False, _rehydrate_args, _rehydrate
    ),
    "status": Command("ingest state and database summary", False, _no_args, _status),
    "run": Command(
        "ingest, then signals, then outcomes, optionally grade with --grade",
        True,
        _run_args,
        _run,
    ),
}


def build_parser() -> argparse.ArgumentParser:
    # SUPPRESS defaults let --json/--config appear before or after the subcommand
    # without the subparser's default overwriting a value given at the top level.
    common = _Parser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="one JSON document on stdout"
    )
    common.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="config file (highest precedence)",
    )
    parser = _Parser(prog=PROG, description=DESCRIPTION, parents=[common])
    parser.add_argument("--version", action="version", version=version("slopsniffer"))
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    for name, command in COMMANDS.items():
        command.arguments(
            sub.add_parser(name, help=command.help, description=command.help, parents=[common])
        )
    return parser


def _dispatch(command_name: str, args: argparse.Namespace, as_json: bool) -> int:
    command = COMMANDS[command_name]
    config = load_config(getattr(args, "config", None))
    if not command.writes:
        return command.handler(args, Context(config, None, as_json))
    with open_write(config) as con:
        started = datetime.now(UTC)
        code = command.handler(args, Context(config, con, as_json))
        try:
            status.record_run(con, command_name, started, datetime.now(UTC), code)
        except duckdb.Error as exc:
            # The handler has already emitted its document; do not append a second one.
            logging.error("recording %s run failed: %s", command_name, exc)
            return int(ExitCode.PARTIAL)
        return code


def _fail(
    code: ExitCode,
    error: str,
    message: str,
    command: str | None,
    as_json: bool,
    **extra: int | None,
) -> int:
    sys.stderr.write(f"{PROG}: {message}\n")
    if as_json:
        doc: CliError = {"error": error, "message": message, "command": command}
        if "holder_pid" in extra:
            doc["holder_pid"] = extra["holder_pid"]
        sys.stdout.write(json.dumps(doc) + "\n")
    return int(code)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
    as_json = "--json" in argv
    command: str | None = None
    try:
        args = build_parser().parse_args(argv)
        as_json = getattr(args, "json", False)
        command = args.command
        return _dispatch(args.command, args, as_json)
    except DatabaseLocked as exc:
        return _fail(
            ExitCode.LOCKED,
            "database_locked",
            str(exc),
            command,
            as_json,
            holder_pid=exc.holder_pid,
        )
    except UsageError as exc:
        return _fail(ExitCode.USAGE, exc.code, str(exc), command, as_json)
    except duckdb.Error as exc:
        if command == "ingest":
            # Opening/configuring the database can fail before the engine can summarize.
            sys.stderr.write(f"{PROG}: ingest: {exc}\n")
            doc = engine.failed_summary(args.harness, str(exc))
            sys.stdout.write(json.dumps(doc) + "\n")
            return int(ExitCode.PARTIAL)
        return _fail(ExitCode.PARTIAL, "database_error", str(exc), command, as_json)
