# Post-ingest memory contract

## Resource contract

Event-local text/JSON transforms use physical event spans with the ingest writer's
row and byte caps. Selection and all body consumption share one batch transaction;
only a stable event-id anchor crosses commits, and its physical position is resolved
afresh. Body-table positions are resolved from narrow identities before
reading physical spans, following `ingest/common.py`'s raw-body retrieval rule.
JSON trees are decoded one event at a time in Python; only scalar signal flags
and the fixed `message.visibility = user_only` classification survive decoding.
SQL sees compact JSON, not the original trees.

Each session scope resolves its narrow event metadata once and materializes the
normalized tool calls once. All kinds share those inputs, avoiding repeated
history scans and repeated JSON normalization within a batch.

Whole-session batches use the same caps and never split a session's lag/lead
windows. An oversized session is a singleton. User-correction windows retain only
role, identity, a boolean and the bounded lexicon's matches. Compaction aggregation
retains only event identities and sequence numbers. Raw bodies do not survive into
these windows, including for sessions larger than the event byte cap.

Durable staging tables commit batches independently, and publication swaps tables
in a short transaction. Failed replacements preserve the previously published
result. Staging uses catalog DDL to preserve NOT NULL/CHECK constraints; CTAS alone
would remove them. A process killed during staging leaves only unpublished staging
state; the next computation replaces that state and replays from the source.

The policy membership test covers the CLI command registry, and the signal policy
sets must equal `KINDS`, with no overlap. Adding a command or kind without an
explicit resource policy fails the test.

## Statement-by-statement audit

### Pricing, metadata and signal operations

These additions retain the existing default pool/thread limits. The repricing
regression in `tests/test_value_contracts.py` uses a 32 MB pool with 10,000 usage
rows and 4 KiB irrelevant raw event bodies per row; its assertions verify the
priced amount and method. The existing 64 MB analysis regressions remain required.

| Operation | Bound / retained state |
| --- | --- |
| `pricing.install`: model mapping and `priced_usage` | Config-sized rate rows; usage scalars and model ids only. No event JSON or transcript bodies. |
| `pricing.refresh`: `refreshed_costs`, delete/insert, commit/drop | Spillable scalar aggregate per session; reported-cost rows preserved. The publication transaction contains scalar cost rows only. |
| `pricing.coverage` / `pricing.unpriced` | Numeric sums and narrow session/model identities. Python results contain harness coverage and distinct model groups. |
| `metadata.refresh`: title-event queue and body fetch | Page physical row positions and byte lengths; enforce the shared row/byte budgets, then decode one raw event at a time. Staging retains title metadata only. |
| `metadata.refresh`: latest-title window and metadata-kind update | Narrow title/priority/order fields in the window; scalar classification update, no raw JSON expansion. |
| `signals._result_flags` | Page tool-call identities using the shared byte/row budget; fetch result bodies by physical position and immediately reduce each to hash, failure and transient flags plus the result sequence number. |
| `signal_calls` / precision SQL | Whole-session scalar flags and bounded input batches; no result bodies enter the result-comparison or compaction windows. |
| `outcomes._direct_prs` | The owning physical event span supplies narrow positions; each matching `pr-link` body is decoded separately. Retained links are scalar identities. |
| `report.findings` | At most the requested top findings; fetch one cited correction message by physical position. Session/navigation queries project scalar metadata only. |

“Narrow” below means identities, paths, classifications, timestamps or numeric
aggregates, excluding raw JSON, message/tool/context bodies and signal evidence.
DuckDB can spill these history-wide joins and aggregates under its configured pool.
The exception for explicitly supplied ad hoc SQL is stated separately below.

