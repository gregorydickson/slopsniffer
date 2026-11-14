"""Read-only SQLite snapshots exported as durable, row-lossless JSONL sources.

Never copy a live main file without its WAL. SQLite's online backup API produces a
consistent private snapshot. Only changed sessions are exported, one row at a time.
The exports are the source bytes served by rehydrate, not SQLite physical pages.
"""

import hashlib
import json
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from typing import Any

import duckdb

from slopsniffer.config import Config
from slopsniffer.contracts import Harness
from slopsniffer.ingest import common, engine
from slopsniffer.ingest.progress import tick
from slopsniffer.ingest.reader import Event, LineContext, ParsedLine, Row

HARNESS: Harness = "crush"


@contextmanager
def snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    """Bounded-memory online backup; busy/continuously changing sources fail visibly.

    mode=ro prohibits writes to the live database. SQLite owns WAL coordination;
    no immutable URI, checkpoint, journal-mode change, or raw live-file copy.
    """
    deadline = time.monotonic() + 60

    def progress(status: int, remaining: int, total: int) -> None:
        tick(f"snapshot {path}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"Crush snapshot timed out: {path}")

    with (
        tempfile.TemporaryDirectory(prefix="slopsniffer-crush-") as directory,
        closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as live,
    ):
        live.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(str(Path(directory) / "snapshot.db"))) as private:
            live.backup(private, pages=256, progress=progress, sleep=0.01)
            private.row_factory = sqlite3.Row
            yield private


def _stamp(session: sqlite3.Row, db: sqlite3.Connection) -> list[object]:
    # updated_at is second-resolution, and an in-flight message can change before
    # the session total does. Include narrow row versions to catch these writes.
    values: list[object] = [1, dict(session)]
    for table, column in (
        ("messages", "updated_at"),
        ("files", "updated_at"),
        ("read_files", "read_at"),
    ):
        values.append(
            tuple(
                db.execute(
                    f"SELECT count(*), max({column}) FROM {table} WHERE session_id=?",
                    [session["id"]],
                ).fetchone()
            )
        )
    return values


