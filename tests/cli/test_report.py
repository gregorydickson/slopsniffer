"""``report``: the JSON sections and the self-contained HTML page carry the same sections."""

import re
from collections import Counter
from dataclasses import replace
from html import escape
from pathlib import Path
from typing import Any

import pytest

from slopsniffer.cli import main
from slopsniffer.report import SECTIONS, TOP_FINDINGS
from tests.cli.support import cli, fixture_home, install_gh, install_graders, validate
from tests.ingest.support import jsonl, query
from tests.proposals.support import RUBRICS, Grade, Session, failing, real_home, seed

SKIPPED = "Skipped the review checklist before merging."
IGNORED = "Ignored the deploy runbook."
# Markup planted in a real skill file: it reaches the page only through the proposal diff.
PLANTED = '<a href="https://example.invalid/x">x</a><img src="http://example.invalid/p.png">'
SECTION_ID = re.compile(r'<section id="([^"]+)"')
EXTERNAL = re.compile(r"""\b(?:src|href)\s*=\s*["']?\s*(?:https?:)?//""", re.IGNORECASE)
SESSIONS = [
    Session("g-a", (failing("code-quality", "block", SKIPPED),), skills=("review",)),
    Session("g-b", (failing("code-quality", "block", SKIPPED),), skills=("review",)),
    Session("g-c", (failing("procedure-compliance", "noncompliant", IGNORED),), skills=("deploy",)),
    Session(
        "g-stale",
        (
            replace(
                failing("efficiency", RUBRICS["efficiency"].labels[-1].value, SKIPPED), stale=True
            ),
        ),
    ),
    Session("g-error", (Grade("verbosity", None),)),
]
TWICE_CORRECTED = "twice-corrected"


def twice_corrected(claude_root: Path) -> None:
    """A Claude session where the user corrects the assistant twice: two signals of one kind
    in one session, so a kind's signal count and its session count differ."""
    turns = [("assistant", "Done."), ("user", "That's wrong.")] * 2
    path = claude_root / "-work-report" / f"{TWICE_CORRECTED}.jsonl"
    path.parent.mkdir()
    path.write_bytes(
        jsonl(
            *(
                {
                    "sessionId": TWICE_CORRECTED,
                    "uuid": f"{TWICE_CORRECTED}-{i}",
                    "cwd": "/work/report",
                    "timestamp": f"2026-11-09T10:00:0{i}.000Z",
                    "type": role,
                    "message": {"role": role, "content": [{"type": "text", "text": text}]},
                }
                for i, (role, text) in enumerate(turns)
            )
        )
    )


