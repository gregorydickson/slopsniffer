from pathlib import Path

import pytest

from tests.signals import support


def test_a_command_rerun_with_no_edit_between_is_signalled(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    # Runs differing only in whitespace, or in an array/string command shape, are one command.
    assert support.evidence(report, "rerun_loop") == {
        ("rerun-pos", support.ids(root, "rp-a1", "rp-a2", "rp-a3")),
        (support.CODEX_SESSION, support.ids(root, "cx-r1", "cx-r2", "cx-r3")),
    }


def test_reruns_separated_by_edits_are_not_signalled(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    assert "rerun-neg" in support.session_ids()
    assert "rerun-neg" not in support.subjects(report, "rerun_loop")
