"""Bound finite ingest work, including work performed during fixture setup."""

import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def deadline(seconds: float, operation: str) -> Iterator[None]:
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    if 0 < previous_timer[0] <= seconds:
        yield
        return

    def expired(signum: int, frame: object) -> None:
        raise RuntimeError(f"finite {operation} did not terminate within {seconds:g} seconds")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    started = time.monotonic()
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            remaining = max(0.0, previous_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])