@pytest.fixture
def populated(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    """The fixture home run once, graded sessions seeded through the grades writer, and the
    ``propose`` output their failures produced."""
    twice_corrected(fixture_home(isolated_home)["claude"])
    install_gh(tmp_path, monkeypatch)
    code, doc = cli(capsys, "run")
    assert code == 0, doc
    home = real_home(tmp_path)
    skill = home.skill_file("review")
    skill.write_text(skill.read_text() + PLANTED + "\n")
    install_graders(tmp_path, monkeypatch)
    seed(home, SESSIONS)
    # seed() rebuilt the skill inventory: recompute so every stored signal's subject exists.
    assert cli(capsys, "signals")[0] == 0
    code, proposed = cli(capsys, "propose")
    assert code == 0, proposed
    assert proposed["proposals"] and proposed["rejected"]
    out: dict[str, Any] = proposed
    return out


def html_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[str, dict[str, Any]]:
    page = tmp_path / "report.html"
    code, doc = cli(capsys, "report", "--html", str(page))
    assert code == 0, doc
    validate("report", doc)
    return page.read_text("utf-8"), doc


def test_html_section_ids_equal_the_json_section_keys(
    populated: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    page, doc = html_report(tmp_path, capsys)
    ids = SECTION_ID.findall(page)
    assert len(ids) == len(set(ids))
    assert set(ids) == set(doc["sections"]) == set(SECTIONS)


def test_the_page_loads_nothing_external_and_escapes_what_it_shows(
    populated: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    page, doc = html_report(tmp_path, capsys)
    assert EXTERNAL.findall(page) == []
    assert re.findall(r"<(?:script|link|iframe|img)\b", page, re.IGNORECASE) == []
    diffs = [Path(p["diff_path"]).read_text("utf-8") for p in doc["sections"]["proposals"]]
    assert any(PLANTED in diff for diff in diffs)
    assert {escape(diff) in page for diff in diffs} == {True}


def test_sections_carry_the_stored_signals_grades_and_proposals(
    populated: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = cli(capsys, "report")
    assert code == 0, doc
    validate("report", doc)
    sections = doc["sections"]

    ranked = query(
        "SELECT signal_id FROM signals WHERE severity > 0 ORDER BY severity DESC, signal_id"
    )
    assert ranked
    assert [s["signal_id"] for s in sections["top_findings"]] == [
        sid for (sid,) in ranked[:TOP_FINDINGS]
    ]
    by_kind: dict[str, list[tuple[str, float]]] = {}
    for kind, session, severity in query("SELECT kind, session_id, severity FROM signals"):
        by_kind.setdefault(kind, []).append((session, severity))
    assert {r["kind"]: r for r in sections["signal_leaderboard"]} == {
        kind: {
            "kind": kind,
            "signals": len(rows),
            "sessions": len({session for session, _ in rows}),
            "total_severity": sum(severity for _, severity in rows),
            "max_severity": max(severity for _, severity in rows),
        }
        for kind, rows in by_kind.items()
    }
    assert any(len(rows) > len({session for session, _ in rows}) for rows in by_kind.values())
    totals = [r["total_severity"] for r in sections["signal_leaderboard"]]
    assert totals == sorted(totals, reverse=True)

    assert set(sections["rollups"]) == {"lane", "skill", "project", "week"}
    for dim in sections["rollups"]:
        stored = {key for (key,) in query(f"SELECT key FROM rollup_by_{dim}")}
        assert {row["key"] for row in sections["rollups"][dim]} == stored

    current = Counter(
        (g.rubric, g.verdict.label)
        for s in SESSIONS
        for g in s.grades
        if g.verdict is not None and not g.stale
    )
    expected: dict[str, dict[str, int]] = {name: {} for name in RUBRICS}
    for (rubric, label), n in current.items():
        expected[rubric][label] = n
    assert sections["grade_distribution"] == expected

    assert sections["proposals"] == populated["proposals"]
    assert sections["rejected_clusters"] == populated["rejected"]


def test_a_missing_diff_file_is_reported_in_place_of_the_diff(
    populated: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gone = Path(populated["proposals"][0]["diff_path"])
    gone.unlink()
    page, doc = html_report(tmp_path, capsys)
    assert doc["sections"]["proposals"] == populated["proposals"]
    assert "(diff unavailable:" in page


def test_a_database_whose_signals_were_never_computed_reports_empty_sections(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    page, doc = html_report(tmp_path, capsys)
    sections = doc["sections"]
    assert (sections["top_findings"], sections["signal_leaderboard"]) == ([], [])
    assert sections["rollups"] == {"lane": [], "skill": [], "project": [], "week": []}
    assert sections["grade_distribution"] == {name: {} for name in RUBRICS}
    assert set(SECTION_ID.findall(page)) == set(SECTIONS)


def test_html_without_json_prints_nothing_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    page = tmp_path / "plain.html"
    assert main(["report", "--html", str(page)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(page) in captured.err
    assert set(SECTION_ID.findall(page.read_text("utf-8"))) == set(SECTIONS)


def test_html_into_a_missing_directory_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = cli(capsys, "report", "--html", str(tmp_path / "absent" / "r.html"))
    assert code == 2
    validate("error", doc)
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("make_target", ["directory", "unwritable"])
def test_html_that_cannot_be_written_exits_2_and_leaves_no_temp_file(
    make_target: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = tmp_path / "out"
    parent.mkdir()
    target = parent / "r.html"
    if make_target == "directory":
        target.mkdir()
    else:
        parent.chmod(0o500)
    try:
        code, doc = cli(capsys, "report", "--html", str(target))
        assert code == 2
        validate("error", doc)
        assert doc["error"] == "write_failed"
        assert {p.name for p in parent.iterdir()} == ({"r.html"} if target.is_dir() else set())
    finally:
        parent.chmod(0o700)
