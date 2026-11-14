"""Readable report headings and presentation without changing JSON measurements."""

import json
from datetime import UTC, datetime

import pytest

from slopsniffer import report
from slopsniffer.grading.rubrics import load_rubrics
from slopsniffer.report import html
from slopsniffer.signals import runner
from tests.test_value_contracts import database, event


@pytest.mark.parametrize(
    ("title", "parent_title", "prompt", "expected"),
    [
        ("Own title", "Parent title", "First request", "Own title"),
        (None, "Parent <title>", "First request", "Subagent of Parent <title>"),
        (None, None, "First <request>\nwith details", "project · First <request> with details"),
        ("  ", "  ", "First request", "project · First request"),
        (None, None, None, "project · Session without a recorded user prompt"),
    ],
)
def test_report_headings_use_titles_then_parent_then_first_prompt(
    title: str | None, parent_title: str | None, prompt: str | None, expected: str
) -> None:
    from html import escape

    with database() as con:
        con.execute(
            "INSERT INTO sessions (session_id,harness,title,project,parent_session_id) "
            "VALUES ('opaque-child-id','claude',?,'project','parent'), "
            "('parent','claude',?,'project',NULL)",
            [title, parent_title],
        )
        event(con, "startup", "opaque-child-id", 0)
        con.execute(
            "INSERT INTO messages VALUES ('startup', 'user', ?)",
            ["<system-reminder>Injected instructions</system-reminder>"],
        )
        if prompt:
            for seq, text in [(1, prompt), (2, "Later request must not be the title")]:
                event(con, f"prompt-{seq}", "opaque-child-id", seq)
                con.execute("INSERT INTO messages VALUES (?, 'user', ?)", [f"prompt-{seq}", text])
        con.execute(runner._sql("rollups"))
        severity = 364.3333333333333
        con.execute(
            "INSERT INTO signals VALUES "
            "('finding','orphaned_session','opaque-child-id',?,[], '{}', now())",
            [severity],
        )
        now = datetime(2026, 11, 13, 16, 25, 49, 123456, tzinfo=UTC)
        document = report.build(con, load_rubrics(), now)
        finding = document["sections"]["top_findings"][0]
        assert finding["title"] == expected
        assert finding["severity"] == severity
        assert document["generated_at"] == now.isoformat()
        page = html.render(document, {})
        assert f"<h3>{escape(expected)}</h3>" in page
        assert "<h3>opaque-child-id</h3>" not in page
        assert "severity 364.3</p>" in page
        assert "Generated 2026-11-13 16:25 UTC</p>" in page
        assert (
            json.loads(json.dumps(document))["sections"]["top_findings"][0]["severity"] == severity
        )
