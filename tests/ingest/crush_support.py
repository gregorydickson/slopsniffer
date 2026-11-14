"""Synthetic Crush databases, never copied from real history."""

import json
import sqlite3
from pathlib import Path
from typing import Any

from slopsniffer.config import Config, load_config
from tests.ingest.test_source_contracts import configured

DDL = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, title TEXT,
    prompt_tokens INTEGER, completion_tokens INTEGER, cost REAL, created_at INTEGER,
    updated_at INTEGER);
CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT, role TEXT, parts TEXT,
    model TEXT, provider TEXT, created_at INTEGER, updated_at INTEGER,
    finished_at INTEGER, is_summary_message INTEGER);
CREATE TABLE files (id TEXT, session_id TEXT, path TEXT, content TEXT, version INTEGER,
    created_at INTEGER, updated_at INTEGER);
CREATE TABLE read_files (session_id TEXT, path TEXT, read_at INTEGER);
"""


def part(kind: str, **data: Any) -> dict[str, Any]:
    return {"type": kind, "data": data}


def message(
    db: sqlite3.Connection,
    sid: str,
    mid: str,
    role: str,
    parts: list[dict[str, Any]],
    *,
    when: int = 100,
    summary: bool = False,
) -> None:
    db.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            mid,
            sid,
            role,
            json.dumps(parts),
            "crush-model",
            "provider",
            when,
            when,
            when + 1,
            summary,
        ],
    )


def fixture_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(DDL)
        db.execute("INSERT INTO sessions VALUES ('parent',NULL,'Parent title',123,45,1.25,90,120)")
        db.execute("INSERT INTO sessions VALUES ('child','parent','Child title',7,3,0.3,91,115)")
        message(db, "parent", "u1", "user", [part("text", text="hello")])
        message(
            db,
            "parent",
            "a1",
            "assistant",
            [
                part("reasoning", thinking="private reasoning"),
                part("text", text="running"),
                part(
                    "tool_call", id="call", name="bash", input=json.dumps({"command": "pytest -q"})
                ),
                part("finish", reason="tool_use"),
            ],
            when=101,
        )
        message(
            db,
            "parent",
            "t1",
            "tool",
            [part("tool_result", tool_call_id="call", content="failed output", is_error=True)],
            when=102,
        )
        message(db, "child", "c1", "assistant", [part("text", text="a summary")], summary=True)
        db.execute("INSERT INTO files VALUES ('f','parent','x.py','original bytes',1,102,102)")
        db.execute("INSERT INTO read_files VALUES ('parent','y.py',103)")


def fixture_config(tmp_path: Path) -> tuple[Config, Path]:
    database = tmp_path / "project" / ".crush" / "crush.db"
    fixture_db(database)
    index = tmp_path / "projects.json"
    index.write_text(
        json.dumps(
            {
                "projects": [
                    {"path": str(database.parent.parent), "data_dir": str(database.parent)},
                    {
                        "path": str(tmp_path / "missing"),
                        "data_dir": str(tmp_path / "missing/.crush"),
                    },
                    {"path": str(database.parent.parent), "data_dir": str(database.parent)},
                ]
            }
        )
    )
    return configured(load_config(), crush_projects=index), database
