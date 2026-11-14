from pathlib import Path

import pytest

from tests.signals import support


def test_a_failed_call_retried_identically_is_signalled(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    assert support.evidence(report, "unchanged_retry") == {
        ("retry-pos", support.ids(root, "tp-u1", "tp-r1", "tp-u2")),
    }


def test_a_changed_retry_or_a_repeated_success_is_not_signalled(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    assert "retry-neg" in support.session_ids()
    assert "retry-neg" not in support.subjects(report, "unchanged_retry")
