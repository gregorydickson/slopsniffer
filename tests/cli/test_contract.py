"""Every subcommand's ``--json`` document matches ``schemas/cli/<cmd>.schema.json`` and its exit
code matches the scenario: 0 ok (including a rate-limited stop), 2 usage, 3 partial failure.

Exit 4 (another writer holds the lock) is asserted for every writing subcommand in
``test_lock.py``. :func:`tests.cli.support.cli` asserts stdout holds exactly one document, so logs
reaching stdout fail every test here.
"""

import os
from collections import Counter
from pathlib import Path

import pytest

from slopsniffer.cli import COMMANDS
from slopsniffer.contracts import Harness
from tests.cli.support import (
    FILE_LANES,
    PR,
    cli,
    fixture_home,
    install_gh,
    install_graders,
    source_files,
)
from tests.cli.support import validate as matches
from tests.ingest.support import query
from tests.outcomes.support import FakeGh
from tests.proposals.support import Fakes, Session, failing, real_home, seed

SKIPPED = "Skipped the review checklist before merging."
TESTED = {
    "ingest",
    "signals",
    "outcomes",
    "grade",
    "propose",
    "report",
    "query",
    "rehydrate",
    "status",
    "run",
}
unreadable = pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    return install_gh(tmp_path, monkeypatch)


@pytest.fixture
def graders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    return install_graders(tmp_path, monkeypatch)


@pytest.fixture
def home(isolated_home: Path, gh: FakeGh, graders: Fakes) -> dict[Harness, Path]:
    del gh, graders
    return fixture_home(isolated_home)


@pytest.fixture
def ingested(home: dict[Harness, Path], capsys: pytest.CaptureFixture[str]) -> dict[Harness, Path]:
    assert cli(capsys, "ingest")[0] == 0
    return home


def grown_and_unreadable(home: dict[Harness, Path]) -> Path:
    """An ingested file grown since, so it must be read again, and made unreadable."""
    path = Path(sorted(source_files(home)["claude"])[0])
    with path.open("a") as f:
        f.write('{"type": "user", "sessionId": "grown"}\n')
    path.chmod(0)
    return path


def test_every_subcommand_has_a_contract_test() -> None:
    assert set(COMMANDS) == TESTED


@unreadable
def test_ingest(ingested: dict[Harness, Path], capsys: pytest.CaptureFixture[str]) -> None:
    code, doc = cli(capsys, "ingest")
    assert code == 0
    matches("ingest", doc)

    broken = grown_and_unreadable(ingested)
    try:
        code, doc = cli(capsys, "ingest")
    finally:
        broken.chmod(0o644)
    assert code == 3
    matches("ingest", doc)
    assert doc["harnesses"]["claude"]["files_failed"] == [str(broken)]


def test_signals(ingested: dict[Harness, Path], capsys: pytest.CaptureFixture[str]) -> None:
    code, doc = cli(capsys, "signals")
    assert code == 0
    matches("signals", doc)
    assert doc["signals"]

    code, doc = cli(capsys, "signals", "--kind", "bogus")
    assert code == 2
    matches("error", doc)


