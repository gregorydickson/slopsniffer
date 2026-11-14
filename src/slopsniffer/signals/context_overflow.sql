-- Compaction is context overhead, not itself proof of waste. Exclude inherited
-- startup history and count one compaction per interval of active tool work.
WITH active AS (
    SELECT c.*, (SELECT count(*) FROM signal_calls t
                 WHERE t.session_id = c.session_id AND t.seq < c.seq) AS work_before
    FROM signal_compactions c
), unique_compactions AS (
    SELECT * FROM active WHERE work_before > 0
    QUALIFY row_number() OVER (PARTITION BY session_id, work_before ORDER BY seq, event_id) = 1
)
SELECT session_id, 0.0 AS severity,
       list(event_id ORDER BY seq, event_id) AS evidence_event_ids,
       json_object('events', count(*), 'interpretation', 'context_overhead_not_proven_waste') AS details
FROM unique_compactions GROUP BY session_id
