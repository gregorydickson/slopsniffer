-- All rollup plans project narrow identities and scalar totals, never parsed JSON,
-- message text, tool inputs/results or signal evidence/details. Hash groups keep
-- one row per session/kind/key and can spill under the configured memory pool.
-- Rollups over session trees: a root session (no parent, or a parent never ingested) and
-- every subagent below it, so a subagent's tokens, cost and signals count toward its root.

CREATE OR REPLACE VIEW session_tree AS
WITH RECURSIVE tree (root_session_id, session_id) AS (
    SELECT session_id, session_id FROM sessions
    WHERE parent_session_id IS NULL
       OR parent_session_id NOT IN (SELECT session_id FROM sessions)
    UNION ALL
    SELECT tree.root_session_id, s.session_id
    FROM tree JOIN sessions s ON s.parent_session_id = tree.session_id
)
SELECT root_session_id, session_id FROM tree;

CREATE OR REPLACE VIEW session_tokens AS
SELECT e.session_id,
       sum(coalesce(u.input_tokens, 0) + coalesce(u.output_tokens, 0)
           + coalesce(u.cache_read, 0) + coalesce(u.cache_write, 0)) AS tokens
FROM usage u JOIN events e USING (event_id)
GROUP BY e.session_id;

-- Keep known dollars and count every incompletely priced member of the tree.
CREATE OR REPLACE VIEW session_rollup AS
SELECT r.session_id, r.harness AS lane, r.project, r.started_at,
       strftime(timezone('UTC', r.started_at), '%Y-%m-%d') AS day,
       count(*) AS tree_sessions,
       sum(coalesce(k.tokens, 0)) AS tokens,
       coalesce(sum(c.cost_usd), 0) AS cost_usd,
       count(*) FILTER (WHERE c.method IS NULL OR c.method IN ('unpriced', 'partial_price_table')
                              OR c.cost_usd IS NULL) AS unpriced_sessions,
       epoch(max(m.ended_at)) - epoch(min(m.started_at)) AS wall_seconds
FROM session_tree t
JOIN session_projects r ON r.session_id = t.root_session_id
JOIN sessions m ON m.session_id = t.session_id
LEFT JOIN session_tokens k ON k.session_id = t.session_id
LEFT JOIN session_cost c ON c.session_id = t.session_id
GROUP BY r.session_id, r.harness, r.project, r.started_at;

CREATE OR REPLACE VIEW session_signal_counts AS
SELECT t.root_session_id AS session_id, s.kind, count(*) AS signals
FROM signals s JOIN session_tree t ON t.session_id = s.session_id
GROUP BY t.root_session_id, s.kind;

-- The key each root session rolls up under, per dimension; a session tree counts once under
-- every skill invoked anywhere in it.
CREATE OR REPLACE VIEW rollup_keys AS
SELECT 'lane' AS dimension, lane AS key, session_id FROM session_rollup
UNION ALL SELECT 'project', project, session_id FROM session_rollup
UNION ALL SELECT 'day', day, session_id FROM session_rollup
UNION ALL SELECT 'week', strftime(date_trunc('week', timezone('UTC', started_at)), '%Y-%m-%d'), session_id FROM session_rollup
UNION ALL
SELECT DISTINCT 'skill', i.skill_name, t.root_session_id
FROM skill_invocations i JOIN session_tree t ON t.session_id = i.session_id;

CREATE OR REPLACE MACRO rollup(dim, only_project, only_lane, since) AS TABLE
WITH members AS (
    SELECT k.key, r.*
    FROM rollup_keys k JOIN session_rollup r USING (session_id)
    WHERE k.dimension = dim
      AND (only_project IS NULL OR r.project = only_project)
      AND (only_lane IS NULL OR r.lane = only_lane)
      AND (since IS NULL OR r.started_at >= since)
), totals AS (
    SELECT key, count(*) AS sessions, sum(tokens) AS total_tokens, avg(tokens) AS mean_tokens,
           sum(cost_usd) AS total_cost_usd,
           count(*) FILTER (WHERE unpriced_sessions > 0) AS unpriced_sessions,
           CASE WHEN sum(unpriced_sessions) > 0 THEN 'partial' ELSE 'complete' END AS cost_status,
           sum(wall_seconds) AS wall_seconds,
           count(*) AS top_level_sessions,
           sum(tree_sessions) AS sessions_including_subagents
    FROM members GROUP BY key
), worktrees AS (
    SELECT m.key, count(DISTINCT s.worktree) AS worktree_count
    FROM members m JOIN session_tree t ON t.root_session_id=m.session_id
    JOIN session_projects s ON s.session_id=t.session_id GROUP BY m.key
), kinds AS (
    SELECT key, json_group_object(kind, signals) AS signal_counts
    FROM (SELECT m.key, c.kind, sum(c.signals) AS signals
          FROM members m JOIN session_signal_counts c USING (session_id)
          GROUP BY m.key, c.kind)
    GROUP BY key
)
SELECT t.*, w.worktree_count, coalesce(k.signal_counts, '{}'::JSON) AS signal_counts
FROM totals t LEFT JOIN worktrees w ON w.key IS NOT DISTINCT FROM t.key
LEFT JOIN kinds k ON k.key IS NOT DISTINCT FROM t.key
ORDER BY t.key NULLS LAST;

CREATE OR REPLACE VIEW rollup_by_lane AS
SELECT * FROM rollup('lane', NULL::VARCHAR, NULL::VARCHAR, NULL::TIMESTAMPTZ);
CREATE OR REPLACE VIEW rollup_by_skill AS
SELECT * FROM rollup('skill', NULL::VARCHAR, NULL::VARCHAR, NULL::TIMESTAMPTZ);
CREATE OR REPLACE VIEW rollup_by_project AS
SELECT * FROM rollup('project', NULL::VARCHAR, NULL::VARCHAR, NULL::TIMESTAMPTZ);
CREATE OR REPLACE VIEW rollup_by_day AS
SELECT * FROM rollup('day', NULL::VARCHAR, NULL::VARCHAR, NULL::TIMESTAMPTZ);

CREATE OR REPLACE VIEW rollup_by_week AS
SELECT * FROM rollup('week', NULL::VARCHAR, NULL::VARCHAR, NULL::TIMESTAMPTZ);
