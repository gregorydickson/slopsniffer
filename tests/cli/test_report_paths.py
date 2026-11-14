"""Report paths are safe to share without changing the source records or diff files."""

import json
from datetime import UTC, datetime
from html import unescape
from pathlib import Path

import pytest

from slopsniffer.config import load_config
from slopsniffer.db import open_write
from slopsniffer.grading.rubrics import load_rubrics
from slopsniffer.report import build, html
from slopsniffer.report.paths import display_text
from tests.grading.support import memory_db


def test_json_and_html_use_home_relative_paths_without_changing_storage(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = isolated_home / "person & name"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    skill = str(home / ".agents/skills/review/SKILL.md")
    outside = str(tmp_path / "outside/SKILL.md")
    sibling = str(home) + "-other/SKILL.md"
    diff = home / "change.diff"
    diff_text = f"--- {skill}\n+++ {outside}\n context {sibling}\n"
    diff.write_text(diff_text)
    with open_write(load_config()) as con:
        con.execute(
            "INSERT INTO proposals "
            "(proposal_id, skill, path, diff_path, status, evidence, cluster_key, created_at) "
            "VALUES (?, ?, ?, ?, 'proposed', '{}', 'paths', current_timestamp)",
            ["home-path", skill, outside, str(diff)],
        )
        report = build(con, load_rubrics(), datetime.now(UTC))
        original = con.execute("SELECT skill, path, diff_path FROM proposals").fetchall()
    proposal = report["sections"]["proposals"][0]
    assert proposal["skill"] == "~/.agents/skills/review/SKILL.md"
    assert proposal["path"] == outside
    assert proposal["diff_path"] == "~/change.diff"
    assert original == [(skill, outside, str(diff))]
    assert str(home) not in json.dumps(report)
    page = tmp_path / "report.html"
    html.write(page, report)
    rendered = unescape(page.read_text())
    assert f"--- ~/.agents/skills/review/SKILL.md\n+++ {outside}" in rendered
    assert sibling in rendered
    assert diff.read_text() == diff_text


@pytest.mark.parametrize("name", ["person ", "personX"])
def test_home_names_keep_trailing_spaces_and_letters_when_shortened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    home = tmp_path / name
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert display_text(f"'{home}/SKILL.md' and {home}-other/SKILL.md") == (
        f"'~/SKILL.md' and {home}-other/SKILL.md"
    )


def test_proposals_with_null_evidence_keep_their_file_and_merge_other_session_ids() -> None:
    with memory_db() as con:
        for pid, evidence in [("empty", None), ("full", '{"session_ids":["last","first"]}')]:
            con.execute(
                "INSERT INTO proposals "
                "(proposal_id, skill, path, diff_path, status, evidence, cluster_key, created_at) "
                "VALUES (?, 'review', '/draft.md', '/change.diff', 'proposed', ?, 'c', now())",
                [pid, evidence],
            )
        report = build(con, {}, datetime.now(UTC))
    assert report["sections"]["proposals"] == [
        {
            "skill": "review",
            "path": "/draft.md",
            "diff_path": "/change.diff",
            "evidence_sessions": ["first", "last"],
        }
    ]
