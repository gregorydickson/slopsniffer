import json
from pathlib import Path

import duckdb
import pytest

from slopsniffer.config import Config, load_config
from slopsniffer.db import SCHEMA_VERSION, open_read, open_write
from slopsniffer.errors import SchemaVersionMismatch

PRD_CORE_TABLES = {
    "source_files",
    "sessions",
    "events",
    "messages",
    "tool_calls",
    "skill_invocations",
    "blobs",
    "context_blocks",
    "usage",
    "session_cost",
    "pickle_runs",
    "pickle_iterations",
    "skills",
    "ingest_errors",
    "signals",
    "session_links",
    "pr_outcomes",
    "grades",
    "grade_calls",
    "proposals",
}
SUPPORTING_TABLES = {
    "schema_version",
    "pickle_session_links",
    "command_runs",
    "session_relations",
    "ingest_parser_versions",
}

PRD_COLUMNS = {
    "sessions": {
        "title",
        "session_id",
        "harness",
        "parent_session_id",
        "project",
        "cwd",
        "git_branch",
        "model",
        "started_at",
        "ended_at",
        "source_file_id",
    },
    "events": {
        "event_id",
        "session_id",
        "seq",
        "record_type",
        "kind",
        "ts",
        "source_file_id",
        "byte_offset",
        "byte_length",
        "parsed",
    },
    "messages": {"event_id", "role", "text"},
    "tool_calls": {
        "call_id",
        "session_id",
        "use_event_id",
        "result_event_id",
        "tool_name",
        "input",
        "result_text",
        "is_error",
        "duration_ms",
    },
    "blobs": {"content_hash", "content"},
    "usage": {"event_id", "input_tokens", "output_tokens", "cache_read", "cache_write", "model"},
    "signals": {
        "signal_id",
        "kind",
        "session_id",
        "severity",
        "evidence_event_ids",
        "details",
        "computed_at",
    },
    "session_links": {
        "session_id",
        "repo",
        "branch",
        "pr_number",
        "commit_sha",
        "ticket_id",
        "source_event_id",
        "method",
    },
}


@pytest.fixture
def config() -> Config:
    return load_config()


def tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE NOT temporary"
        ).fetchall()
    }


def columns(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    rows = con.execute(
        "SELECT column_name FROM duckdb_columns() WHERE table_name = ?", [table]
    ).fetchall()
    return {row[0] for row in rows}


def test_every_core_table_exists(config: Config) -> None:
    with open_write(config) as con:
        assert tables(con) == PRD_CORE_TABLES | SUPPORTING_TABLES


@pytest.mark.parametrize("table", sorted(PRD_COLUMNS))
def test_prd_specified_columns(config: Config, table: str) -> None:
    with open_write(config) as con:
        assert columns(con, table) == PRD_COLUMNS[table]


def test_schema_version_recorded_once_across_reopens(config: Config) -> None:
    for _ in range(2):
        with open_write(config):
            pass
    with open_write(config) as con:
        assert con.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]


def test_newer_schema_version_is_refused_and_left_intact(config: Config) -> None:
    with open_write(config) as con:
        con.execute("UPDATE schema_version SET version = ?", [SCHEMA_VERSION + 1])
    with pytest.raises(SchemaVersionMismatch), open_write(config):
        pass
    with open_read(config) as con:
        assert con.execute("SELECT version FROM schema_version").fetchall() == [
            (SCHEMA_VERSION + 1,)
        ]


def test_db_lives_in_data_dir(config: Config, tmp_path: Path) -> None:
    with open_write(config):
        pass
    assert config.db_path.is_file()
    assert config.db_path.is_relative_to(tmp_path)


def test_memory_limit_from_config_is_applied(config: Config) -> None:
    probe = duckdb.connect()
    probe.execute("SET memory_limit = ?", [config.ingest.memory_limit])
    expected = probe.execute("SELECT current_setting('memory_limit')").fetchall()
    with open_write(config) as con:
        assert con.execute("SELECT current_setting('memory_limit')").fetchall() == expected


def test_read_only_open_initializes_absent_db_and_refuses_writes(config: Config) -> None:
    assert not config.db_path.exists()
    with open_read(config) as con:
        assert tables(con) == PRD_CORE_TABLES | SUPPORTING_TABLES
        with pytest.raises(duckdb.InvalidInputException):
            con.execute("DELETE FROM events")


def test_version_1_database_is_upgraded_to_generic_ticket_links(config: Config) -> None:
    with open_write(config) as con:
        con.execute("DROP TABLE session_links")
        con.execute(
            "CREATE TABLE session_links (session_id VARCHAR NOT NULL, repo VARCHAR, "
            "branch VARCHAR, pr_number INTEGER, commit_sha VARCHAR, loa_id VARCHAR, "
            "source_event_id VARCHAR, method VARCHAR NOT NULL)"
        )
        con.execute(
            "INSERT INTO session_links (session_id, loa_id, method) "
            "VALUES ('s', 'X-1', 'loa_branch')"
        )
        con.execute("INSERT INTO sessions (session_id, harness) VALUES ('kept', 'claude')")
        con.execute("ALTER TABLE sessions DROP COLUMN title")
        con.execute("UPDATE schema_version SET version = 1")
    with open_write(config) as con:
        assert columns(con, "session_links") == PRD_COLUMNS["session_links"]
        assert con.execute("SELECT count(*) FROM session_links").fetchone() == (0,)
        assert con.execute("SELECT session_id FROM sessions").fetchall() == [("kept",)]
        assert con.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]


