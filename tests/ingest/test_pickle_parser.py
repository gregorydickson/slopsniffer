import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from slopsniffer.cli import main
from slopsniffer.ingest import engine, pickle
from slopsniffer.ingest.reader import LineContext
from tests.helpers import REPO_ROOT
from tests.ingest import claude_support, support

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pickle"
RUN = "2026-11-09-aaaa1111"
NOT_A_RUN = "not-a-run"
KNOWN, WORKER_KNOWN, UNKNOWN = "s-other", "s-main", "s-ghost"


@pytest.fixture
def sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Path:
    claude_support.fixture_root(tmp_path, monkeypatch)
    claude_support.ingest(capsys)
    root = tmp_path / "pickle"
    shutil.copytree(FIXTURES / "sessions", root)
    shutil.copy(FIXTURES / "run_state.json", root / RUN / pickle.STATE)
    monkeypatch.setitem(engine.REGISTRY, pickle.HARNESS, pickle.source([root]))
    assert main(["ingest", "--harness", pickle.HARNESS, "--json"]) == 0
    counts = json.loads(capsys.readouterr().out)["harnesses"][pickle.HARNESS]
    assert (counts["files_failed"], counts["ingest_errors"]) == ([], 0)
    return root


def test_run_row_carries_the_state_file_fields(sessions: Path) -> None:
    state = json.loads((FIXTURES / "run_state.json").read_text())
    rows = support.query(
        "SELECT pickle_run_id, session_dir, active, step, iteration, max_iterations, "
        "current_ticket, start_time_epoch, working_dir, last_log_write_at IS NOT NULL "
        "FROM pickle_runs"
    )
    fields = ("active", "step", "iteration", "max_iterations", "current_ticket")
    expected = (*(state[f] for f in fields), state["start_time_epoch"], state["working_dir"])
    assert rows == [(RUN, str((sessions / RUN).resolve()), *expected, True)]


def test_iterations_are_keyed_by_the_log_number(sessions: Path) -> None:
    numbers = {
        int(m.group(1))
        for log in (sessions / RUN).iterdir()
        if (m := pickle.ITERATION_LOG.fullmatch(log.name))
    }
    rows = support.query("SELECT pickle_run_id, iteration FROM pickle_iterations")
    assert set(rows) == {(RUN, n) for n in numbers}
    spans = support.query(
        "SELECT epoch(started_at), epoch(ended_at) FROM pickle_iterations WHERE iteration = 1"
    )
    lines = [
        json.loads(line)
        for line in (sessions / RUN / "tmux_iteration_1.log").open()
        if line.startswith("{")
    ]
    instants = [datetime.fromisoformat(r["timestamp"]).timestamp() for r in lines]
    assert spans == [(min(instants), max(instants))]


def test_links_name_only_sessions_that_exist(sessions: Path) -> None:
    logs = "".join(p.read_text() for p in (sessions / RUN).rglob("*.log"))
    assert UNKNOWN in logs
    rows = support.query("SELECT pickle_run_id, iteration, session_id FROM pickle_session_links")
    assert set(rows) == {(RUN, 1, KNOWN), (RUN, None, WORKER_KNOWN)}
    sources = support.query(
        "SELECT e.byte_offset FROM pickle_session_links l JOIN events e "
        "ON e.event_id = l.source_event_id WHERE l.session_id = ?",
        [KNOWN],
    )
    assert sources == [(0,)]


def test_every_log_line_is_an_event_and_text_is_not_malformed(sessions: Path) -> None:
    logs = sorted((sessions / RUN).rglob("*.log"))
    expected = {
        (str(log.resolve()), pickle.kind(str(log)), line.startswith("{"))
        for log in logs
        for line in log.read_text().splitlines()
    }
    rows = support.query(
        "SELECT f.path, e.kind, e.parsed IS NOT NULL FROM events e JOIN source_files f "
        "USING (source_file_id) WHERE f.harness = 'pickle'"
    )
    assert set(rows) == expected
    assert sum(len(log.read_text().splitlines()) for log in logs) == len(rows)
    text_types = support.query(
        "SELECT DISTINCT e.record_type FROM events e JOIN source_files f USING (source_file_id) "
        "WHERE f.harness = 'pickle' AND e.parsed IS NULL"
    )
    assert text_types == [(pickle.TEXT,)]
    paths = {p for (p,) in support.query("SELECT path FROM source_files WHERE harness = 'pickle'")}
    assert not any(NOT_A_RUN in p for p in paths)


def test_a_rerun_rebuilds_rather_than_duplicates(
    sessions: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = support.query("SELECT * FROM pickle_session_links ORDER BY ALL")
    assert main(["ingest", "--harness", pickle.HARNESS, "--json"]) == 0
    capsys.readouterr()
    assert support.query("SELECT * FROM pickle_session_links ORDER BY ALL") == before


def test_a_line_that_is_not_utf8_is_text() -> None:
    ctx = LineContext("/run/hooks.log", 1, 0, "e1")
    parsed = pickle.PickleParser().parse_line(b"tmux \xff\xfe output\n", ctx)
    assert (parsed.event.record_type, parsed.event.kind, parsed.event.parsed) == (
        pickle.TEXT,
        "hooks",
        None,
    )