@pytest.mark.parametrize(
    ("scenario", "code", "schema"),
    [
        ({}, 0, "outcomes"),
        ({"rate": [0]}, 0, "outcomes"),
        ({"graphql_fails": True}, 3, "outcomes"),
        ({"authenticated": False}, 3, "error"),
    ],
    ids=["ok", "rate_limited", "query_failed", "unauthenticated"],
)
def test_outcomes(
    scenario: dict[str, object],
    code: int,
    schema: str,
    ingested: dict[Harness, Path],
    gh: FakeGh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if scenario:
        gh.scenario(**scenario)
    got, doc = cli(capsys, "outcomes")
    assert got == code, doc
    matches(schema, doc)
    if scenario.get("rate"):
        assert doc["rate_limited"] is True


@pytest.mark.parametrize(
    ("behaviour", "code"), [("ok", 0), ("not_json", 3)], ids=["ok", "unusable_answers"]
)
def test_grade(
    behaviour: str,
    code: int,
    ingested: dict[Harness, Path],
    graders: Fakes,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli(capsys, "signals")[0] == 0
    graders.scenario(codex=[behaviour], claude=[behaviour])
    got, doc = cli(capsys, "grade", "--worst", "1")
    assert got == code, doc
    matches("grade", doc)
    assert doc["selected_session_ids"]
    assert bool(doc["chunks_errored"]) == bool(code)


@pytest.mark.parametrize(
    ("behaviour", "code"), [("ok", 0), ("fail", 3)], ids=["ok", "draft_failed"]
)
def test_propose(
    behaviour: str,
    code: int,
    tmp_path: Path,
    graders: Fakes,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pair = (failing("code-quality", "block", SKIPPED),)
    seed(real_home(tmp_path), [Session(s, pair, skills=("review",)) for s in ("p-a", "p-b")])
    graders.scenario(codex=[behaviour], claude=[behaviour])
    got, doc = cli(capsys, "propose")
    assert got == code, doc
    matches("propose", doc)
    assert bool(doc["proposals"]) == (code == 0)


def test_report(
    ingested: dict[Harness, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = cli(capsys, "report")
    assert code == 0
    matches("report", doc)

    code, doc = cli(capsys, "report", "--html", str(tmp_path / "absent" / "r.html"))
    assert code == 2
    matches("error", doc)


def test_query(ingested: dict[Harness, Path], capsys: pytest.CaptureFixture[str]) -> None:
    code, doc = cli(capsys, "query", "SELECT count(*) AS n FROM events")
    assert code == 0
    matches("query", doc)

    code, doc = cli(capsys, "query", "DELETE FROM events")
    assert code == 2
    matches("error", doc)


def test_rehydrate(ingested: dict[Harness, Path], capsys: pytest.CaptureFixture[str]) -> None:
    ((event,),) = query("SELECT min(event_id) FROM events")
    code, doc = cli(capsys, "rehydrate", event)
    assert code == 0
    matches("rehydrate", doc)
    assert doc["event_id"] == event

    code, doc = cli(capsys, "rehydrate", "0" * 32)
    assert code == 2
    matches("error", doc)


def test_status(ingested: dict[Harness, Path], capsys: pytest.CaptureFixture[str]) -> None:
    code, doc = cli(capsys, "status")
    assert code == 0
    matches("status", doc)


def test_run(home: dict[Harness, Path], capsys: pytest.CaptureFixture[str]) -> None:
    code, doc = cli(capsys, "run")
    assert code == 0, doc
    matches("run", doc)
    assert "grade" not in doc

    code, doc = cli(capsys, "run", "--grade", "--worst", "1")
    assert code == 0, doc
    matches("run", doc)
    assert doc["grade"]["selected_session_ids"]


@pytest.mark.parametrize("argv", [["--grade"], ["--worst", "1"]], ids=["grade", "worst"])
def test_run_needs_grade_and_worst_together(
    argv: list[str], home: dict[Harness, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = cli(capsys, "run", *argv)
    assert code == 2
    matches("error", doc)
    assert query("SELECT path FROM source_files") == []


@unreadable
def test_run_exits_with_the_worst_step(
    home: dict[Harness, Path], gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(capsys, "run")[0] == 0
    broken = grown_and_unreadable(home)
    try:
        code, doc = cli(capsys, "run")
    finally:
        broken.chmod(0o644)
    assert code == 3
    matches("run", doc)
    assert doc["ingest"]["harnesses"]["claude"]["files_failed"] == [str(broken)]
    assert doc["outcomes"]["prs_cached"] == 1


def test_run_without_a_usable_gh_still_reports_every_step(
    home: dict[Harness, Path], gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    gh.scenario(authenticated=False)
    code, doc = cli(capsys, "run")
    assert code == 3
    matches("run", doc)
    assert {h for h in FILE_LANES if doc["ingest"]["harnesses"][h]["files_new"]} == set(FILE_LANES)
    assert doc["outcomes"]["prs_fetched"] == 0
    assert set(query("SELECT repo, pr_number FROM session_links WHERE pr_number IS NOT NULL")) == {
        PR
    }


RUN_STEPS = ("ingest", "signals", "outcomes", "grade")


def test_run_logs_each_step_as_its_own_command_run(
    home: dict[Harness, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = cli(capsys, "run", "--grade", "--worst", "1")
    assert code == 0, doc
    logged = Counter(command for (command,) in query("SELECT command FROM command_runs"))
    assert logged == Counter([*RUN_STEPS, "run"])
    assert set(doc) == set(RUN_STEPS)


def test_a_failing_grade_step_sets_the_run_exit(
    home: dict[Harness, Path], graders: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    graders.scenario(codex=["fail"], claude=["fail"])
    code, doc = cli(capsys, "run", "--grade", "--worst", "1")
    assert code == 3, doc
    matches("run", doc)
    assert doc["grade"]["chunks_errored"]
    exits = dict(query("SELECT command, exit_code FROM command_runs"))
    assert exits == {"ingest": 0, "signals": 0, "outcomes": 0, "grade": 3, "run": 3}
