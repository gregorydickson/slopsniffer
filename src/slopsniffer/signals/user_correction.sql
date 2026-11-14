-- Body transforms happen in event batches; only narrow turn metadata is windowed.
WITH conversation AS (
    SELECT *, lag(role) OVER turn AS previous_role, lag(event_id) OVER turn AS previous_event
    FROM signal_turns
    WHERE role = 'assistant' OR has_text
    WINDOW turn AS (PARTITION BY session_id ORDER BY seq, event_id)
)
SELECT session_id, 1.0 AS severity, [previous_event, event_id] AS evidence_event_ids,
       json_object('phrases', phrases) AS details
FROM conversation
WHERE role = 'user' AND previous_role = 'assistant' AND len(phrases) > 0