def _export(
    db: sqlite3.Connection, path: Path, project: str, database: str, namespace: str
) -> Iterator[Path]:
    path.mkdir(parents=True, exist_ok=True)
    for table in ("messages", "files", "read_files"):
        orphan = db.execute(
            f"SELECT t.session_id FROM {table} t WHERE NOT EXISTS "
            "(SELECT 1 FROM sessions s WHERE s.id=t.session_id) LIMIT 1"
        ).fetchone()
        if orphan is not None:
            raise ValueError(f"orphaned {table} row for session {orphan[0]!r}")
    for session in db.execute("SELECT * FROM sessions ORDER BY id"):
        tick("export SQLite rows")
        sid = session["id"]
        target = path / (hashlib.sha256(sid.encode()).hexdigest() + ".jsonl")
        stamp = hashlib.sha256(
            json.dumps(_stamp(session, db), ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        try:
            with target.open(encoding="utf-8") as old:
                previous = json.loads(old.readline()).get("revision")
        except OSError, ValueError:
            previous = None
        if previous != stamp:
            header = {
                "revision": stamp,
                "type": "session",
                "session_id": namespace + sid,
                "project": project,
                "database": database,
                "row": dict(session),
            }
            temporary = target.with_suffix(".tmp")
            try:
                with temporary.open("w", encoding="utf-8") as out:
                    out.write(json.dumps(header, ensure_ascii=False) + "\n")
                    for table in ("messages", "files", "read_files"):
                        order = "created_at, id" if table == "messages" else "rowid"
                        for row in db.execute(
                            f"SELECT * FROM {table} WHERE session_id=? ORDER BY {order}", [sid]
                        ):
                            out.write(
                                json.dumps(
                                    {
                                        "type": table,
                                        "session_id": namespace + sid,
                                        "isCompactSummary": bool(row["is_summary_message"])
                                        if table == "messages"
                                        else False,
                                        "row": dict(row),
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        yield target


def discover(config: Config) -> Iterator[Path]:
    index = config.ingest.sources.crush_projects.expanduser()
    if not index.is_file():
        return
    failed_source = index
    try:
        projects = json.loads(index.read_text("utf-8"))["projects"]
        seen: set[Path] = set()
        home = {"path": str(Path.home()), "data_dir": str(Path.home() / ".crush")}
        for entry in chain(projects, [home]):
            project = str(Path(entry["path"]).expanduser())
            database = (
                Path(entry.get("data_dir", str(Path(project) / ".crush"))).expanduser() / "crush.db"
            ).resolve()
            if database in seen or not database.is_file():
                continue
            seen.add(database)
            failed_source = database
            digest = hashlib.sha256(str(database).encode()).hexdigest()[:16]
            directory = config.data_dir / "sources" / "crush" / digest
            with snapshot(database) as db:
                yield from _export(db, directory, project, str(database), f"crush:{digest}:")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        raise engine.SourceError(f"Crush source {failed_source}: {exc}") from exc


def _instant(value: object) -> str | None:
    if isinstance(value, int):
        return datetime.fromtimestamp(value, UTC).isoformat()
    return None


@dataclass(frozen=True)
class CrushParser:
    def parse_line(self, line: bytes, ctx: LineContext) -> ParsedLine:
        record = json.loads(line)
        row = record["row"]
        kind = record["type"]
        parts = json.loads(row.get("parts", "[]"))
        summary = bool(row.get("is_summary_message"))
        event = Event(
            kind,
            "compaction" if summary else "message" if kind == "messages" else "meta",
            session_id=record["session_id"],
            ts=_instant(row.get("created_at", row.get("read_at"))),
            parsed=line.decode().rstrip("\n"),
        )
        rows = []
        if kind == "messages":
            text = "\n".join(p["data"]["text"] for p in parts if p.get("type") == "text")
            rows.append(
                Row(
                    "messages",
                    {"event_id": ctx.event_id, "role": row["role"], "text": text or None},
                )
            )
        return ParsedLine(event, rows)


def _fields(record: dict[str, Any], kind: str, location: dict[str, Any]) -> Iterator[Row]:
    row = record["row"]
    eid = location["event_id"]
    if kind == "session":
        namespace = record["session_id"][: -len(row["id"])]
        yield Row(
            "crush_meta",
            {
                "session_id": record["session_id"],
                "parent": namespace + row["parent_session_id"]
                if row.get("parent_session_id")
                else None,
                "title": row["title"],
                "project": record["project"],
                "started": _instant(row["created_at"]),
                "ended": _instant(row["updated_at"]),
                "cost": row["cost"],
                "source_file_id": location["source_file_id"],
                "event_id": eid,
                "input_tokens": row.get("prompt_tokens"),
                "output_tokens": row.get("completion_tokens"),
            },
        )
    if kind != "messages":
        return
    if row.get("model"):
        yield Row("crush_models", {"model": row["model"], "seq": location["seq"]})
    loc = {key: location[key] for key in ("event_id", "session_id", "seq")}
    for part in json.loads(row["parts"]):
        data = part.get("data", {})
        if part.get("type") == "tool_call":
            raw = data.get("input")
            if isinstance(raw, str):
                try:
                    json.loads(raw)
                except ValueError:
                    raw = json.dumps(raw)
            else:
                raw = json.dumps(raw)
            yield Row(
                "lane_uses", {**loc, "call_id": data["id"], "tool_name": data["name"], "input": raw}
            )
        elif part.get("type") == "tool_result":
            yield Row(
                "lane_results",
                {
                    **loc,
                    "call_id": data["tool_call_id"],
                    "result_text": data.get("content"),
                    "is_error": data.get("is_error"),
                },
            )


def _derive(con: duckdb.DuckDBPyConnection, config: Config) -> None:
    with common.tool_tables(con, config) as bodies:
        con.execute(
            "CREATE TEMP TABLE crush_meta (session_id VARCHAR, parent VARCHAR, title VARCHAR, "
            "project VARCHAR, started TIMESTAMPTZ, ended TIMESTAMPTZ, cost DOUBLE, "
            "source_file_id BIGINT, event_id VARCHAR, input_tokens BIGINT, output_tokens BIGINT); "
            "CREATE TEMP TABLE crush_models (model VARCHAR, seq BIGINT)"
        )
        common.json_batches(con, config, _fields, bodies)
        common.insert_tools(con, config, bodies, error_default=False)
        con.execute(
            "INSERT INTO sessions (session_id,harness,parent_session_id,title,project,cwd,model,"
            "started_at,ended_at,source_file_id) SELECT session_id,'crush',parent,title,project,"
            "project,(SELECT arg_min(model,seq) FROM crush_models),started,ended,source_file_id "
            "FROM crush_meta"
        )
        # Totals are published in the session's final transaction, after body
        # commits. There is no invented per-assistant token allocation.
        con.execute(
            "INSERT INTO usage (event_id,input_tokens,output_tokens,model) "
            "SELECT event_id,input_tokens,output_tokens, "
            "(SELECT CASE WHEN count(DISTINCT model)=1 THEN max(model) END FROM crush_models) "
            "FROM crush_meta"
        )
        con.execute("INSERT INTO session_cost SELECT session_id,cost,'cost_state' FROM crush_meta")
        con.execute("DROP TABLE crush_meta; DROP TABLE crush_models")


def finalize(con: duckdb.DuckDBPyConnection, config: Config) -> None:
    # Remove prior totals for pending sessions in one narrow history pass, not
    # one global usage scan per session. The replacement totals publish last.
    con.execute(common._QUEUE, [HARNESS])
    con.execute(
        "DELETE FROM usage WHERE event_id IN (SELECT e.event_id FROM events e "
        "JOIN lane_queue q USING(session_id) JOIN source_files f USING(source_file_id) "
        "WHERE f.harness='crush' AND e.record_type='session')"
    )
    con.execute("DROP TABLE lane_queue")
    common.pending(con, HARNESS, lambda: _derive(con, config))


def source() -> engine.HarnessSource:
    return engine.HarnessSource(CrushParser(), discover, finalize)