| Owner / statements | Bound or justification |
| --- | --- |
| `analysis.session_batches`: queue CTAS, event/session anti-join | Narrow one-row-per-session aggregate; no raw bodies. |
| `analysis._batches`: ordered keyset SELECT | At most ingest `batch_rows` narrow session summaries in Python. |
| `analysis.scope`: identity and narrow event CTAS / DROP | One selected session batch, resolved once and shared across kinds. |
| `analysis.scoped`: materialized event/tool/invocation/session inputs | Filtered whole-session batch before wide expressions; shared by every session-local kind. |
| `analysis.event_batches`: rowid/byte-length keyset SELECT | At most ingest `batch_rows` narrow locations; split again by `BATCH_BYTES`. |
| `analysis._body_predicate`: body rowid lookup | Joins narrow event identities; returns only positions belonging to the current event span. No body is fetched by this lookup. |
| `analysis.event_transaction`: begin / next batch / commit / rollback | Position selection and body consumption use one snapshot; rollback also covers errors selecting the next batch. |
| `analysis.event_scoped`: event/message/tool/session CTEs | Physical row/byte span before regex, JSON or expansion; related body spans resolved afresh. |
| `analysis.create_replacement`: catalog SELECT / DDL | One catalog DDL string; creates an empty table. |
| `signals._event_flags`: raw SELECT / compact-flag INSERT | One event decoded at a time from a row/byte span; inserted flags are at most one event batch. |
| `turn_inputs.sql`: regex/context stripping, flag tests, lexicon matching | Event span; only narrow turn metadata and lexicon matches are retained. |
| `compaction_inputs.sql`: subtype / summary tests | Event span of compact flags; only matching event identities retained. |
| `prelude.sql`: shell normalization and edited paths | Global view definitions are catalog-only; each selected session scope materializes normalized calls once, and drops them after the batch. |
| `rerun_loop.sql`: edit epoch window, command grouping / evidence | Whole-session batch; windows partition by session. |
| `edit_churn.sql`: path expansion and distinct edits | Whole-session batch. |
| `unchanged_retry.sql`: neighboring call window | Whole-session batch; partitions by session, preserving neighboring-call semantics. |
| `user_correction.sql`: neighboring turn window | Whole-session batch of preprocessed narrow turns, never message text or original JSON. |
| `skill_abandoned.sql`: invocation / session-end / error lookups | Whole-session batch. |
| `orphaned_session.sql`: last event and unanswered calls | Whole-session batch. |
| `context_overflow.sql`: grouped ordered evidence | Whole-session batch of narrow matching event identities. |
| `never_fired_skill.sql`: global inventory anti-join | Installed inventory plus narrow skill names, event ids and timestamps; no wide columns. |
| `stale_pickle.sql`: run/log arg-max and stale predicate | Global narrow run/event/file metadata; one last-event identity per run. |
| `signals.compute`: staging CREATE / per-kind INSERT / COMMIT / publication / cleanup | Per-session batch writes; global writes are narrow inventory/run facts; final publication is DDL, not a full-history copy or DELETE transaction. |
| `signals.compute`: kind counts | Scalar aggregate per registered kind. |
| `signals._REPORT`: subject union and owner priority window | Narrow subject metadata window before the signal-body join. |
| `signals.report(top=N)`: narrow top selection / body retrieval | Filtered top-N identities selected before evidence/details; report requests `TOP_FINDINGS`. |
| `signals._signal_rows` / `write_report`: keyset SELECT, physical body SELECT and JSON emission | Only narrow identities/metadata are sorted and paged by the shared row cap. Evidence/details are fetched one signal at a time by physical position; no full-history wide sort or Python body list. |
| `rollups.sql`: recursive tree, session tokens, rollup, signal counts, keys, macro and views | All scans project narrow identities and scalar totals, never original JSON/text/tool bodies or signal evidence; aggregate rows per session/kind/key. View creation itself is catalog-only. |
| `outcomes.link`: cwd lookup / repo table INSERT | Distinct narrow filesystem paths and resolved repository identities. |
| `outcomes.link`: branch-only INSERT | Narrow session branch facts; empty event/body CTEs. Emits branches once, including sessions with no events. |
| `linkage.sql`: shell JSON normalization, URL/header expansion, ticket-message regex and candidate grouping | Event-local extractors see only a physical event/body batch. Branch/session repo metadata is narrow. |
| `outcomes.link`: final earliest-evidence aggregate | Narrow link identities plus source-event sequence; dedupes mentions spanning batches with the original `arg_min` rule. |
| `outcomes.link`: staging, commits, publication and count | Event-batch writes; narrow final aggregate; DDL swap; scalar count. |
| `github._REQUESTED` / `Fetcher.due` | Narrow distinct PR identities, states and fetch timestamps. |
| GitHub GraphQL / `_UPSERT` | Existing bounded PR request batches; each response row is upserted within that request's bound. |
| `github_candidates` CREATE / INSERT / SELECT / DROP | Spillable temporary storage for fetched/search candidate bodies; Python decodes one candidate at a time instead of retaining all bodies for the run. |
| `github._UNREVERTED` | Narrow PR identities, capped GitHub title and merge timestamp. |
| Revert UPDATE | One PR at a time; running minimum preserves the lowest later revert number across candidate batches. |
| `grading._FACTS` | Narrow session/PR identities and numeric aggregates; no signal evidence, event JSON or transcript bodies. |
| `selection.select_worst` | Current-rubric exclusion, weighting, sort and LIMIT execute over narrow facts; only requested N ids fetched in Python. |
| `rank` / `_GRADED` compatibility helpers | Narrow per-session numeric facts / successful-grade identities only. |
| `render._EVENTS`, `_CALLS`, `_BLOCKS` | One explicitly selected session; blob bodies restricted to that session's context event identities. |
| `_graded_rubrics` / `_store_grade` / `_record_call` | One session's rubric identities / one reduced verdict / one backend call. |
| `clusters._failure_rows` / compatibility `failures` | Current rubric/hash filter and keyset pagination carry scalar grade identities; each reason/evidence body is fetched by a freshly resolved physical position. The library `failures` list is output-sized; CLI consumes the iterator. |
| `stored_clusters`: spool CREATE / INSERT / metadata GROUP / DROP | One verdict body written at a time into spillable temporary storage. Only normalized cluster identities are grouped and retained in Python. |
| `_StoredFailures.__iter__` | Pages narrow spool ordinals and fetches one verdict body by physical position. No wide global sort. |
| `analysis.physical_row` | One narrow position lookup followed by one physical body lookup, without a commit in between. |
| `analysis.sized_batches` | Shared ingest row/byte budgets for proposal model inputs and stored evidence fragments; one oversized session verdict remains atomic. |
| `_skills_by_name` | Installed inventory only. |
| `surfaces`: cwd / invocation / rule-path SELECTs | One failure session; paths and identities only. |
| `_groups` / `_draft` / streamed sidecar JSON | Grade bodies are read from the spool into shared row/byte-bounded prompt batches; successive batches refine the current draft. Evidence sidecars stream all failures without retaining a history-sized JSON string. |
| `proposals._store` / `_insert` | Each cluster's evidence is split into the same bounded batches, with a stable distinct fragment id. Writes commit per fragment; publication swaps tables. |
| `writer._write_json` | Recursive mapping/iterator emission holds one evidence record at a time; scalar session/event-id summaries remain output-sized. |
| `report._ROLLUP_EXISTS` / `_GRADES` | Catalog scalar / narrow rubric-hash-label counts. |
| `report._stored_leaderboard` | Kind/session/severity aggregates only; no evidence or details; matches known-subject membership. |
| `report._PROPOSED` / `_REJECTED` | Proposal ordering projects narrow metadata only; each bounded evidence fragment is fetched by physical position before unioning scalar session ids. Rejections select distinct scalar summaries. |
| `status._FILES`, `_UNKNOWN`, `_LAST_RUNS`, schema version | Narrow classifications and scalar aggregates per harness/status/type/command; no event bodies. |
| `status.record_run` | One command record. |
| `query.harden` | Setting statements only. |
| `query.run` | Explicit caller-provided SQL escape hatch; SQL and result cardinality follow the caller's query, under the unchanged DuckDB memory pool. No transparent rewrite can preserve arbitrary SQL while bounding every result. |
| `rehydrate` lookup | One event location; one raw source record. |
| `run` | Sequential composition of the above; grading remains opt-in and is excluded from real-data proof. |

