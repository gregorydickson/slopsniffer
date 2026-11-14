WITH turns AS MATERIALIZED (
    SELECT e.session_id, e.seq, e.event_id, m.role,
           trim(regexp_replace(coalesce(m.text, ''),
               $own_text_pattern,
               '', 'g')) AS own_text
    FROM messages m JOIN events e USING (event_id)
    WHERE NOT regexp_matches(coalesce(m.text, ''),
        '(?is)^\s*(?:Another Claude session sent a message:|<teammate-message|<task-notification|Unable to reach https://)')
      AND m.role IN ('user', 'assistant') AND e.session_id IS NOT NULL
      AND coalesce(json_extract_string(e.parsed, '$.message.visibility'), '') <> 'user_only'
      AND NOT coalesce(TRY_CAST(json_extract_string(e.parsed, '$.isMeta') AS BOOLEAN), false)
      AND NOT coalesce(
          TRY_CAST(json_extract_string(e.parsed, '$.isCompactSummary') AS BOOLEAN), false)
)
SELECT session_id, seq, event_id, role, own_text <> '' AS has_text,
       list_filter($correction_lexicon::VARCHAR[],
                   lambda phrase: contains(lower(own_text), lower(phrase))
                     AND (NOT list_contains($clause_phrases::VARCHAR[], lower(phrase))
                          OR regexp_matches(own_text, $correction_clause))
                     AND (lower(phrase) <> 'try again' OR NOT regexp_matches(own_text,
                          $recovery_retry))) AS phrases
FROM turns
