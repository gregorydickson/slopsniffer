"""Explicit list-price estimates; unknown models never inherit a family's price.

All global work projects scalar tokens/identities and uses spillable aggregates.
"""

import duckdb

from slopsniffer.config import Config

TOTALS = """
SELECT e.session_id, coalesce(sum(u.cost_usd), 0) AS cost_usd,
       CASE WHEN bool_and(u.priced) THEN 'price_table'
            WHEN bool_or(u.priced) THEN 'partial_price_table' ELSE 'unpriced' END AS method
FROM priced_usage u JOIN {events} e USING (event_id)
WHERE e.session_id IS NOT NULL GROUP BY e.session_id
"""


def install(con: duckdb.DuckDBPyConnection, config: Config, *, usage_table: str = "usage") -> None:
    """Install a connection-local mapping and one scalar cost fact per usage row."""
    names = list(config.pricing)
    prices = [config.pricing[name] for name in names]
    con.execute(
        "CREATE OR REPLACE TEMP TABLE model_prices AS SELECT "
        "unnest($1::VARCHAR[]) AS model, unnest($2::DOUBLE[]) AS input, "
        "unnest($3::DOUBLE[]) AS cache_read, unnest($4::DOUBLE[]) AS cache_write, "
        "unnest($5::DOUBLE[]) AS output",
        [
            names,
            [p.input_usd_per_mtok for p in prices],
            [p.cache_read_usd_per_mtok for p in prices],
            [p.cache_write_usd_per_mtok for p in prices],
            [p.output_usd_per_mtok for p in prices],
        ],
    )
    from slopsniffer.ingest.engine import _ident

    con.execute(f"""
CREATE OR REPLACE TEMP VIEW priced_usage AS
WITH matched AS (
    SELECT u.*, coalesce(exact.model, dated.model) AS price_model,
           coalesce(exact.input, dated.input) AS input_rate,
           coalesce(exact.cache_read, dated.cache_read) AS read_rate,
           coalesce(exact.cache_write, dated.cache_write) AS write_rate,
           coalesce(exact.output, dated.output) AS output_rate
    FROM {_ident(usage_table)} u LEFT JOIN model_prices exact ON exact.model = u.model
    LEFT JOIN model_prices dated ON dated.model = regexp_replace(
        u.model, '-(?:[0-9]{{8}}|[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})$', '')
), costs AS (
    SELECT *, coalesce(input_tokens, 0) + coalesce(cache_read, 0)
               + coalesce(cache_write, 0) + coalesce(output_tokens, 0) AS tokens,
           price_model IS NOT NULL AND
             (coalesce(cache_write, 0) = 0 OR write_rate > 0) AS priced
    FROM matched
)
SELECT *, CASE WHEN priced THEN (
    coalesce(input_tokens, 0) * input_rate + coalesce(cache_read, 0) * read_rate
    + coalesce(cache_write, 0) * write_rate + coalesce(output_tokens, 0) * output_rate
    ) / 1e6 END AS cost_usd
FROM costs
""")


def refresh(con: duckdb.DuckDBPyConnection, config: Config) -> None:
    """Reprice existing history, including unchanged files; retain reported cost-state."""
    install(con, config)
    con.execute(
        """
CREATE OR REPLACE TEMP TABLE refreshed_costs AS
"""
        + TOTALS.format(events="events")
    )
    con.begin()
    try:
        con.execute("DELETE FROM session_cost WHERE method <> 'cost_state'")
        con.execute("""
INSERT INTO session_cost
SELECT r.* FROM refreshed_costs r
WHERE NOT EXISTS (SELECT 1 FROM session_cost c WHERE c.session_id = r.session_id)
""")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.execute("DROP TABLE refreshed_costs")


def coverage(con: duckdb.DuckDBPyConnection) -> list[dict[str, object]]:
    """Session coverage includes sessions with no usage, rather than hiding them."""
    rows = con.execute("""
SELECT s.harness, count(*) AS sessions,
       count(*) FILTER (WHERE c.method IN ('price_table', 'cost_state')
                              AND c.cost_usd IS NOT NULL) AS priced_sessions,
       count(*) FILTER (WHERE c.method = 'partial_price_table') AS partial_sessions,
       count(*) FILTER (WHERE c.method IS NULL OR c.method = 'unpriced'
                              OR c.cost_usd IS NULL) AS unpriced_sessions,
       coalesce(sum(c.cost_usd), 0) AS known_cost_usd
FROM sessions s LEFT JOIN session_cost c USING (session_id) GROUP BY s.harness ORDER BY s.harness
""")
    columns = [col[0] for col in rows.description]
    return [dict(zip(columns, row, strict=True)) for row in rows.fetchall()]


def unpriced(con: duckdb.DuckDBPyConnection) -> list[dict[str, object]]:
    rows = con.execute("""
SELECT s.harness, u.model, count(*) AS usage_rows, count(DISTINCT e.session_id) AS sessions,
       sum(u.tokens) AS tokens, sum(coalesce(u.input_tokens, 0)) AS input_tokens,
       sum(coalesce(u.cache_read, 0)) AS cache_read_tokens,
       sum(coalesce(u.cache_write, 0)) AS cache_write_tokens,
       sum(coalesce(u.output_tokens, 0)) AS output_tokens
FROM priced_usage u JOIN events e USING (event_id) JOIN sessions s USING (session_id)
WHERE NOT u.priced GROUP BY s.harness, u.model ORDER BY tokens DESC, s.harness, u.model
""")
    columns = [col[0] for col in rows.description]
    return [dict(zip(columns, row, strict=True)) for row in rows.fetchall()]
