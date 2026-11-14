"""Exit codes and the exceptions that map onto them."""

from enum import IntEnum
from pathlib import Path


class ExitCode(IntEnum):
    OK = 0
    USAGE = 2
    PARTIAL = 3
    LOCKED = 4


class UsageError(Exception):
    """Bad invocation: unknown flag, bad value, refused query."""

    def __init__(self, message: str, code: str = "usage") -> None:
        super().__init__(message)
        self.code = code


class ConfigError(UsageError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="config")


class DatabaseLocked(Exception):
    """Another process holds the single-writer lock."""

    def __init__(self, holder_pid: int | None, lock_path: Path) -> None:
        holder = str(holder_pid) if holder_pid is not None else "unknown"
        super().__init__(f"database locked by pid {holder} (lock file {lock_path})")
        self.holder_pid = holder_pid
        self.lock_path = lock_path


class SchemaVersionMismatch(Exception):
    def __init__(self, found: int, expected: int) -> None:
        super().__init__(f"database schema version {found} does not match code version {expected}")
        self.found = found
        self.expected = expected
