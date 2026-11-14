-- Tool calls in session order, with the shell command (whitespace-normalized) and the file
-- paths an edit touched. Read by the kind files; a temp view, rebuilt on every computation.
CREATE OR REPLACE TEMP VIEW signal_calls AS
WITH calls AS (
    SELECT t.session_id, u.seq, r.result_seq, t.call_id,
           t.use_event_id, t.result_event_id, t.tool_name, t.input,
           t.is_error, r.result_hash, r.failed, r.transient, coalesce(json_extract(t.input, '$.command'),
                                json_extract(t.input, '$.cmd')) AS shell
    FROM tool_calls t JOIN events u ON u.event_id = t.use_event_id
    LEFT JOIN signal_result_flags r ON r.session_id = t.session_id AND r.call_id = t.call_id
)
SELECT session_id, seq, result_seq, call_id, use_event_id, result_event_id, tool_name, input, is_error,
       result_hash, coalesce(failed, false) AS failed, coalesce(transient, false) AS transient,
       CASE WHEN tool_name IN ('Bash', 'shell', 'exec_command', 'local_shell', 'Execute', 'bash') THEN
           NULLIF(trim(regexp_replace(
               CASE json_type(shell)
                   WHEN 'ARRAY' THEN array_to_string(CAST(shell AS VARCHAR[]), ' ')
                   ELSE json_extract_string(shell, '$')
               END, '[ \t]+', ' ', 'g')), '')
       END AS command,
       CASE WHEN tool_name IN ('Edit', 'MultiEdit', 'Write', 'NotebookEdit', 'Create',
                                'edit', 'multiedit', 'write', 'lsp_replace_symbol') THEN
           [coalesce(json_extract_string(input, '$.file_path'), json_extract_string(input, '$.path'),
                     json_extract_string(input, '$.notebook_path'))]
            WHEN tool_name = 'apply_patch' AND json_type(input, '$.changes') = 'OBJECT' THEN
           json_keys(input, '$.changes')
            WHEN tool_name = 'apply_patch' THEN
           regexp_extract_all(coalesce(json_extract_string(input, '$.input'),
                                       json_extract_string(input, '$')),
                              '\*\*\* (?:Add|Update|Delete) File: ([^\n]+)', 1)
       END AS edited_paths
FROM calls;
