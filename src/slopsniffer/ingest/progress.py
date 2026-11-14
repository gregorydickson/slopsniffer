"""Rate-limited stderr progress; JSON stdout remains a single document."""

import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from slopsniffer.contracts import HarnessIngest

CURRENT: ContextVar[Progress | None] = ContextVar("ingest_progress", default=None)


@dataclass
class Progress:
    harness: str
    counts: HarnessIngest
    total: int = 0
    done: int = 0
    phase: str = "discover"
    started: float = field(default_factory=time.monotonic)
    last: float = float("-inf")
    sessions_total: int | None = None
    sessions_done: int = 0
    phase_started: float = 0

    def emit(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last < (2 if sys.stderr.isatty() else 30):
            return
        self.last = now
        elapsed = now - self.started
        work_done = self.done if self.sessions_total is None else self.sessions_done
        work_total = self.total if self.sessions_total is None else self.sessions_total
        phase_elapsed = elapsed if self.sessions_total is None else now - self.phase_started
        estimate = self.sessions_total is not None or self.phase in ("read", "complete")
        eta = (
            f"{phase_elapsed * (work_total - work_done) / work_done:.0f}s"
            if work_done and estimate
            else "?"
        )
        sessions = (
            f", sessions {self.sessions_done}/{self.sessions_total}"
            if self.sessions_total is not None
            else ""
        )
        print(
            f"ingest {self.harness}: files {self.done}/{self.total}, "
            f"events {self.counts['events_written']}, elapsed {elapsed:.0f}s, "
            f"ETA {eta}, {self.phase}{sessions}",
            file=sys.stderr,
            flush=True,
        )


def tick(phase: str | None = None) -> None:
    progress = CURRENT.get()
    if progress is not None:
        if phase is not None:
            progress.phase = phase
        progress.emit()


def finalizing(done: int, total: int) -> None:
    progress = CURRENT.get()
    if progress is not None:
        if progress.sessions_total is None:
            progress.phase_started = time.monotonic()
        progress.sessions_done = done
        progress.sessions_total = total
        progress.emit()
