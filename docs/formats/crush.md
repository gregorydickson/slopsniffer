# Crush SQLite source

`ingest.sources.crush_projects` names `~/.local/share/crush/projects.json` by
default. Missing index means no-op. Each distinct existing `data_dir/crush.db`
(or `path/.crush/crush.db` without `data_dir`) is read. Once the index exists,
`~/.crush/crush.db` is also checked even if the home project is not indexed. Stale project entries are skipped. The index's `path` supplies both
project and cwd; no project name is inferred from the location of the database.

The live database is opened with SQLite's `mode=ro`, `timeout=0` and
`PRAGMA query_only=ON`. The online backup API copies it to a private temporary
SQLite file in 256-page steps. This includes committed WAL contents and produces
one consistent database snapshot. SQLite coordinates its short read locks;
WAL readers do not block the application's writer. No checkpoint, journal-mode
change, immutable live-file read, or independent copying of the database/WAL is
used. A busy or perpetually changing backup fails visibly after 60 seconds.
There is no fallback to an inconsistent main-file clone. The private snapshot is
deleted on success and failure. See [SQLite's backup API](https://www.sqlite.org/backup.html)
and [WAL concurrency](https://www.sqlite.org/wal.html).

For each session, the reader compares a digest of the complete sessions row and
narrow `(count, max row timestamp)` summaries of messages, files and read_files.
Thus an advanced session `updated_at` replaces that session's export; unchanged
sessions keep their export bytes and mtime. Changes to a message/file watermark
or row count also trigger replacement, including some writes made before the
session totals update. SQLite uses second-resolution timestamps; this is a row
version contract, not change-data capture of every intermediate edit between
ingests. A live row overwritten or deleted before any ingest cannot be recovered.

Exports live under `<data_dir>/sources/crush/<database-path-hash>/`. Each session
has a stable filename and is written to a temporary sibling followed by atomic
replacement. A revision digest is first in the header, ensuring a replacement
invalidates the ingest engine's head hash even if only a late message changed.
The export contains the full session row, every message row in deterministic
`(created_at,id)` order, and every files/read_files row for that session. JSON
strings such as `parts` retain their entire original string value. No message,
reasoning, tool output, file content, or extra SQLite column is truncated.

`rehydrate` returns **the exact exported JSONL record**, including its newline.
It is a logical SQLite row envelope with its original column values, original
database path (on the session header), and a namespaced session identity. It does
not return original SQLite pages, a SQL dump, WAL frames, or a reconstructable
physical database. Keep the `sources/crush` directory with DuckDB: removing or
changing these exports produces the ordinary source-missing/source-changed error.
Replacing a session export replaces its derived events just like rewriting a
transcript. Historical versions of an overwritten row are not an archive product.

Session ids are `crush:<database-path-hash>:<original-id>`; parent ids receive the
same namespace. This prevents identical ids in different databases from merging.
Titles, start/end timestamps, first recorded model, messages and tool calls are
mapped to the ordinary tables. Summary messages are compaction events and remain
messages, with an explicit summary flag for turn analysis. Text and reasoning
parts remain lossless in the event; only text parts enter messages.text.
`tool_call.data.id` pairs with `tool_result.data.tool_call_id`; input JSON and
`is_error` are retained. `sessions.cost` is authoritative and uses the existing
`cost_state` method. This version stores token totals only on the session event:
the source has no per-assistant token breakdown, so no allocation is invented.
Cache counts are unknown. Aggregate usage has a model only when exactly one model
is recorded in that session. The provider and all extra columns remain in raw rows.

Crush 0.100.0's observed shell tool is `bash`. Its edit tools are `edit`,
`multiedit`, `write`, and `lsp_replace_symbol`, all with `file_path`; the shared
signal normalizer recognizes these exact names. Tool-independent retry,
correction, compaction and unanswered-call signals consume ordinary derived rows.
Skills have no explicit Crush Skill-tool invocation record; reading a skill file
is not fabricated into an invocation. Pickle lifecycle signals still require
Pickle's own logs.

| Signal kind | Crush input / applicability |
| --- | --- |
| `rerun_loop` | `bash` with `command`, normalized by the shared shell mapper. |
| `edit_churn` | `edit`, `multiedit`, `write`, `lsp_replace_symbol` with `file_path`. |
| `unchanged_retry` | Any paired call's name, input and error flag; no name whitelist. |
| `user_correction` | User/assistant text turns, excluding summary turns. |
| `context_overflow` | Messages marked `is_summary_message`, classified as compaction. |
| `orphaned_session` | Last recorded turn and unanswered call; includes Crush. Its separate stale-Pickle arm requires lifecycle logs. |
| `skill_abandoned` | Requires an explicit skill invocation; none is invented for Crush. |
| `never_fired_skill` | Global installed inventory versus recorded invocations, independent of harness. |

Discovery, export and derivation use a row cursor or the existing row/byte batch
budgets. SQLite pages and wide tool bodies stay on disk; no history-sized body
collection or history-growing ART index is added. Synthetic SQLite fixtures cover
WAL inclusion, writer coexistence, failed exports, incremental replacement,
idempotence, cross-database identities, and 64MB derivation.
