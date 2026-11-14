from pathlib import Path

import pytest

from tests.signals import support


def test_a_correcting_user_turn_after_an_assistant_turn_is_signalled(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    # The Codex user message between them holds only injected context, so it is no turn.
    assert support.evidence(report, "user_correction") == {
        ("correct-pos", support.ids(root, "co-a1", "co-u2")),
        (support.CODEX_SESSION, support.ids(root, "cx-m1", "cx-m2")),
    }


def test_tool_results_injected_context_and_turns_after_users_are_not_corrections(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    assert "correct-neg" in support.session_ids()
    assert "correct-neg" not in support.subjects(report, "user_correction")
