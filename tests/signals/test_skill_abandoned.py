from pathlib import Path

import pytest

from tests.signals import support


def test_a_skill_followed_by_an_error_or_no_tool_call_before_the_end_is_signalled(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    assert support.evidence(report, "skill_abandoned") == {
        ("abandon-err", support.ids(root, "ae-s1", "ae-r2")),
    }


def test_a_skill_that_keeps_working_or_outlives_the_window_is_not_signalled(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    negatives = {"abandon-neg", "abandon-late", "abandon-pos", "abandon-slash"}
    assert negatives <= support.session_ids()
    assert negatives.isdisjoint(support.subjects(report, "skill_abandoned"))
