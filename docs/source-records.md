# Source coverage and inventory semantics

Codex records now map as follows; the original bytes and parsed event are always
retained, including when a record has no additional derived row.

| Record | Analysis representation |
| --- | --- |
| `event_msg:mcp_tool_call_end` | Tool use/result under the same call id; name `mcp__<server>__<tool>`, invocation arguments, full result, transport/tool error flag. |
| `response_item:tool_search_call/output` | Paired `tool_search` tool call and tool definitions result. |
| `response_item:web_search_call`, `event_msg:web_search_end` | `web_search` call/action and result; adjacent identical id-less response observations collapse into the id-bearing end call. Standalone id-less calls use a stable event-derived id. |
| `event_msg:exec_command_end`, `event_msg:patch_apply_end` | Fallback shell/patch uses and results, paired with normal response calls by call id; shell exit and patch success flags retained. |
| `event_msg:context_compacted` | Compaction event consumed by context-overflow analysis. |
| `event_msg:sub_agent_activity` | Subagent event and event-owned observed child relationship. |
| `event_msg:collab_*_end` | Fallback spawn/interaction/wait/close calls; spawn/interaction child relationships where a child id is recorded. Only a spawn establishes a missing direct parent; interaction with a peer does not. |
| `event_msg:thread_rolled_back` | Rollback event with full original payload. Historical events remain available; rollback does not erase telemetry. |
| `event_msg:entered_review_mode/exited_review_mode` | Context event with review target/state. |

Inherited Codex parent replay is excluded from messages, usage, tools and child
relationships. Existing unknown records are reclassified using narrow metadata;
affected sessions with new derived rows are marked pending and rebuilt through
the existing crash-resumable, bounded finalizer. Classification-only changes do
not rebuild tool bodies. A one-time recognized-record migration also checks older
web-search, shell/patch completion and collaboration records against existing call
and relationship identities, including missing results. It stages only narrow
metadata in committed row/byte batches, excludes inherited ordinals, and marks
only missing derivations pending. Its constant-sized `ingest_parser_versions`
revision commits atomically with those pending markers; it does not repeat on
unchanged history. A failed rebuild remains pending for retry.

Recovery revision 2 checks id-less web observations by their use-event identity.
An already-derived web call in a session cannot hide a different missing call.
Stores with revision 1 run the corrected recovery once. Adjacent duplicate web
observations can conservatively schedule one rebuild; normal derivation still
collapses them using their action hashes.

Claude's `file-history-snapshot/delta` become queryable `file_history` events.
They describe backup bookkeeping, not another execution of an edit tool, so do
not create duplicate tool calls. `mode` and `permission-mode` are context events.
`worktree-state` and `relocated` also update the session's most recent explicit
working directory/branch. `fork-context-ref`, workflow `started/result/launched`
and `failed` preserve subagent lifecycle and findings/error context. Workflow
journals derive their owner from `<session>/subagents/workflows/<workflow>/journal.jsonl`;
they cannot all merge into a session named `journal`. Existing collisions are
reassigned before normal finalization. Event-owned `session_relations` retain
child identities and lifecycle kind without claiming that every observation is
a direct spawn.

UI/transport/account and artifact-monitor bookkeeping can remain unknown when
it has no tool, compaction, usage or direct-child execution semantics. Examples
include Claude's `agent-name`, `frame-link`, `bridge-session`, `session-owner`,
`artifact-autoreact-ledger` and `artifact-comment-monitor` records. Titles and
direct PR links are handled by metadata and outcome derivation respectively.
Unknown is an open set: future unrecognized types stay visible in status and are
never silently discarded.

## Skill inventory

The prior zero-file value described the source's lack of JSONL files, not a failed
walk. Symlinked skills already worked. Repeated worktrees, symlinks and plugin-cache
versions multiply installation paths without necessarily adding different skill
definitions. Ingest reports the actual installed-file count for skills.
Crush runs before the inventory, so newly discovered project skills are included
on the first ingest. Default global roots include `.claude/skills`, `.agents/skills` and `.codex/skills`;
the existing `.claude/plugins` root remains enabled. Explicit single `SKILL.md`
roots and plugin roots pointing directly at a `skills` directory are supported.

The skills table retains every discovered installation path. `never_fired_skill`
now emits one finding per identical `(name, content_hash)` definition, instead of
one per installation. Distinct definitions remain distinct findings. Namespaced
plugin invocations still match exactly; an unqualified plugin name matches only
when it names one unambiguous installed skill. The signal means no **recorded
explicit invocation** in the chosen window. It does not prove a skill was never
read through a file tool or followed by a harness that emits no invocation record.

## Progress

Ingest writes per-harness file completion, events, elapsed time and ETA to stderr.
Finalization also reports completed/total sessions and uses that phase's rate for
ETA. Work with no known total, such as the one-time recovery scan, displays `ETA ?`
instead of a completed-file estimate. TTY updates are at most every two seconds;
ordinary logs at most every thirty seconds, plus phase boundaries. `--json` stdout stays one machine-readable JSON
document. Inventory reports installed skill files, not invented transcript files.
