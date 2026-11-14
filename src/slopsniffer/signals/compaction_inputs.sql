SELECT event_id, session_id, seq FROM events
WHERE session_id IS NOT NULL
  AND (kind = 'compaction' OR record_type = 'summary'
       OR (record_type = 'system'
           AND json_extract_string(parsed, '$.subtype') = 'compact_boundary')
       OR (record_type = 'user'
           AND coalesce(TRY_CAST(json_extract_string(parsed, '$.isCompactSummary') AS BOOLEAN),
                        false)))
