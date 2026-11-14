"""Test parsers. Module-level so spawned ingest workers can unpickle them."""

from dataclasses import dataclass

from slopsniffer.ingest.passthrough import PassthroughParser
from slopsniffer.ingest.reader import LineContext, ParsedLine, Row


@dataclass(frozen=True)
class MessageEchoParser:
    """Passthrough that also writes a ``messages`` row per line, owned by the line's event."""

    def parse_line(self, line: bytes, ctx: LineContext) -> ParsedLine:
        parsed = PassthroughParser().parse_line(line, ctx)
        message = Row("messages", {"event_id": ctx.event_id, "role": "user", "text": None})
        return ParsedLine(parsed.event, (message,))


@dataclass(frozen=True)
class ExplodingParser:
    """Raises a non-``ValueError`` (a parser bug, not a bad line) on lines containing ``boom``."""

    def parse_line(self, line: bytes, ctx: LineContext) -> ParsedLine:
        if b"boom" in line:
            raise RuntimeError("parser bug")
        return PassthroughParser().parse_line(line, ctx)


@dataclass(frozen=True)
class RejectedRowParser:
    """Passthrough that, on lines containing ``reject``, emits a row the database refuses."""

    def parse_line(self, line: bytes, ctx: LineContext) -> ParsedLine:
        parsed = PassthroughParser().parse_line(line, ctx)
        if b"reject" not in line:
            return parsed
        bad = Row("messages", {"event_id": ctx.event_id, "no_such_column": 1})
        return ParsedLine(parsed.event, (bad,))


@dataclass(frozen=True)
class ProcessContextParser:
    """Expose the actual child's isolation context through an ordinary parsed row."""

    def parse_line(self, line: bytes, ctx: LineContext) -> ParsedLine:
        import json
        import multiprocessing
        import os

        parsed = PassthroughParser().parse_line(line, ctx)
        process = multiprocessing.current_process()
        row = Row(
            "messages",
            {
                "event_id": ctx.event_id,
                "role": "system",
                "text": json.dumps(
                    {
                        "pid": os.getpid(),
                        "parent": os.getppid(),
                        "daemon": process.daemon,
                        "method": multiprocessing.get_start_method(),
                    }
                ),
            },
        )
        return ParsedLine(parsed.event, (row,))


@dataclass(frozen=True)
class ExitParser:
    """A synthetic hard worker failure, distinct from a handled parser exception."""

    def parse_line(self, line: bytes, ctx: LineContext) -> ParsedLine:
        import os

        if b"exit-worker" in line:
            os._exit(7)
        return ExplodingParser().parse_line(line, ctx)


@dataclass(frozen=True)
class DelayedServe:
    """Keep one real worker busy while another reports a batch."""

    marker: str
    pause: float

    def __call__(self, connection: object) -> None:
        import time
        from multiprocessing.connection import Connection
        from pathlib import Path
        from typing import cast

        from slopsniffer.ingest.reader import Batch, BatchRequest, read_batch, serve

        link = cast("Connection[Batch, BatchRequest | None]", connection)
        request = link.recv()
        assert request is not None
        marker = Path(self.marker)
        if Path(request.path).name == "slow":
            marker.touch()
            time.sleep(self.pause)
        else:
            until = time.monotonic() + self.pause
            while not marker.exists():
                if time.monotonic() > until:
                    raise RuntimeError("other worker did not begin reading")
                time.sleep(0.01)
        link.send(read_batch(request))
        serve(link)
