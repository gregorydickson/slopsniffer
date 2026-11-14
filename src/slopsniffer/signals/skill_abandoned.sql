-- A skill invocation after which the session ends within $abandon_window events and
-- an error was observed from the invocation on; identified transient tool failures
-- do not establish abandonment. Events of a
-- session are numbered 1..n by seq, so the events after the invocation are n - its seq.
WITH invocations AS (
    SELECT i.session_id, i.event_id, i.skill_name, i.via, e.seq
    FROM skill_invocations i JOIN events e USING (event_id)
), session_ends AS (
    SELECT session_id, max(seq) AS last_seq FROM events
    WHERE session_id IN (SELECT session_id FROM invocations) GROUP BY session_id
), outcomes AS (
    SELECT i.session_id, i.event_id, i.skill_name, i.via, s.last_seq - i.seq AS events_after,
           coalesce((SELECT list(c.result_event_id ORDER BY c.seq, c.use_event_id)
                     FROM signal_calls c
                     WHERE c.session_id = i.session_id AND c.seq >= i.seq
                       AND c.is_error AND NOT c.transient), [])
           || coalesce((SELECT list(x.event_id ORDER BY x.seq) FROM events x
                        WHERE x.session_id = i.session_id AND x.seq > i.seq
                          AND x.kind = 'error'), []) AS errors,
           EXISTS (SELECT 1 FROM signal_calls c
                   WHERE c.session_id = i.session_id AND c.seq > i.seq) AS called_after
    FROM invocations i JOIN session_ends s USING (session_id)
)
SELECT session_id, 1.0 AS severity, [event_id] || errors AS evidence_event_ids,
       json_object('skill', skill_name, 'via', via, 'events_after', events_after,
                   'reason', CASE WHEN len(errors) > 0 THEN 'error'
                                  ELSE 'no_further_tool_call' END) AS details
FROM outcomes
WHERE events_after <= $abandon_window AND len(errors) > 0
