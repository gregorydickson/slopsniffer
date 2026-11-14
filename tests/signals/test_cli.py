import json
from pathlib import Path

import pytest

from slopsniffer.cli import main
from slopsniffer.contracts import SignalsReport
from tests.ingest.support import query
from tests.signals import support


def harnesses(report: SignalsReport) -> set[str]:
    return {s["harness"] for s in report["signals"]}


def test_project_filter_excludes_sessions_of_other_projects(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    everything = support.run(capsys)
    assert "rerun-pos" in {s["session_id"] for s in everything["signals"]}
    report = support.run(capsys, "--project", "other")
    assert {s["session_id"] for s in report["signals"]} == {"retry-pos"}
    assert {row["key"] for dim in ("lane", "project") for row in report["rollups"][dim]} == {
        "claude",
        "other",
    }


def test_kind_and_lane_filters_narrow_the_signals(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    kinds = support.run(capsys, "--kind", "rerun_loop", "--kind", "edit_churn")
    assert {s["kind"] for s in kinds["signals"]} == {"rerun_loop", "edit_churn"}
    codex = support.run(capsys, "--lane", "codex")
    assert harnesses(codex) == {"codex"}
    assert {row["key"] for row in codex["rollups"]["lane"]} == {"codex"}


def test_since_keeps_only_sessions_started_in_the_window(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys, "--since", "2026-11-10")
    assert report["window"]["since"] == "2026-11-10"
    # Every fixture session starts on 2026-11-09; skill subjects have no start and stay.
    assert harnesses(report) == {"skills"}
    assert all(rows == [] for rows in report["rollups"].values())


def test_a_rerun_replaces_the_signals_rather_than_adding_to_them(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = {s["signal_id"] for s in support.run(capsys)["signals"]}
    second = {s["signal_id"] for s in support.run(capsys)["signals"]}
    stored = [row[0] for row in query("SELECT signal_id FROM signals")]
    assert first == second == set(stored)
    assert len(stored) == len(set(stored))


@pytest.mark.parametrize(
    "argv", [["--kind", "bogus"], ["--lane", "bogus"], ["--since", "yesterday"]]
)
def test_a_bad_filter_exits_2_with_a_json_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["signals", "--json", *argv]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["error"] == "usage"
    assert doc["message"].startswith(f"signals: argument {argv[0]}")