def assert_no_history_indexes(con: duckdb.DuckDBPyConnection) -> None:
    # Exact membership also catches an index on a newly introduced history table.
    assert con.execute("SELECT table_name, index_name FROM duckdb_indexes()").fetchall() == []
    assert con.execute(
        "SELECT table_name, constraint_type, constraint_column_names FROM duckdb_constraints() "
        "WHERE constraint_type IN ('PRIMARY KEY', 'UNIQUE')"
    ).fetchall() == [("skills", "PRIMARY KEY", ["path"])]


def test_history_has_no_art_indexes(config: Config) -> None:
    with open_write(config) as con:
        assert_no_history_indexes(con)


def test_v2_upgrade_preserves_history_and_column_contracts(config: Config) -> None:
    from tests.helpers import REPO_ROOT

    config.data_dir.mkdir(parents=True)
    with duckdb.connect(str(config.db_path)) as con:
        con.execute((REPO_ROOT / "tests/fixtures/schema_v2.sql").read_text("utf-8"))
        con.execute("INSERT INTO schema_version (version) VALUES (2)")
        # Populate every keyed table with its required columns, using the catalog to
        # cover keys beyond the ingest lane without duplicating the schema inventory.
        for (table,) in con.execute(
            "SELECT DISTINCT table_name FROM duckdb_constraints() "
            "WHERE constraint_type = 'PRIMARY KEY' ORDER BY table_name"
        ).fetchall():
            cols = con.execute(
                "SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = ? "
                "AND NOT is_nullable AND column_default IS NULL ORDER BY column_index",
                [table],
            ).fetchall()
            literals = {
                "status": "'graded'" if table == "grades" else "'proposed'",
                "method": "'unpriced'",
                "via": "'skill_tool'",
                "scope": "'global'",
                "kind": "'other'",
            }
            values = [
                literals.get(
                    name,
                    "[]"
                    if kind.endswith("[]")
                    else "now()"
                    if kind == "TIMESTAMP WITH TIME ZONE"
                    else "1"
                    if kind in ("BIGINT", "INTEGER", "DOUBLE")
                    else "'kept'",
                )
                for name, kind in cols
            ]
            con.execute(
                f"INSERT INTO {table} ({', '.join(n for n, _ in cols)}) "
                f"VALUES ({', '.join(values)})"
            )
        before = {
            table: con.execute(f"SELECT to_json(t)::VARCHAR FROM {table} t").fetchall()
            for table in (PRD_CORE_TABLES | SUPPORTING_TABLES)
            - {"schema_version", "session_relations", "ingest_parser_versions"}
        }
    before["sessions"] = [
        (json.dumps(json.loads(raw) | {"title": None}, separators=(",", ":")),)
        for (raw,) in before["sessions"]
    ]
    with open_write(config) as con:
        assert_no_history_indexes(con)
        assert {
            table: con.execute(f"SELECT to_json(t)::VARCHAR FROM {table} t").fetchall()
            for table in before
        } == before
        assert con.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO messages (event_id, role) VALUES ('invalid', NULL)")
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO context_blocks VALUES ('invalid', 0, 'bad', NULL, 'hash')")
        con.execute("INSERT INTO source_files (path, harness) VALUES ('next', 'claude')")
        assert con.execute("SELECT path FROM source_files ORDER BY source_file_id").fetchall() == [
            ("kept",),
            ("next",),
        ]


@pytest.mark.parametrize("with_title", [False, True])
def test_v3_upgrade_preserves_titles_rows_and_matches_fresh_schema(
    config: Config, with_title: bool
) -> None:
    from slopsniffer.db import apply_schema

    with open_write(config) as con:
        con.execute(
            "INSERT INTO sessions (session_id, harness, title) "
            "VALUES ('kept', 'crush', 'Kept title')"
        )
        con.execute("INSERT INTO session_cost VALUES ('kept', 1.25, 'cost_state')")
        if not with_title:
            con.execute("ALTER TABLE sessions DROP COLUMN title")
        con.execute("ALTER TABLE session_cost RENAME TO previous_cost")
        con.execute(
            "CREATE TABLE session_cost (session_id VARCHAR NOT NULL, cost_usd DOUBLE, "
            "method VARCHAR NOT NULL CHECK (method IN ('cost_state','price_table','unpriced')))"
        )
        con.execute("INSERT INTO session_cost SELECT * FROM previous_cost")
        con.execute("DROP TABLE previous_cost")
        con.execute("UPDATE schema_version SET version=3")
    with open_write(config) as con, duckdb.connect() as fresh:
        apply_schema(fresh)
        catalog = (
            "SELECT table_name, column_name, data_type, is_nullable, column_default "
            "FROM duckdb_columns() WHERE NOT internal ORDER BY table_name, column_name"
        )
        assert con.execute(catalog).fetchall() == fresh.execute(catalog).fetchall()
        assert con.execute("SELECT session_id, title FROM sessions").fetchall() == [
            ("kept", "Kept title" if with_title else None)
        ]
        assert con.execute("SELECT * FROM session_cost").fetchall() == [
            ("kept", 1.25, "cost_state")
        ]
        con.execute("INSERT INTO session_cost VALUES ('partial', 0.5, 'partial_price_table')")
        assert con.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
