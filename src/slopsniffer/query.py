"""Ad hoc SQL over a connection that cannot change the database or touch the filesystem.

The database is opened read-only, external access is switched off and the configuration is
locked before the caller's SQL runs, so the engine itself refuses writes, ``COPY ... TO``,
``ATTACH``, file readers and attempts to re-enable any of them.
"""

import math

import duckdb

from slopsniffer.contracts import QueryResult
from slopsniffer.errors import UsageError

# Columns of these types cannot be fetched without pytz; they are returned as text.
_TEXT_ONLY_TYPE = "TIME ZONE"
# The engine's refusals of a write on a read-only database and of a locked setting; every
# other InvalidInputException is a fault in the caller's input, not a refusal.
_REFUSALS = ("Cannot execute statement of type", "Cannot change configuration option")


def harden(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("SET TimeZone = 'UTC'")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")


def run(con: duckdb.DuckDBPyConnection, sql: str) -> QueryResult:
    """Columns and rows of ``sql``'s last statement; a refused or invalid statement is a
    :class:`UsageError` (code ``read_only``, ``invalid_input`` or ``sql_error``)."""
    # This explicit SQL escape hatch is caller-controlled, not generated analysis:
    # DuckDB enforces its configured memory pool; result size follows the SQL.
    try:
        relation = con.sql(sql)
        if relation is None:
            return {"columns": [], "rows": []}
        columns = list(relation.columns)
        types = [str(t) for t in relation.types]
        if any(_TEXT_ONLY_TYPE in t for t in types):
            relation = relation.project(
                ", ".join(
                    f"CAST(#{i} AS VARCHAR)" if _TEXT_ONLY_TYPE in t else f"#{i}"
                    for i, t in enumerate(types, start=1)
                )
            )
        rows = [[_json_safe(value) for value in row] for row in relation.fetchall()]
    except duckdb.PermissionException as exc:
        raise UsageError(f"query refused: {exc}", code="read_only") from None
    except duckdb.InvalidInputException as exc:
        if any(refusal in str(exc) for refusal in _REFUSALS):
            raise UsageError(f"query refused: {exc}", code="read_only") from None
        raise UsageError(f"query invalid: {exc}", code="invalid_input") from None
    except (duckdb.ParserException, duckdb.BinderException, duckdb.CatalogException) as exc:
        raise UsageError(f"query failed: {exc}", code="sql_error") from None
    return {"columns": columns, "rows": rows}


def _json_safe(value: object) -> object:
    """Strict JSON has no NaN, Infinity or bytes: non-finite floats become null and a BLOB
    becomes its lowercase hex digits."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return value.hex()
    return value
