"""DuckDB access under a single-writer file lock.

Writers hold an exclusive ``flock`` on ``<data_dir>/telemetry.lock`` (which
records the holder's pid) for the lifetime of their connection, so a second
writer fails fast with ``DatabaseLocked`` instead of blocking or racing DuckDB.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

import duckdb

from slopsniffer.config import Config
from slopsniffer.errors import DatabaseLocked, SchemaVersionMismatch

SCHEMA_VERSION = 4

# Rebuild history tables transactionally, preserving rows and non-index constraints.
_UPGRADES: dict[int, str] = {
    1: "DROP TABLE session_links",
    2: resources.files("slopsniffer").joinpath("upgrade_v3.sql").read_text("utf-8"),
    3: resources.files("slopsniffer").joinpath("upgrade_v4.sql").read_text("utf-8"),
}
LOCK_FILENAME = "telemetry.lock"


def lock_path(data_dir: Path) -> Path:
    return data_dir / LOCK_FILENAME


def read_holder_pid(data_dir: Path) -> int | None:
    try:
        text = lock_path(data_dir).read_text("ascii").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


@contextmanager
def write_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = lock_path(data_dir)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DatabaseLocked(read_holder_pid(data_dir), path) from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode("ascii"), 0)
        try:
            yield
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _writer_is_live(data_dir: Path) -> bool:
    """True when some process currently holds the write lock."""
    path = lock_path(data_dir)
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def apply_schema(con: duckdb.DuckDBPyConnection) -> None:
    ddl = resources.files("slopsniffer").joinpath("schema.sql").read_text("utf-8")
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(ddl)
        row = con.execute("SELECT max(version) FROM schema_version").fetchone()
        found = row[0] if row else None
        if found is None:
            con.execute("INSERT INTO schema_version (version) VALUES (?)", [SCHEMA_VERSION])
        elif found != SCHEMA_VERSION:
            _upgrade(con, found, ddl)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _upgrade(con: duckdb.DuckDBPyConnection, found: int, ddl: str) -> None:
    steps = range(found, SCHEMA_VERSION)
    if not steps or any(version not in _UPGRADES for version in steps):
        raise SchemaVersionMismatch(found, SCHEMA_VERSION)
    for version in steps:
        con.execute(_UPGRADES[version])
    con.execute(ddl)
    con.execute("UPDATE schema_version SET version = ?", [SCHEMA_VERSION])


def _configure(con: duckdb.DuckDBPyConnection, config: Config) -> None:
    con.execute("SET memory_limit = ?", [config.ingest.memory_limit])
    con.execute("SET threads = ?", [config.ingest.threads])
    con.execute("SET temp_directory = ?", [str(config.data_dir / "tmp")])
    con.execute("SET preserve_insertion_order = false")


@contextmanager
def open_write(config: Config) -> Iterator[duckdb.DuckDBPyConnection]:
    with write_lock(config.data_dir):
        con = duckdb.connect(str(config.db_path))
        try:
            _configure(con, config)
            apply_schema(con)
            yield con
        finally:
            con.close()


@contextmanager
def open_read(config: Config) -> Iterator[duckdb.DuckDBPyConnection]:
    """Read-only connection; initializes an absent database first."""
    if not config.db_path.exists():
        with open_write(config):
            pass
    try:
        con = duckdb.connect(str(config.db_path), read_only=True)
    except duckdb.IOException:
        if _writer_is_live(config.data_dir):
            raise DatabaseLocked(
                read_holder_pid(config.data_dir), lock_path(config.data_dir)
            ) from None
        raise
    try:
        _configure(con, config)
        yield con
    finally:
        con.close()
