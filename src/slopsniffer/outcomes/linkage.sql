-- Rebuild session_links from the ingested tables. Reads temp table link_repos(cwd, repo): the
-- GitHub owner/name of each session cwd's origin remote, resolved by linkage.py.
-- $ticket_pattern is outcomes.ticket_pattern; empty means no ticket links.
-- PR URLs are read only from the output of the shell call that created the PR, never from
-- message text, so a URL inside a pasted document cannot link.
INSERT INTO session_links (session_id, repo, branch, pr_number, commit_sha, ticket_id,
                           source_event_id, method)
WITH session_repos AS (
    SELECT s.session_id, s.git_branch, r.repo
    FROM sessions s LEFT JOIN link_repos r ON r.cwd = s.cwd
), shell AS (
    SELECT t.session_id, t.result_event_id, t.result_text, e.seq,
           NULLIF(trim(regexp_replace(
               CASE json_type(t.shell)
                   WHEN 'ARRAY' THEN array_to_string(CAST(t.shell AS VARCHAR[]), ' ')
                   ELSE json_extract_string(t.shell, '$')
               END, '\s+', ' ', 'g')), '') AS command
    FROM (
        SELECT *, coalesce(json_extract(input, '$.command'), json_extract(input, '$.cmd')) AS shell
        FROM tool_calls
        WHERE tool_name IN ('Bash', 'shell', 'exec_command', 'local_shell', 'Execute')
          AND result_text IS NOT NULL
    ) t
    JOIN events e ON e.event_id = t.result_event_id
), pr_urls AS (
    SELECT session_id, result_event_id, seq,
           CASE WHEN regexp_matches(command, '\bgh\s+pr\s+create\b') THEN 'gh_pr_create'
                ELSE 'gt_submit' END AS method,
           unnest(regexp_extract_all(
               result_text,
               '(?:github\.com/[\w.-]+/[\w.-]+/pull|app\.graphite\.(?:dev|com)/github/pr/[\w.-]+/[\w.-]+)/\d+'
           )) AS url
    FROM shell
    WHERE regexp_matches(command, '\bgh\s+pr\s+create\b|\bgt\s+(?:submit|ss)\b')
), commit_headers AS (
    -- `git commit` prints `[<branch> <sha>] <subject>` (`[<branch> (root-commit) <sha>]`).
    SELECT session_id, result_event_id, seq,
           unnest(regexp_extract_all(result_text, '(?m)^\[[^\]\n]+ [0-9a-f]{7,40}\]')) AS header
    FROM shell
    WHERE regexp_matches(command, '\bgit\b[^|;&]*\bcommit\b')
), ticket_messages AS (
    -- Injected context (CLAUDE.md, AGENTS.md, environment) is not something the session said.
    SELECT e.session_id, m.event_id, e.seq,
           upper(unnest(regexp_extract_all(
               regexp_replace(
                   m.text,
                   '(?s)<system-reminder>.*?</system-reminder>|<user_instructions>.*?</user_instructions>|<environment_context>.*?</environment_context>',
                   '', 'g'),
               '(?i)\b(?:' || $ticket_pattern || ')\b', 0))) AS ticket
    FROM messages m JOIN events e USING (event_id)
    WHERE m.role IN ('user', 'assistant') AND m.text IS NOT NULL AND e.session_id IS NOT NULL
      AND $ticket_pattern <> ''
), ticket_branches AS (
    SELECT session_id,
           upper(unnest(regexp_extract_all(
               git_branch, '(?i)\b(?:' || $ticket_pattern || ')\b', 0))) AS ticket
    FROM session_repos
    WHERE $ticket_pattern <> ''
), candidates AS (
    SELECT session_id, repo, git_branch AS branch, CAST(NULL AS INTEGER) AS pr_number,
           CAST(NULL AS VARCHAR) AS commit_sha, CAST(NULL AS VARCHAR) AS ticket_id,
           CAST(NULL AS VARCHAR) AS source_event_id, CAST(NULL AS BIGINT) AS seq,
           'git_branch' AS method
    FROM session_repos WHERE git_branch IS NOT NULL AND git_branch <> 'HEAD'
    UNION ALL
    SELECT session_id, NULL, NULL, NULL, NULL, ticket, NULL, NULL, 'ticket_branch'
    FROM ticket_branches
    UNION ALL
    SELECT session_id, lower(regexp_extract(url, '([\w.-]+/[\w.-]+)/(?:pull/)?\d+$', 1)), NULL,
           CAST(regexp_extract(url, '(\d+)$', 1) AS INTEGER), NULL, NULL, result_event_id, seq,
           method
    FROM pr_urls
    UNION ALL
    SELECT c.session_id, r.repo,
           NULLIF(regexp_extract(c.header, '^\[(.+?)(?: \(root-commit\))? [0-9a-f]{7,40}\]$', 1),
                  'detached HEAD'),
           NULL, regexp_extract(c.header, '([0-9a-f]{7,40})\]$', 1), NULL, c.result_event_id,
           c.seq, 'git_commit'
    FROM commit_headers c LEFT JOIN session_repos r USING (session_id)
    UNION ALL
    SELECT session_id, NULL, NULL, NULL, NULL, ticket, event_id, seq, 'ticket_mention'
    FROM ticket_messages
)
SELECT session_id, repo, branch, pr_number, commit_sha, ticket_id, arg_min(source_event_id, seq),
       method
FROM candidates
GROUP BY session_id, repo, branch, pr_number, commit_sha, ticket_id, method
