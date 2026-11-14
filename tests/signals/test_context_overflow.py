from pathlib import Path

import pytest

from tests.signals import support


def test_compaction_and_summary_events_are_signalled_per_session(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    assert support.evidence(report, "context_overflow") == {
        ("compact-pos", support.ids(root, "cp-s1")),
        (support.CODEX_SESSION, support.ids(root, "cx-c1")),
    }


def test_other_system_records_are_not_compaction(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    assert "compact-neg" in support.session_ids()
    assert "compact-neg" not in support.subjects(report, "context_overflow")
