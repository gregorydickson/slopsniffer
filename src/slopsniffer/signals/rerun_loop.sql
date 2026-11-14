-- The same shell command run at least $rerun_min times with no file edit between the runs:
-- calls are grouped by the number of edits before them, so an edit starts a new group.
WITH epochs AS (
    SELECT session_id, seq, use_event_id, command, result_hash, failed, transient,
           count(*) FILTER (WHERE edited_paths IS NOT NULL) OVER (
               PARTITION BY session_id ORDER BY seq, use_event_id ROWS UNBOUNDED PRECEDING
           ) AS edits_before
    FROM signal_calls
), repeated AS (
SELECT session_id, command, count(*) AS runs,
       list(use_event_id ORDER BY seq, use_event_id) AS evidence_event_ids,
       bool_and(failed AND NOT transient) AS failed_check
FROM epochs
WHERE command IS NOT NULL AND result_hash IS NOT NULL
  -- Polling reads externally changing state; it is not a deterministic retry.
  AND NOT regexp_matches(command,
      '(?i)\b(sleep|ps|tail|watch|wait|kill -0)\b|\b(gh|gt) .*(checks|status)|status.*--json|wake[.]sh|[.]log\b|droid-bg-|/tasks/|/jobs/')
GROUP BY session_id, edits_before, command, result_hash
HAVING count(*) >= $rerun_min
)
-- Re-inspection and successful verification can be intentional. Only deterministic
-- failed checks and literal busy-wait builtins establish actionable repetition.
SELECT session_id,
       CASE WHEN (failed_check AND regexp_matches(command, $verification_command)
                  AND NOT regexp_matches(command, '(?i)mutant|mutation|mutmut'))
                  OR regexp_full_match(command, $noop_command)
            THEN runs / $rerun_min ELSE 0.0 END AS severity,
       evidence_event_ids,
       json_object('command', command, 'runs', runs, 'interpretation',
                   CASE WHEN severity > 0 THEN 'actionable_repetition'
                        ELSE 'repetition_not_proven_waste' END) AS details
FROM repeated