The transcript renderer retains one selected session. Proposal drafting reads
bounded batches from temporary storage, including when one owning surface has
failures across the whole history. Large clusters have multiple `proposals` rows
with the same `cluster_key` and distinct `proposal_id`s; each `evidence` value is a
complete fragment. Reports still union the same file's evidence sessions and show
one rejection per cluster. A single-fragment cluster preserves its previous id and
JSON membership. Full sidecar evidence retains every failure and is streamed. Full-document
library APIs can return output-sized Python collections; the full signals CLI
streams its wide results, while the analysis report selects only its top findings.

## Regression methodology

Synthetic histories exercise whole sessions with alternating turns, wide message
and tool bodies, repeated PR/commit/ticket evidence, wide stored signal details,
and obsolete grade reasons. The tests compare complete signal/link membership or
complete command-helper output between 64MB and 512MB. Signals also have an
unscoped SQL oracle at 512MB. Status and selection use the same wide raw history.

Fast contracts independently exercise row and byte caps, deadlines on
non-advancing queues, registry membership, failure after a prior batch commit,
cleanup, and preservation of non-index constraints. Proposal regressions compare
persisted failure identities and canonical evidence digests across memory limits.
No output-row count substitutes for membership assertions.

Mutation review covers dropped physical fences, missing cleanup, incorrect byte
accounting, eager evidence emission, missing session partitions, removed batch
commits and history-wide signal transactions. See
[mutation-exemptions.md](mutation-exemptions.md) for accepted equivalences.

