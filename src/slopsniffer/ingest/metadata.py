"""Backfill narrow title metadata without replaying transcript bodies."""

import json

import duckdb

from slopsniffer.ingest.reader import BATCH_BYTES, BATCH_ROWS

TITLE_TYPES = ("ai-title", "custom-title", "event_msg:thread_name_updated")


def refresh(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        "CREATE TEMP TABLE session_titles (session_id VARCHAR, title VARCHAR, "
        "priority INTEGER, seq BIGINT, source_file_id BIGINT, byte_offset BIGINT)"
    )
    last = -1
    try:
        while page := con.execute(
            "SELECT rowid, byte_length FROM events WHERE rowid > ? AND record_type IN (?, ?, ?) "
            "ORDER BY rowid LIMIT ?",
            [last, *TITLE_TYPES, BATCH_ROWS],
        ).fetchall():
            size = 0
            bounded: list[int] = []
            for position, length in page:
                if bounded and size + length > BATCH_BYTES:
                    break
                bounded.append(position)
                size += length
            values = []
            for _position, sid, seq, fid, offset, kind, raw in con.sql(
                "SELECT rowid, session_id, seq, source_file_id, byte_offset, record_type, "
                "parsed::VARCHAR FROM events WHERE rowid IN (SELECT unnest($1::BIGINT[]))",
                params=[bounded],
            ).iter_rows(batch_size=1):
                record = json.loads(raw)
                payload = record.get("payload")
                payload = payload if isinstance(payload, dict) else {}
                title = (
                    record.get("customTitle")
                    if kind == "custom-title"
                    else (
                        record.get("aiTitle") if kind == "ai-title" else payload.get("thread_name")
                    )
                )
                if isinstance(title, str) and title.strip():
                    values.append(
                        (sid, title, 2 if kind == "custom-title" else 1, seq, fid, offset)
                    )
            last = bounded[-1]
            if values:
                con.execute(
                    "INSERT INTO session_titles SELECT unnest($1::VARCHAR[]), "
                    "unnest($2::VARCHAR[]), unnest($3::INTEGER[]), unnest($4::BIGINT[]), "
                    "unnest($5::BIGINT[]), unnest($6::BIGINT[])",
                    [list(column) for column in zip(*values, strict=True)],
                )
        con.execute("""
UPDATE sessions SET title = t.title FROM (
    SELECT session_id, title FROM session_titles
    QUALIFY row_number() OVER (PARTITION BY session_id ORDER BY priority DESC,
        seq DESC NULLS LAST, source_file_id DESC, byte_offset DESC) = 1
) t WHERE sessions.session_id = t.session_id
""")
        con.execute(
            "UPDATE events SET kind = 'meta' WHERE kind = 'unknown' "
            "AND record_type IN ('ai-title', 'custom-title', 'pr-link', "
            "'event_msg:thread_name_updated')"
        )
    finally:
        con.execute("DROP TABLE session_titles")
