import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from slopsniffer.cli import COMMANDS, main
from tests.helpers import CLI_SCHEMAS_DIR, REPO_ROOT, load_schema, schema_registry

PRD_SUBCOMMANDS = {
    "ingest",
    "signals",
    "outcomes",
    "grade",
    "propose",
    "report",
    "query",
    "rehydrate",
    "status",
    "run",
}
UNKNOWN_EVENT = "0" * 32


def assert_valid_error(doc: object) -> None:
    schema = load_schema(CLI_SCHEMAS_DIR / "error.schema.json")
    Draft202012Validator(schema, registry=schema_registry()).validate(doc)


def test_registered_subcommands_are_the_prd_set() -> None:
    assert set(COMMANDS) == PRD_SUBCOMMANDS


def test_console_script_help_lists_every_subcommand() -> None:
    script = Path(sys.executable).parent / "slopsniffer"
    result = subprocess.run(
        [str(script), "--help"], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0
    listed = {
        line.split()[0]
        for line in result.stdout.splitlines()
        if line.startswith("    ") and not line[4].isspace()
    }
    assert listed == PRD_SUBCOMMANDS


@pytest.mark.parametrize(
    "argv",
    [["--json", "rehydrate", UNKNOWN_EVENT], ["rehydrate", UNKNOWN_EVENT, "--json"]],
    ids=["before", "after"],
)
def test_json_flag_accepted_before_or_after_subcommand(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    doc = json.loads(capsys.readouterr().out)
    assert (doc["command"], doc["error"]) == ("rehydrate", "unknown_event")


@pytest.mark.parametrize(
    "argv",
    [
        ["ingest", "--harness", "claude,bogus"],
        ["ingest", "--since", "yesterday"],
        ["ingest", "--workers", "0"],
        ["grade"],
        ["grade", "--worst", "2", "--backend", "gpt"],
        ["nonsense"],
        [],
    ],
)
def test_usage_errors_exit_2_with_json_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*argv, "--json"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert_valid_error(doc)
    assert doc["error"] == "usage"


def test_errors_without_json_leave_stdout_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rehydrate", UNKNOWN_EVENT]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown event id" in captured.err


def test_harness_list_is_parsed_in_canonical_order() -> None:
    from slopsniffer.cli import build_parser

    args = build_parser().parse_args(["ingest", "--harness", "pickle, claude"])
    assert args.harness == ("claude", "pickle")


def test_writer_leaves_repo_tree_untouched() -> None:
    before = git_status()
    main(["ingest", "--json"])
    assert git_status() == before


def git_status() -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout


def test_console_script_version_uses_installed_metadata() -> None:
    from importlib.metadata import version

    script = Path(sys.executable).parent / "slopsniffer"
    result = subprocess.run(
        [str(script), "--version"], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0
    assert result.stdout.strip() == version("slopsniffer")
    assert result.stderr == ""


def test_run_help_describes_pipeline_and_optional_grading(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["run", "--help"])
    assert exited.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "ingest, then signals, then outcomes, optionally grade with --grade" in help_text
