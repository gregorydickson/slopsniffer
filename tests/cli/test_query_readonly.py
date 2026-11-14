"""``query`` runs on a connection that cannot change the database or the filesystem."""

import json
from pathlib import Path

import duckdb
import pytest

from slopsniffer.cli import main
from slopsniffer.errors import UsageError
from slopsniffer.query import harden, run
from tests.cli.support import cli, fixture_home, validate
from tests.ingest.support import event_ids, query


@pytest.fixture
def ingested(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> set[str]:
    fixture_home(isolated_home)
    code, doc = cli(capsys, "ingest", "--harness", "claude")
    assert code == 0, doc
    ids = event_ids()
    assert ids
    return ids


def test_select_returns_columns_and_rows(
    ingested: set[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = cli(capsys, "query", "SELECT event_id, kind FROM events ORDER BY event_id")
    assert code == 0, doc
    validate("query", doc)
    assert doc["columns"] == ["event_id", "kind"]
    assert {row[0] for row in doc["rows"]} == ingested


def test_timestamptz_columns_come_back_as_utc_text(
    ingested: set[str], capsys: pytest.CaptureFixture[str]
) -> None:
    expected = {
        (eid, ts)
        for eid, ts in query(
            "SELECT event_id, strftime(timezone('UTC', ts), '%Y-%m-%d %H:%M:%S') FROM events"
            " WHERE ts IS NOT NULL"
        )
    }
    assert expected
    code, doc = cli(capsys, "query", "SELECT event_id, ts, [ts] AS listed FROM events")
    assert code == 0, doc
    validate("query", doc)
    stamped = [(eid, ts, listed) for eid, ts, listed in doc["rows"] if ts is not None]
    assert {(eid, ts[:19]) for eid, ts, _ in stamped} == expected
    assert {ts.endswith("+00") for _, ts, _ in stamped} == {True}
    assert {listed == f"['{ts}']" for _, ts, listed in stamped} == {True}


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM events",
        "SELECT 1; DELETE FROM events",
        "DROP TABLE events",
        "UPDATE events SET kind = 'x'",
    ],
)
def test_a_write_exits_2_and_leaves_the_events(
    sql: str, ingested: set[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = cli(capsys, "query", sql)
    assert code == 2
    validate("error", doc)
    assert (doc["error"], doc["command"]) == ("read_only", "query")
    assert event_ids() == ingested


def test_filesystem_access_is_refused(
    tmp_path: Path, ingested: set[str], capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "exported.csv"
    attached = tmp_path / "other.duckdb"
    for sql in (
        f"COPY events TO '{target}'",
        f"ATTACH '{attached}' AS other",
        "SET enable_external_access = true",
    ):
        code, doc = cli(capsys, "query", sql)
        assert (code, doc["error"]) == (2, "read_only"), sql
    assert {p.name for p in tmp_path.iterdir()} & {target.name, attached.name} == set()


def test_invalid_sql_exits_2(ingested: set[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, doc = cli(capsys, "query", "SELEC 1")
    assert code == 2
    validate("error", doc)
    assert doc["error"] == "sql_error"


def test_a_hardened_connection_is_utc_offline_and_locked(tmp_path: Path) -> None:
    con = duckdb.connect()
    harden(con)
    assert con.sql("SELECT current_setting('TimeZone')").fetchall() == [("UTC",)]
    with pytest.raises(UsageError) as copy_out:
        run(con, f"COPY (SELECT 1) TO '{tmp_path / 'out.csv'}'")
    assert copy_out.value.code == "read_only"
    assert not (tmp_path / "out.csv").exists()
    with pytest.raises(UsageError) as unlock:
        run(con, "SET threads = 1")
    assert unlock.value.code == "read_only"


@pytest.mark.parametrize(
    ("sql", "code", "prefix"),
    [
        ("DELETE FROM t", "read_only", "query refused: "),
        ("SET threads = 1", "read_only", "query refused: "),
        ("COPY t TO 'out.csv'", "read_only", "query refused: "),
        ("SELECT * FROM json_each('not json')", "invalid_input", "query invalid: "),
        ("SELEC 1", "sql_error", "query failed: "),
    ],
)
def test_each_failure_names_its_kind_and_carries_the_engine_message(
    sql: str, code: str, prefix: str, tmp_path: Path
) -> None:
    path = tmp_path / "q.duckdb"
    duckdb.connect(str(path)).execute("CREATE TABLE t (a INTEGER)").close()
    con = duckdb.connect(str(path), read_only=True)
    harden(con)
    with pytest.raises(UsageError) as failure:
        run(con, sql)
    assert failure.value.code == code
    message = str(failure.value)
    assert message.startswith(prefix)
    assert len(message) > len(prefix)


def test_rows_are_strict_json(ingested: set[str], capsys: pytest.CaptureFixture[str]) -> None:
    sql = "SELECT 'nan'::DOUBLE, 'inf'::DOUBLE, '-inf'::DOUBLE, 1.5::DOUBLE, '\\xAA\\x01'::BLOB"
    assert main(["query", sql, "--json"]) == 0
    out = capsys.readouterr().out

    def refuse(constant: str) -> object:
        raise AssertionError(f"non-strict JSON constant {constant}")

    doc = json.loads(out, parse_constant=refuse)
    validate("query", doc)
    assert doc["rows"] == [[None, None, None, 1.5, "aa01"]]


def test_comment_only_sql_returns_the_empty_result_contract() -> None:
    with duckdb.connect() as con:
        harden(con)
        assert run(con, "-- no statement") == {"columns": [], "rows": []}


@pytest.mark.parametrize("mixed", [False, True])
def test_timezone_projection_preserves_unrelated_column_types(mixed: bool) -> None:
    sql = "SELECT TIMESTAMPTZ '2026-11-10 12:34:56+00' AS at"
    if mixed:
        sql += ", 7 AS number, true AS flag"
    with duckdb.connect() as con:
        harden(con)
        expected: dict[str, object] = {
            "columns": ["at", "number", "flag"] if mixed else ["at"],
            "rows": [["2026-11-10 12:34:56+00", 7, True]]
            if mixed
            else [["2026-11-10 12:34:56+00"]],
        }
        assert run(con, sql) == expected
