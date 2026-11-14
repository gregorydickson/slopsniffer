import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from slopsniffer.ingest.engine import KILL_ENV
from tests.ingest import support

GROUP_EXIT_SECONDS = 30


def build_corpus(root: Path) -> None:
    for name in ("a", "b", "c"):
        records = ({"type": "line", "file": name, "n": n} for n in range(20))
        (root / f"{name}.jsonl").write_bytes(support.jsonl(*records))


def small_batch_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text("[ingest]\nbatch_rows = 3\nworkers = 2\n", "utf-8")
    return config


def wait_for_group_exit(pgid: int) -> bool:
    deadline = time.monotonic() + GROUP_EXIT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_sigkill_after_first_batch_then_rerun_matches_an_uninterrupted_run(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    build_corpus(root)
    config = str(small_batch_config(tmp_path))
    crashed, clean = tmp_path / "crashed", tmp_path / "clean"

    killed = support.run_child(root, crashed, SLOPSNIFFER_CONFIG=config, **{KILL_ENV: "1"})
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    assert killed.stdout == ""
    partial = support.event_pairs(crashed)
    all_starts = set().union(*(support.line_starts(p) for p in root.glob("*.jsonl")))
    assert set() < set(partial) < all_starts

    resumed = support.run_child(root, crashed, SLOPSNIFFER_CONFIG=config)
    assert resumed.returncode == 0, resumed.stderr
    assert "files_resumed" in support.nonzero(json.loads(resumed.stdout)["harnesses"]["claude"])

    uninterrupted = support.run_child(root, clean, SLOPSNIFFER_CONFIG=config)
    assert uninterrupted.returncode == 0, uninterrupted.stderr

    final = support.event_pairs(crashed)
    assert len(final) == len(set(final))
    assert final == support.event_pairs(clean)
    assert set(final) == all_starts


def test_killed_ingest_leaves_no_worker_processes(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    build_corpus(root)
    env = {
        **os.environ,
        "SLOPSNIFFER_HOME": str(tmp_path / "data"),
        "SLOPSNIFFER_CONFIG": str(small_batch_config(tmp_path)),
        KILL_ENV: "1",
    }
    child = subprocess.Popen(
        [sys.executable, "-c", support.CHILD, str(root), "ingest", "--harness", "claude"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert child.wait(timeout=120) == -signal.SIGKILL
    assert wait_for_group_exit(child.pid)
