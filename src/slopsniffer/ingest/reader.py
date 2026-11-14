"""Worker side of ingest: the parser contract and reading one batch of lines.

This module runs inside spawned worker processes, so every request it receives
(including the parser) must pickle by qualified name.
"""

import hashlib
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Protocol

HEAD_BYTES = 64 * 1024
# Per-batch cap on line bytes, so a file of huge lines cannot balloon a batch held in memory.
BATCH_ROWS = 5000
BATCH_BYTES = 64 * 1024 * 1024
MALFORMED = "malformed"


def event_id(source_path: str, byte_offset: int) -> str:
    key = f"{source_path}\x00{byte_offset}".encode("utf-8", "surrogateescape")
    return hashlib.sha256(key).hexdigest()[:32]


def head_sha256(path: Path, size: int) -> str:
    """Hash of the file's first ``min(HEAD_BYTES, size)`` bytes."""
    with path.open("rb") as f:
        return hashlib.sha256(f.read(min(HEAD_BYTES, size))).hexdigest()


@dataclass(frozen=True)
class LineContext:
    source_path: str
    source_file_id: int
    byte_offset: int
    event_id: str


@dataclass(frozen=True)
class Event:
    """Parser-supplied columns of the line's ``events`` row; the engine adds the rest."""

    record_type: str
    kind: str
    session_id: str | None = None
    seq: int | None = None
    ts: str | None = None
    parsed: str | None = None


@dataclass(frozen=True)
class Row:
    """A row for a table other than ``events``, owned by the line's event or source file."""

    table: str
    values: Mapping[str, object]


@dataclass(frozen=True)
class ParsedLine:
    event: Event
    rows: Sequence[Row] = ()


class Parser(Protocol):
    def parse_line(self, line: bytes, ctx: LineContext) -> ParsedLine:
        """Parse one line, terminator included. Raise ``ValueError`` if it cannot be parsed."""
        ...


@dataclass(frozen=True)
class BatchRequest:
    """Read complete lines of ``path`` from ``start`` up to (not past) ``limit``."""

    path: str
    source_file_id: int
    start: int
    limit: int
    max_rows: int
    parser: Parser


@dataclass
class Batch:
    source_file_id: int
    end: int
    eof: bool = False
    failure: str | None = None
    # (event_id, byte_offset, byte_length, event)
    events: list[tuple[str, int, int, Event]] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)

    def add(self, line: bytes, offset: int, req: BatchRequest) -> None:
        eid = event_id(req.path, offset)
        try:
            parsed = req.parser.parse_line(
                line, LineContext(req.path, req.source_file_id, offset, eid)
            )
        except ValueError as exc:
            parsed = ParsedLine(Event(MALFORMED, MALFORMED))
            self.errors.append((offset, f"{type(exc).__name__}: {exc}"))
        self.events.append((eid, offset, len(line), parsed.event))
        self.rows.extend(parsed.rows)


def read_batch(req: BatchRequest) -> Batch:
    """One batch of complete lines. ``eof`` once the limit or an unterminated line is reached;
    the unterminated line itself is never part of a batch."""
    batch = Batch(req.source_file_id, req.start)
    offset, line_bytes = req.start, 0
    try:
        with open(req.path, "rb") as f:
            f.seek(offset)
            while len(batch.events) < req.max_rows and line_bytes < BATCH_BYTES:
                line = f.readline(req.limit - offset) if offset < req.limit else b""
                if not line.endswith(b"\n"):
                    batch.eof = True
                    break
                batch.add(line, offset, req)
                offset += len(line)
                line_bytes += len(line)
    except Exception as exc:  # a file that cannot be read or parsed fails as a whole
        return Batch(req.source_file_id, req.start, failure=f"{type(exc).__name__}: {exc}")
    batch.end = offset
    return batch


def serve(conn: Connection[Batch, BatchRequest | None]) -> None:
    """Worker loop: answer each request with one batch. Exits on ``None`` or once the parent
    is gone (its end of the pipe closes), so a killed parent leaves no orphans."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        while (req := conn.recv()) is not None:
            conn.send(read_batch(req))
    except EOFError, OSError:
        pass
