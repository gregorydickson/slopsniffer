"""The rendered page preserves report values and introduces no stray visible text."""

import errno
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest

from slopsniffer.contracts import Report
from slopsniffer.report import html


class VisibleReport(HTMLParser):
    def __init__(self, page: str) -> None:
        super().__init__()
        self.section = ""
        self.stack: list[str] = []
        self.content: dict[str, list[tuple[str, str]]] = {"": []}
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "section":
            self.section = dict(attrs)["id"] or ""
            self.content[self.section] = []
        if tag not in {"br", "meta"}:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack.pop() == tag
        if tag == "section":
            self.section = ""

    def handle_data(self, data: str) -> None:
        tag = self.stack[-1] if self.stack else ""
        if tag in {"style", "title"} or not data.strip():
            return
        self.content[self.section].append((tag, data if tag == "pre" else data.strip()))


def report_document() -> Report:
    return {
        "generated_at": "2026-11-10T12:00:00+00:00",
        "sections": {
            "top_findings": [
                {"event": "first", "details": {"z": 7, "a": ["é", True]}, "missing": None},
                {"event": "last", "at": datetime(2026, 11, 10, tzinfo=UTC)},
            ],
            "rollups": {
                "lane": [{"key": "claude", "severity": 2.5}],
                "project": [{"key": "widgets", "severity": 1.5}],
            },
            "signal_leaderboard": [{"kind": "rerun_loop", "sessions": 3}],
            "grade_distribution": {"verbosity": {"good": 2}, "efficiency": {"bad": 4}},
            "proposals": [
                {
                    "skill": "review",
                    "path": "/draft/review.md",
                    "diff_path": "review.diff",
                    "evidence_sessions": ["s-first", "s-last"],
                },
                {
                    "skill": "deploy",
                    "path": "/draft/deploy.md",
                    "diff_path": "missing.diff",
                    "evidence_sessions": [],
                },
            ],
            "rejected_clusters": [{"cluster_key": "old", "reason": "already addressed"}],
        },
    }


def test_rendered_sections_keep_table_values_evidence_and_missing_diff_fallback() -> None:
    page = html.render(report_document(), {"review.diff": "-old\n+new\n"})
    assert VisibleReport(page).content == {
        "": [
            ("h1", "slopsniffer report"),
            ("p", "Generated 2026-11-10 12:00 UTC"),
        ],
        "top_findings": [
            ("h2", "Top findings"),
            ("th", "event"),
            ("th", "details"),
            ("th", "missing"),
            ("th", "at"),
            ("td", "first"),
            ("td", '{"a": ["\\u00e9", true], "z": 7}'),
            ("td", "last"),
            ("td", '"2026-11-10 00:00:00+00:00"'),
        ],
        "rollups": [
            ("h2", "Rollups by lane, skill and project"),
            ("h3", "lane"),
            ("th", "key"),
            ("th", "severity"),
            ("td", "claude"),
            ("td", "2.5"),
            ("h3", "project"),
            ("th", "key"),
            ("th", "severity"),
            ("td", "widgets"),
            ("td", "1.5"),
        ],
        "signal_leaderboard": [
            ("h2", "Signal leaderboard (includes informational observations)"),
            ("th", "kind"),
            ("th", "sessions"),
            ("td", "rerun_loop"),
            ("td", "3"),
        ],
        "grade_distribution": [
            ("h2", "Grade distribution per rubric"),
            ("h3", "verbosity"),
            ("th", "label"),
            ("th", "grades"),
            ("td", "good"),
            ("td", "2"),
            ("h3", "efficiency"),
            ("th", "label"),
            ("th", "grades"),
            ("td", "bad"),
            ("td", "4"),
        ],
        "proposals": [
            ("h2", "Proposals"),
            ("h3", "review"),
            ("p", "Draft: /draft/review.md"),
            ("p", "Evidence sessions: s-first, s-last"),
            ("pre", "-old\n+new\n"),
            ("h3", "deploy"),
            ("p", "Draft: /draft/deploy.md"),
            ("p", "Evidence sessions:"),
        ],
        "rejected_clusters": [
            ("h2", "Rejected clusters"),
            ("th", "cluster_key"),
            ("th", "reason"),
            ("td", "old"),
            ("td", "already addressed"),
        ],
    }


def test_html_write_and_diff_read_use_utf8_with_alternate_text_defaults(
    tmp_path: Path, non_utf8_text_defaults: None
) -> None:
    report = report_document()
    output = tmp_path / "output"
    output.mkdir()
    diff = output / "change.diff"
    diff.write_bytes("-café\n+naïve\n".encode())
    proposal = report["sections"]["proposals"][0]
    proposal["diff_path"] = str(diff)
    proposal["skill"] = "révision"
    target = output / "report.html"
    html.write(target, report)
    content = VisibleReport(target.read_bytes().decode("utf-8")).content["proposals"]
    assert ("h3", "révision") in content
    assert ("pre", "-café\n+naïve\n") in content
    assert {path.name for path in output.iterdir()} == {diff.name, target.name}


def test_write_failure_before_temp_creation_preserves_the_original_error_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    target = output / "report.html"
    target.write_bytes(b"previous report")
    temp = target.with_name(f".{target.name}.tmp")
    failure = OSError(errno.ENOSPC, "No space left on device", str(temp))

    def full_disk(path: Path, *args: object, **kwargs: object) -> int:
        assert path == temp
        raise failure

    monkeypatch.setattr(Path, "write_text", full_disk)
    with pytest.raises(OSError) as caught:
        html.write(target, report_document())
    assert caught.value is failure
    assert target.read_bytes() == b"previous report"
    assert {path.name for path in output.iterdir()} == {target.name}
