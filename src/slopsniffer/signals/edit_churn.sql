-- Many progressive edits are ordinary work. Require a repeated identical failed
-- verification between edits to this file: evidence of an unresolved repair cycle.
WITH edits AS (
    SELECT session_id, seq, use_event_id, unnest(edited_paths) AS path
    FROM signal_calls WHERE edited_paths IS NOT NULL
), bounds AS (
    SELECT session_id, path, min(seq) AS first_edit, max(seq) AS last_edit,
           count(DISTINCT use_event_id) AS edits,
           list_sort(list(DISTINCT use_event_id)) AS evidence
    FROM edits WHERE path IS NOT NULL AND path <> '' GROUP BY session_id, path
    HAVING count(DISTINCT use_event_id) >= $churn_min
), failures AS (
    SELECT b.session_id, b.path, c.tool_name, c.command, c.result_hash, count(*) AS failures,
           list(c.use_event_id ORDER BY c.seq) AS evidence
    FROM bounds b JOIN signal_calls c ON c.session_id = b.session_id
      AND c.seq > b.first_edit AND c.seq < b.last_edit
    WHERE c.failed AND NOT c.transient AND c.result_hash IS NOT NULL
      AND c.command IS NOT NULL
      AND regexp_matches(c.command, $verification_command)
      AND NOT regexp_matches(coalesce(c.command, ''), '(?i)mutant|mutation|mutmut')
    GROUP BY b.session_id, b.path, c.tool_name, c.command, c.result_hash HAVING count(*) > 1
)
SELECT b.session_id, b.edits / $churn_min AS severity,
       list_sort(list_distinct(b.evidence || f.evidence)) AS evidence_event_ids,
       json_object('path', b.path, 'edits', b.edits, 'repeated_failures', f.failures) AS details
FROM bounds b JOIN failures f USING (session_id, path)
QUALIFY row_number() OVER (PARTITION BY b.session_id, b.path ORDER BY f.failures DESC, f.result_hash) = 1
