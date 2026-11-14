-- Narrow event identities and file timestamps only; no bodies/JSON.
WITH run_logs AS (
    SELECT r.pickle_run_id, arg_max(e.event_id, (f.mtime_ns, e.byte_offset)) AS event_id
    FROM pickle_runs r
    JOIN source_files f ON f.harness = 'pickle' AND starts_with(f.path, r.session_dir || '/')
    JOIN events e ON e.source_file_id = f.source_file_id
    GROUP BY r.pickle_run_id
)
SELECT r.pickle_run_id AS session_id, 1.0 AS severity,
       CASE WHEN g.event_id IS NULL THEN []::VARCHAR[] ELSE [g.event_id] END AS evidence_event_ids,
       json_object('reason', 'stale_pickle_run', 'last_log_write_at',
                   strftime(timezone('UTC', r.last_log_write_at), '%Y-%m-%dT%H:%M:%SZ')) AS details
FROM pickle_runs r LEFT JOIN run_logs g USING (pickle_run_id)
WHERE r.active
  AND coalesce(r.last_log_write_at, to_timestamp(r.start_time_epoch))
      < CAST($now AS TIMESTAMPTZ) - to_hours(CAST($orphan_hours AS BIGINT))
