-- Small scalar table only: widen the cost method CHECK without rebuilding history.
ALTER TABLE session_cost RENAME TO session_cost_v3;
CREATE TABLE session_cost (
    session_id VARCHAR NOT NULL,
    cost_usd DOUBLE,
    method VARCHAR NOT NULL CHECK (method IN ('cost_state', 'price_table', 'partial_price_table', 'unpriced'))
);
INSERT INTO session_cost SELECT * FROM session_cost_v3;
DROP TABLE session_cost_v3;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title VARCHAR;
