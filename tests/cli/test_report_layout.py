"""Measure rendered overflow in a real browser, including unbroken evidence and paths."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from slopsniffer.contracts import Report
from slopsniffer.report.html import render


def chrome() -> str:
    for name in ("chromium", "chromium-browser", "google-chrome"):
        if executable := shutil.which(name):
            return executable
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac.is_file():
        return str(mac)
    pytest.skip("Install Chrome or Chromium to run report layout tests")


@pytest.mark.parametrize("width", [640, 1280])
def test_long_findings_do_not_overflow_the_page(tmp_path: Path, width: int) -> None:
    evidence = [f"event-{i}-" + "a" * 80 for i in range(50)]
    skill = "/skills/" + "longdirectory" * 250 + "/SKILL.md"
    report: Report = {
        "generated_at": "2026-11-10",
        "sections": {
            "top_findings": [{"evidence_event_ids": evidence, "skill": skill}],
            "rollups": {},
            "signal_leaderboard": [],
            "grade_distribution": {},
            "proposals": [],
            "rejected_clusters": [],
        },
    }
    page = render(report, {})
    assert re.search(r"\b(?:src|href)\s*=", page) is None
    # This probe belongs to the test only; the report itself needs no JavaScript.
    probe = """<script>
    const root = document.documentElement;
    document.body.dataset.layout = root.scrollWidth <= root.clientWidth ? 'fits' : 'overflows';
    </script>"""
    target = tmp_path / "report.html"
    target.write_text(page.replace("</body>", probe + "</body>"))
    result = subprocess.run(
        [
            chrome(),
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            f"--user-data-dir={tmp_path / 'browser'}",
            f"--window-size={width},900",
            "--dump-dom",
            target.as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert 'data-layout="fits"' in result.stdout, (result.stdout, result.stderr)
    assert skill in result.stdout
    for event in evidence:
        assert event in result.stdout
