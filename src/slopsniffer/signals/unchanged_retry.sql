-- A retry must be issued after the first failure was observed, not in the same parallel burst.
WITH ordered AS (
    SELECT *, lead(tool_name) OVER next AS next_tool, lead(input::VARCHAR) OVER next AS next_input,
           lead(use_event_id) OVER next AS next_use,
           lead(seq) OVER next AS next_seq,
           lead(failed) OVER next AS next_failed,
           lead(result_hash) OVER next AS next_result_hash
    FROM signal_calls
    WINDOW next AS (PARTITION BY session_id ORDER BY seq, use_event_id, call_id)
)
SELECT session_id, 1.0 AS severity,
       [use_event_id, result_event_id, next_use] AS evidence_event_ids,
       json_object('tool', tool_name) AS details
FROM ordered
WHERE next_seq > result_seq AND failed AND next_failed AND result_hash = next_result_hash AND NOT transient AND next_tool = tool_name AND next_input = input::VARCHAR
  AND NOT regexp_matches(coalesce(command, ''),
      '(?i)\b(sleep|ps|tail|watch|wait|kill -0)\b|status.*--json|wake[.]sh|[.]log\b|droid-bg-')
