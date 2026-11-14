import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from slopsniffer.cli import COMMANDS, main
from slopsniffer.config import load_config
from slopsniffer.db import open_read, read_holder_pid, write_lock
from slopsniffer.errors import DatabaseLocked

HOLDER = """
import sys
from slopsniffer.config import load_config
from slopsniffer.db import open_write

with open_write(load_config()):
    print("ready", flush=True)
    sys.stdin.read()
"""

# Holds only the single-writer flock, not the DuckDB file, over an initialized database.
FLOCK_HOLDER = """
import sys
from slopsniffer.config import load_config
from slopsniffer.db import open_write, write_lock

config = load_config()
with open_write(config):
    pass
with write_lock(config.data_dir):
    print("ready", flush=True)
    sys.stdin.read()
"""


def hold(script: str) -> Iterator[subprocess.Popen[str]]:
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None and child.stdin is not None
    assert child.stdout.readline().strip() == "ready"
    try:
        yield child
    finally:
        child.stdin.close()
        child.wait(timeout=30)


@pytest.fixture
def lock_holder() -> Iterator[subprocess.Popen[str]]:
    """A child process holding the write lock (and the DuckDB file) until stdin closes."""
    yield from hold(HOLDER)


@pytest.fixture
def flock_holder() -> Iterator[subprocess.Popen[str]]:
    """A child process holding only the write lock until stdin closes."""
    yield from hold(FLOCK_HOLDER)


def test_second_writer_exits_4_naming_the_holder_pid(
    lock_holder: subprocess.Popen[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["ingest", "--json"]) == 4
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert (doc["error"], doc["holder_pid"], doc["command"]) == (
        "database_locked",
        lock_holder.pid,
        "ingest",
    )
    assert str(lock_holder.pid) in doc["message"]
    assert str(lock_holder.pid) in captured.err


def test_every_writing_subcommand_is_refused_while_locked(
    lock_holder: subprocess.Popen[str],
) -> None:
    required = {"grade": ["--worst", "1"]}
    writers = {name for name, command in COMMANDS.items() if command.writes}
    refused = {name for name in writers if main([name, *required.get(name, [])]) == 4}
    assert refused == writers


@pytest.mark.usefixtures("flock_holder")
def test_read_only_subcommands_do_not_take_the_lock() -> None:
    required = {"query": ["SELECT 1"], "rehydrate": ["0" * 32]}
    readers = {name for name, command in COMMANDS.items() if not command.writes}
    locked_out = {name for name in readers if main([name, *required.get(name, [])]) == 4}
    assert locked_out == set()


def test_read_only_open_while_writer_holds_db_raises_locked(
    lock_holder: subprocess.Popen[str],
) -> None:
    with pytest.raises(DatabaseLocked) as excinfo, open_read(load_config()):
        pass
    assert excinfo.value.holder_pid == lock_holder.pid


def test_lock_is_released_when_the_holder_exits(capsys: pytest.CaptureFixture[str]) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", HOLDER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    assert child.stdout is not None and child.stdin is not None
    assert child.stdout.readline().strip() == "ready"
    child.stdin.close()
    child.wait(timeout=30)
    capsys.readouterr()

    assert main(["ingest", "--json"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"started_at", "finished_at", "harnesses"}


def test_stale_pid_in_lock_file_does_not_block(tmp_path: Path) -> None:
    data_dir = tmp_path / "stale"
    data_dir.mkdir()
    (data_dir / "telemetry.lock").write_text("999999\n")
    with write_lock(data_dir):
        assert read_holder_pid(data_dir) is not None
    assert read_holder_pid(data_dir) is None


def test_lock_contention_within_one_process(tmp_path: Path) -> None:
    with write_lock(tmp_path), pytest.raises(DatabaseLocked), write_lock(tmp_path):
        pass