A multi-megabyte single durable verdict fragment can hit DuckDB's atomic-write /
autocheckpoint floor at 64MB. The regression uses many smaller atomic records to
test history growth below that storage floor. No checkpoint tuning or higher
limit is used to hide it. Explicit ad hoc SQL and full-result library APIs retain
the caller/output-sized limits described above.

## Repository identities and grouped reports (v0.2)

Repository identity is derived on every reader connection, including read-only SQL,
with no persisted schema change. `repository_paths` pages distinct cwd strings;
Git reads only local common-directory, worktree-root and remote metadata. The
`repository_metadata` spool decodes only physical session-meta records under the
shared row and byte caps, one body at a time. Inherited records with no own
sequence cannot supply an identity. `session_projects` joins these narrow maps,
preferring recorded URLs to resolved filesystem identity, then full original paths.
Both maps live only for the connection; they are deliberate reader infrastructure,
not leaked analysis staging. Existing v4 rollups are shadowed by current temporary
views/macros so every reader applies the same fallback and counting policy.

Grouped reports page signal identities, fetch one details body by physical position,
and spool bounded scalar kind/subject/session/severity/call facts. Long subjects
retain a short display prefix plus a full SHA-256 identity. SQL groups and ranks
these scalars, choosing one group per kind per round. Only the selected groups'
five worst distinct sessions have their evidence and navigation metadata loaded.
Costs are summed after deduplicating affected sessions within each group, with
incomplete pricing counted separately. No history-sized evidence list or Python
set of all sessions is retained. The HTML includes the same bounded worst-session
list. Rollup worktree counts and the two session denominators are scalar aggregates.

Synthetic v0.2 contracts run at a 32 MB pool, observe both row and byte boundaries,
and compare complete identity membership. The v4 read-only regression compares
database bytes before and after reporting, including a stored older rollup macro.

Crush cache relocation can leave multiple raw export headers for one logical
session. Publication now selects one complete header by captured export mtime,
then source-file and event identity, before inserting session, authoritative cost
and usage totals. The window carries only one session's scalar metadata, never
message/tool bodies. Ingest detects duplicate derived session/cost identities in
narrow global aggregates and marks their header events pending for the existing
bounded finalizer. Already completed v4 offsets therefore repair on the next
ingest; raw event identities and JSON stay intact. Synthetic relocation,
newest-snapshot and session-only/cost-only repair cases run with a 32 MB pool.
