"""Every database-sourced string the HTML page shows is escaped, field by field."""

from html import escape

import pytest

from slopsniffer.contracts import Report
from slopsniffer.report.html import render

FIELDS = (
    "finding_column",
    "finding_cell",
    "rollup_dimension",
    "rollup_cell",
    "leaderboard_cell",
    "rubric",
    "grade_label",
    "proposal_skill",
    "proposal_path",
    "evidence_session",
    "diff",
    "cluster_key",
    "rejection_reason",
    "generated_at",
)


def payload(field: str) -> str:
    return f'<script data-field="{field}">x</script>'


def page() -> str:
    p = payload
    report: Report = {
        "generated_at": p("generated_at"),
        "sections": {
            "top_findings": [{p("finding_column"): p("finding_cell")}],
            "rollups": {p("rollup_dimension"): [{"key": p("rollup_cell")}]},
            "signal_leaderboard": [{"kind": p("leaderboard_cell")}],
            "grade_distribution": {p("rubric"): {p("grade_label"): 1}},
            "proposals": [
                {
                    "skill": p("proposal_skill"),
                    "path": p("proposal_path"),
                    "diff_path": "d",
                    "evidence_sessions": [p("evidence_session")],
                }
            ],
            "rejected_clusters": [
                {"cluster_key": p("cluster_key"), "reason": p("rejection_reason")}
            ],
        },
    }
    return render(report, {"d": p("diff")})


@pytest.mark.parametrize("field", FIELDS)
def test_the_field_is_shown_escaped_and_never_raw(field: str) -> None:
    html = page()
    assert payload(field) not in html
    assert escape(payload(field)) in html
