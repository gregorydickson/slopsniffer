"""Thresholds come from the config: moving one flips a fixture across the signal boundary."""

import json
from pathlib import Path

import pytest

from slopsniffer.config import packaged_defaults
from tests.signals import support

DEFAULTS = packaged_defaults()["signals"]


def with_signals(tmp_path: Path, **values: object) -> list[str]:
    # JSON spells these ints and string lists exactly as TOML does.
    lines = ["[signals]", *(f"{key} = {json.dumps(value)}" for key, value in values.items())]
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines) + "\n")
    return ["--config", str(path)]


def test_raising_rerun_min_past_a_loop_unsignals_it(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "rerun-pos" in support.subjects(support.run(capsys), "rerun_loop")
    raised = with_signals(tmp_path, rerun_min=DEFAULTS["rerun_min"] + 1)
    assert "rerun-pos" not in support.subjects(support.run(capsys, *raised), "rerun_loop")


def test_churn_min_moves_both_ways(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    default = support.subjects(support.run(capsys), "edit_churn")
    assert ("churn-pos" in default, "churn-neg" in default) == (True, False)
    raised = with_signals(tmp_path, churn_min=DEFAULTS["churn_min"] + 1)
    assert "churn-pos" not in support.subjects(support.run(capsys, *raised), "edit_churn")
    lowered = with_signals(tmp_path, churn_min=DEFAULTS["churn_min"] - 1)
    a = support.ids(root, *(f"cn-a{i}" for i in range(1, 5)), "cn-f1", "cn-f2")
    b = support.ids(root, *(f"cn-b{i}" for i in range(1, 5)), "cn-f1", "cn-f2")
    churn = support.evidence(support.run(capsys, *lowered), "edit_churn")
    assert {("churn-neg", a), ("churn-neg", b)} <= churn


def test_a_wider_abandon_window_catches_a_late_abandonment(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "abandon-late" not in support.subjects(support.run(capsys), "skill_abandoned")
    wider = with_signals(tmp_path, abandon_window=DEFAULTS["abandon_window"] * 2)
    assert "abandon-late" in support.subjects(support.run(capsys, *wider), "skill_abandoned")


def test_orphan_hours_beyond_a_runs_silence_unsignals_it(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "stale-run" in support.subjects(support.run(capsys), "orphaned_session")
    patient = with_signals(tmp_path, orphan_hours=2 * support.STALE_SECONDS // 3600)
    assert "stale-run" not in support.subjects(support.run(capsys, *patient), "orphaned_session")


def test_the_correction_lexicon_decides_what_a_correction_is(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    default = support.subjects(support.run(capsys), "user_correction")
    assert ("correct-pos" in default, "correct-neg" in default) == (True, False)
    replaced = with_signals(tmp_path, correction_lexicon=["thanks"])
    swapped = support.subjects(support.run(capsys, *replaced), "user_correction")
    assert ("correct-pos" in swapped, "correct-neg" in swapped) == (False, True)
