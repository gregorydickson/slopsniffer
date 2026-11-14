"""The proposal entry point preserves input, output and persisted evidence."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from slopsniffer.config import load_config
from slopsniffer.db import open_write
from slopsniffer.grading.backends import Raw, _Run
from slopsniffer.grading.rubrics import load_rubrics
from slopsniffer.proposals import propose
from tests.proposals.support import (
    Session,
    failing,
    install_fakes,
    invocation_event,
    real_home,
    seed,
)


@pytest.mark.parametrize("answer", ["improved", "unchanged", "invalid", "unreadable"])
def test_proposal_entry_point_passes_the_real_file_and_retains_cited_evidence(
    tmp_path: Path,
    answer: str,
    monkeypatch: pytest.MonkeyPatch,
    non_utf8_text_defaults: None,
) -> None:
    install_fakes(tmp_path, monkeypatch).scenario(codex=["fail"], claude=["fail"])
    home = real_home(tmp_path)
    real = home.skill_file("review")
    current = real.read_text("utf-8") + "\nKeep café instructions.\n"
    real.write_text(current, "utf-8")
    sessions = [
        Session(
            sid,
            (
                failing(
                    "code-quality", "block", "Skipped tests.", [invocation_event(sid, "review")]
                ),
            ),
            skills=("review",),
        )
        for sid in ("s-a", "s-b")
    ]
    seed(home, sessions)
    workdirs: list[Path] = []

    def execute(schema: Mapping[str, Any], prompt: str, cwd: Path) -> Raw:
        assert cwd.is_dir()
        assert f"# Instruction file: review\nPath: {real}\n\n{current}" in prompt
        assert "- [code-quality] sessions s-a, s-b" in prompt
        workdirs.append(cwd)
        output: object = {
            "propose": True,
            "reason": "Repeated missed tests.",
            "skill_md": current if answer == "unchanged" else "Improved é\n",
        }
        if answer == "invalid":
            output = None
        return Raw(_Run(datetime.now(UTC), 0, 0, "", "", None), output, (None, None))

    if answer == "unreadable":
        real.unlink()
    with open_write(load_config()) as con:
        result = propose(
            con, load_config(), load_rubrics(), backend="codex", executors={"codex": execute}
        )
        stored = con.execute("SELECT status, path, diff_path FROM proposals").fetchall()
    if answer == "improved":
        folder = load_config().data_dir / "proposals" / "review"
        assert result.summary == {
            "proposals": [
                {
                    "skill": "review",
                    "path": str(folder / "SKILL.md"),
                    "diff_path": str(folder / "change.diff"),
                    "evidence_sessions": ["s-a", "s-b"],
                }
            ],
            "rejected": [],
        }
        assert result.draft_failures == []
        assert stored == [("proposed", str(folder / "SKILL.md"), str(folder / "change.diff"))]
        evidence = json.loads((folder / "evidence.json").read_text("utf-8"))
        assert evidence["event_ids"] == [invocation_event(s.session_id, "review") for s in sessions]
    else:
        assert result.summary["proposals"] == []
        assert stored == [("rejected", None, None)]
        if answer == "invalid":
            assert result.draft_failures == ["review: codex: invalid output: no output"]
        else:
            assert result.draft_failures == []
            reason = result.summary["rejected"][0]["reason"]
            if answer == "unchanged":
                assert reason == "the grading backend returned the file unchanged"
            else:
                assert reason.startswith(f"the real file {real} cannot be read:")
    assert all(not p.exists() for p in workdirs)


def test_failed_proposal_storage_rolls_back_the_previous_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from slopsniffer import proposals

    home = real_home(tmp_path)
    seed(
        home,
        [Session("s", (failing("code-quality", "block", "Skipped tests"),), skills=("review",))],
    )
    config = load_config()
    with open_write(config) as con:
        propose(con, config, load_rubrics(), backend="codex", executors={})
        previous = con.execute(
            "SELECT proposal_id, cluster_key, status, reason, path, diff_path, evidence "
            "FROM proposals"
        ).fetchall()

        def fail_insert(connection: object, rows: object) -> None:
            raise ValueError("synthetic write failure")

        monkeypatch.setattr(proposals, "_insert", fail_insert)
        with pytest.raises(ValueError, match="synthetic write failure"):
            propose(con, config, load_rubrics(), backend="codex", executors={})
        assert (
            con.execute(
                "SELECT proposal_id, cluster_key, status, reason, path, diff_path, evidence "
                "FROM proposals"
            ).fetchall()
            == previous
        )
        # The failed replacement closes its transaction, allowing another writer.
        con.execute("BEGIN TRANSACTION")
        con.execute("ROLLBACK")
