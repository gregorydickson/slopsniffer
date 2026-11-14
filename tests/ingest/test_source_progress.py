import sys
import time

import pytest

from slopsniffer.ingest import engine, progress


def test_progress_is_stderr_only_and_rate_limited_for_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    p = progress.Progress(
        "crush", engine._empty_counts(), total=10, done=2, phase="read", started=0
    )
    p.emit()
    first = capsys.readouterr()
    assert first.out == ""
    assert "ingest crush: files 2/10, events 0, elapsed 10s, ETA 40s" in first.err
    clock[0] = 39.9
    p.emit()
    assert capsys.readouterr().err == ""
    clock[0] = 40
    p.counts["events_written"] = 9
    p.done = 5
    p.emit()
    assert "files 5/10, events 9, elapsed 40s, ETA 40s" in capsys.readouterr().err
    p.done = p.total
    p.phase = "stage recovery batch 1"
    p.emit(force=True)
    assert "ETA ?, stage recovery batch 1" in capsys.readouterr().err
    p.phase = "complete"
    p.emit(force=True)
    assert "ETA 0s, complete\n" in capsys.readouterr().err


def test_tty_updates_and_context_tick(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    p = progress.Progress("codex", engine._empty_counts(), started=0)
    token = progress.CURRENT.set(p)
    try:
        progress.tick("read")
        assert "ETA ?, read" in capsys.readouterr().err
        clock[0] = 1
        progress.tick()
        assert capsys.readouterr().err == ""
        clock[0] = 2
        progress.tick("finalize")
        assert "finalize" in capsys.readouterr().err
        clock[0] = 4
        progress.finalizing(0, 2)
        assert "ETA ?, finalize, sessions 0/2" in capsys.readouterr().err
        clock[0] = 6
        progress.finalizing(1, 2)
        assert "ETA 2s, finalize, sessions 1/2" in capsys.readouterr().err
    finally:
        progress.CURRENT.reset(token)
    progress.tick()
    assert capsys.readouterr().err == ""
