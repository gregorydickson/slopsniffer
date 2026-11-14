from pathlib import Path

import pytest

from tests.signals import support


def test_a_file_edited_churn_min_times_is_signalled(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    churn = {f"cp-e{i}" for i in range(1, 6)} | {"cp-f1", "cp-f2"}
    assert support.evidence(report, "edit_churn") == {
        ("churn-pos", support.ids(root, *churn)),
    }
    # The Codex patches are successful progressive edits, not failed repair cycles.
    assert support.CODEX_SESSION not in support.subjects(report, "edit_churn")


def test_edits_spread_below_the_threshold_per_file_are_not_signalled(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    assert "churn-neg" in support.session_ids()
    assert "churn-neg" not in support.subjects(report, "edit_churn")
