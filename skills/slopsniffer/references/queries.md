# Read-only SQL reference

Use `slopsniffer query --json 'SQL'`. Every example below is tested against the
synthetic fixture database. Replace synthetic IDs, repository keys and dates with
values from the report. Keep LIMIT even for exploratory aggregate queries.

| Table/view | Useful columns and joins | Size/caution |
| --- | --- | --- |
| `sessions` | `session_id`, `parent_session_id`, `harness`, `cwd`, `project`, `title`, `started_at`, `ended_at` | One per session; `project` here is the raw recorded label |
| `session_projects` | Same columns plus `worktree` (resolved checkout root); `project` derived from recorded repository URL, Git common directory/remote, then full cwd or raw project | Shared read-time view; use for repo grouping |
| `session_cost` | `session_id`, `cost_usd`, `method` | Join sessions by session_id; incomplete pricing is not zero waste |
| `source_files` | `source_file_id`, `path`, `harness`, `status` | Source metadata; join events by source_file_id |
| `events` | `event_id`, `session_id`, `seq`, `record_type`, `source_file_id`, `byte_offset`, `byte_length`, `parsed` | Very big; parsed bodies are wide; filter narrowly before reading |
| `tool_calls` | `session_id`, `call_id`, `use_event_id`, `result_event_id`, `tool_name`, `input`, `result_text`, `is_error` | Very big; join events on use_event_id for sequence; avoid broad input/result scans |
| `messages`, `usage` | `event_id`; messages has `role`, `text`; usage has `input_tokens`, `output_tokens`, `cache_read`, `cache_write`, `model` | Very big; join events by event_id; text is wide |
| `signals` | `signal_id`, `session_id`, `kind`, `severity`, `details`, `evidence_event_ids` | Per finding; details/evidence can be wide |
| `skill_invocations` | `session_id`, `event_id`, `skill_name` | Join sessions/events; recorded invocations only |
| `skills` | `path`, `name`, `scope`, `repo` | Installed inventory; path is the key, skill names need not be unique |
| `grades`, `proposals` | Grades: `session_id`, `rubric`, `rubric_hash`, `status`, `label`; proposals: `proposal_id`, `cluster_key`, `path`, `diff_path`, `status`, `evidence` | Verdict/proposal bodies can be wide; filter by session or cluster before reading |
| `grade_calls` | `session_id`, `call_id`, `backend`, `input_tokens`, `output_tokens`, `status`, `error` | Paid call receipts; token usage can be null; dollar cost is not stored |
| `rollup_by_project`, `rollup_by_week`, `rollup_by_lane` | `key`, `top_level_sessions`, `sessions_including_subagents`, `worktree_count`, `total_cost_usd`, `cost_status`, `signal_counts` | Small aggregates after signals; counts include descendants as labelled |

The read-only query command installs `session_projects` even for an older database.
Repository identity is derived on read; there is no schema migration or partially
backfilled identity column. A missing worktree with no recorded identity keeps
its full original path, so resolving deleted worktrees is necessarily incomplete.

Top individual sessions by estimated cost (a child's cost is separate here):

```sql
SELECT s.session_id, s.project, s.harness, c.cost_usd, c.method
FROM session_projects s JOIN session_cost c USING (session_id)
ORDER BY c.cost_usd DESC NULLS LAST, s.session_id LIMIT 10;
```

One session's tool sequence, including evidence IDs for rehydration:

```sql
SELECT e.seq, t.tool_name, t.is_error, t.use_event_id, t.result_event_id
FROM tool_calls t JOIN events e ON e.event_id = t.use_event_id
WHERE t.session_id = 'rerun-pos'
ORDER BY e.seq, t.call_id LIMIT 50;
```

Repeated shell commands within one session (informational; not proof of waste):

```sql
SELECT coalesce(json_extract_string(input, '$.command'),
                json_extract_string(input, '$.cmd')) AS command, count(*) AS calls
FROM tool_calls WHERE session_id = 'rerun-pos'
  AND tool_name IN ('Bash', 'bash', 'shell', 'exec_command', 'Execute', 'local_shell')
GROUP BY command HAVING count(*) > 1
ORDER BY calls DESC, command LIMIT 20;
```

Findings for a repository and UTC week:

```sql
SELECT f.signal_id, f.session_id, f.kind, f.severity
FROM signals f JOIN session_projects s USING (session_id)
WHERE s.project = '/work/sig'
  AND s.started_at >= TIMESTAMPTZ '2026-11-09 00:00:00+00'
  AND s.started_at < TIMESTAMPTZ '2026-11-16 00:00:00+00'
ORDER BY f.severity DESC, f.signal_id LIMIT 20;
```

Repository totals, including pricing limitations and both session denominators:

```sql
SELECT key, top_level_sessions, sessions_including_subagents, worktree_count,
       total_cost_usd, cost_status
FROM rollup_by_project ORDER BY total_cost_usd DESC, key LIMIT 20;
```

Paid grading receipts (only after an approved paid run):

```sql
SELECT call_id, backend, input_tokens, output_tokens, status, error
FROM grade_calls WHERE session_id = 'rerun-pos'
ORDER BY started_at, call_id LIMIT 20;
```
