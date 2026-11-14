from pathlib import Path

import pytest

from tests.signals import support


def test_unanswered_last_tool_uses_and_stale_active_runs_are_signalled(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    # Meta records after the unanswered use do not hide it; a stale run's evidence is its
    # most recently written log event.
    assert support.evidence(report, "orphaned_session") == {
        ("orphan-pos", support.ids(root, "op-u1")),
        (support.CODEX_SESSION, support.ids(root, "cx-o1")),
        ("stale-run", support.ids(root, "pk-stale-2")),
    }


def test_answered_sessions_fresh_runs_and_inactive_runs_are_not_signalled(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    assert "orphan-neg" in support.session_ids()
    runs = {p.name for p in (root / "pickle").iterdir()}
    assert {"fresh-run", "idle-run"} <= runs
    signalled = support.subjects(report, "orphaned_session")
    assert {"orphan-neg", "fresh-run", "idle-run"}.isdisjoint(signalled)
