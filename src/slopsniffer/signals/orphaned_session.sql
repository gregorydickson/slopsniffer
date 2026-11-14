-- Last turn per session; called only inside a whole-session scope.
WITH last_turns AS (
    SELECT e.session_id, arg_max(e.event_id, e.seq) AS event_id
    FROM events e JOIN sessions s USING (session_id)
    WHERE s.harness IN ('claude', 'codex', 'crush')
      AND s.ended_at < $now - $orphan_hours * INTERVAL '1 hour' AND e.kind IN ('message', 'tool_call', 'tool_result')
    GROUP BY e.session_id
)
SELECT l.session_id, 1.0 AS severity, [l.event_id] AS evidence_event_ids,
       json_object('reason', 'unanswered_tool_use',
                   'tools', list(t.tool_name ORDER BY t.tool_name)) AS details
FROM last_turns l
JOIN tool_calls t ON t.session_id = l.session_id AND t.use_event_id = l.event_id
WHERE t.result_event_id IS NULL
GROUP BY l.session_id, l.event_id
