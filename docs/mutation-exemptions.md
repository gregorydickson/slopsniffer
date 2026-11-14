# Mutation exemptions

Mutants that `uv run mutmut run` leaves alive, and why each is accepted rather than killed.
Every entry is either equivalent (no input can tell it from the original) or presentation-only
(it changes page styling or wording that no contract promises). Run one module's mutants with a
name glob, for example
`uv run mutmut run 'slopsniffer.report*' 'slopsniffer.query*' 'slopsniffer.status*'`.

## slopsniffer.query

- `slopsniffer.query.x_harden__mutmut_3` — Equivalent because DuckDB setting names, SQL keywords and the UTC timezone value are case-insensitive, so hardening applies the same settings.
- `slopsniffer.query.x_harden__mutmut_4` — Equivalent because DuckDB setting names, SQL keywords and the UTC timezone value are case-insensitive, so hardening applies the same settings.
- `slopsniffer.query.x_harden__mutmut_7` — Equivalent because DuckDB setting names, SQL keywords and the UTC timezone value are case-insensitive, so hardening applies the same settings.
- `slopsniffer.query.x_harden__mutmut_8` — Equivalent because DuckDB setting names, SQL keywords and the UTC timezone value are case-insensitive, so hardening applies the same settings.
- `slopsniffer.query.x_harden__mutmut_11` — Equivalent because DuckDB setting names, SQL keywords and the UTC timezone value are case-insensitive, so hardening applies the same settings.
- `slopsniffer.query.x_harden__mutmut_12` — Equivalent because DuckDB setting names, SQL keywords and the UTC timezone value are case-insensitive, so hardening applies the same settings.

- `harden`: case-changed setting statements (`set timezone = 'utc'`, `SET TIMEZONE = 'UTC'`,
  and the same for `enable_external_access` and `lock_configuration`). Equivalent: DuckDB
  setting names and values are case-insensitive. Removing any of the three statements is killed
  by `tests/cli/test_query_readonly.py::test_a_hardened_connection_is_utc_offline_and_locked`.

The error codes and message text of `run`'s failures are asserted by
`test_each_failure_names_its_kind_and_carries_the_engine_message`, and the strict-JSON row
conversion by `test_rows_are_strict_json`. No mutant in `run` is exempt.

## slopsniffer.report.html

- `slopsniffer.report.html.x__diff__mutmut_4` — Equivalent because Python resolves UTF-8 and utf-8 to the same codec when reading the diff.
- `slopsniffer.report.html.x_write__mutmut_22` — Equivalent because Python resolves UTF-8 and utf-8 to the same codec when writing the page.
- `slopsniffer.report.html.x__table__mutmut_15` — Equivalent because HTML tag names are case-insensitive, so changing the row tag case preserves the parsed table and visible content.
- `slopsniffer.report.html.x__table__mutmut_22` — Equivalent because HTML tag names are case-insensitive, so changing the row tag case preserves the parsed table and visible content.
- `slopsniffer.report.html.x_render__mutmut_20` — Equivalent because HTML closing tag names are case-insensitive, so the same section is closed.
- `slopsniffer.report.html.x_render__mutmut_22` — Equivalent because the HTML doctype keyword is case-insensitive and selects the same standards rendering mode.
- `slopsniffer.report.html.x_render__mutmut_23` — Equivalent because HTML tag and attribute names, the HTML doctype, en language tag and UTF-8 charset label are case-insensitive in this document.

Presentation-only, and only these kinds: the text of `_STYLE` (CSS), the doctype and `<head>`
boilerplate, heading and paragraph wording (`TITLES`, "Generated", "Draft:", "Evidence
sessions:", the `None.` empty-state text and its `class="empty"`), and tag names of the table
and section wrappers other than the `<section id=...>` attribute the tests parse.

Not exempt: every `escape(...)` call. `tests/cli/test_report_escaping.py` plants a distinct
payload in each database-sourced field (table headers and cells, rollup dimension and rubric
`<h3>`, grade labels, proposal skill and path, evidence sessions, the diff, rejected cluster key
and reason, the generation time) and asserts it appears escaped and never raw, so removing any
single escape fails. A write failure's temp-file cleanup is asserted by
`tests/cli/test_report.py::test_html_that_cannot_be_written_exits_2_and_leaves_no_temp_file`.

## slopsniffer.report.paths

- `slopsniffer.report.paths.x_display_report__mutmut_1` — Equivalent because typing.cast returns the displayed report unchanged regardless of its type-only first argument.

## slopsniffer.report

- `_leaderboard`: mutants of the secondary sort key (`kv[0]`, the kind name, breaking ties of
  equal total severity). Equivalent on every fixture whose kinds have distinct totals; the report
  promises only the descending-total order. The row values (`signals`, `sessions`,
  `total_severity`, `max_severity`) are asserted against the stored signals and are not exempt;
  the fixture holds a kind with two signals in one session, so `signals` and `sessions` differ.
- `_proposals`: secondary ordering mutants on rows the report orders only by its primary key.

## slopsniffer.status

- `slopsniffer.status.x_record_run__mutmut_6` — Equivalent because DuckDB SQL keywords and unquoted column identifiers are case-insensitive, so the same command-run facts are inserted.
- `slopsniffer.status.x_record_run__mutmut_7` — Equivalent because DuckDB SQL keywords and unquoted column identifiers are case-insensitive, so the same command-run facts are inserted.
- `slopsniffer.status.x_record_run__mutmut_9` — Equivalent because DuckDB SQL keywords and unquoted column identifiers are case-insensitive, so the same command-run facts are inserted.
- `slopsniffer.status.x_collect__mutmut_35` — Equivalent because DuckDB SQL keywords and unquoted identifiers are case-insensitive, so the same maximum schema version is read.
- `slopsniffer.status.x_collect__mutmut_36` — Equivalent because DuckDB SQL keywords and unquoted identifiers are case-insensitive, so the same maximum schema version is read.
- `slopsniffer.status.x_collect__mutmut_49` — Equivalent because database initialization records a non-null schema version before status reads it, so the changed fallback condition or value is unreachable through the CLI.
- `slopsniffer.status.x_collect__mutmut_52` — Equivalent because database initialization records a non-null schema version before status reads it, so the changed fallback condition or value is unreachable through the CLI.
- `slopsniffer.status.x_collect__mutmut_55` — Equivalent because database initialization records a non-null schema version before status reads it, so the changed fallback condition or value is unreachable through the CLI.

- `record_run` and `_LAST_RUNS`: case-only SQL mutants (`insert into`, `values`,
  `select max(version) ...`). Equivalent: SQL keywords are case-insensitive.
- `collect`: the `schema_version` fallback (`or True`, `and` to `or`, `else 0` to `else 1`).
  Equivalent in practice: the schema writer always stores a `schema_version` row before any
  reader opens the database, so the fallback branch is unreachable.

The database size's WAL term is asserted by
`tests/cli/test_status.py::test_db_size_adds_the_wal_to_the_database_file`, and each `run` step's
command-run row by `tests/cli/test_contract.py::test_run_logs_each_step_as_its_own_command_run`.

## slopsniffer.ingest

Historical generated IDs are listed individually below with the reason each
survivor is accepted. IDs can change when production source changes.

The frontmatter index-reset loops (26 and 47), common pending loops (8 and 13),
non-advancing pool continuation (74), and unbounded worker join (76) are killed.
No loop is exempted. Pickle source IDs 11, 15, and 27 only change SQL identifier
case; their earlier timeout classifications were not infinite-loop mutations.

### slopsniffer.ingest.claude

- `slopsniffer.ingest.claude.x__context_rows__mutmut_9` (survived): Equivalent: Python normalizes the codec names `utf-8` and `UTF-8` to the same codec, including for malformed input.
- `slopsniffer.ingest.claude.x__split_reminder__mutmut_12` (survived): Equivalent: `headers` and `[*headers[1:], None]` always have the same length, so `zip` strictness cannot affect iteration or errors.
- `slopsniffer.ingest.claude.x__split_reminder__mutmut_15` (survived): Equivalent: `headers` and `[*headers[1:], None]` always have the same length, so `zip` strictness cannot affect iteration or errors.
- `slopsniffer.ingest.claude.x__split_reminder__mutmut_17` (survived): Equivalent: `headers` and `[*headers[1:], None]` always have the same length, so `zip` strictness cannot affect iteration or errors.
- `slopsniffer.ingest.claude.x_source__mutmut_6` (survived): Equivalent: `Path.rglob('*.jsonl')` yields no matches when the root is not a directory, so removing the `is_dir()` short circuit preserves discovery.
- `slopsniffer.ingest.claude.xǁClaudeParserǁparse_line__mutmut_6` (survived): Equivalent: Python normalizes the codec names `utf-8` and `UTF-8` to the same codec, including for malformed input.

### slopsniffer.ingest.codex

- `slopsniffer.ingest.codex.x_source__mutmut_6` (survived): Equivalent: `Path.rglob('*.jsonl')` yields no matches when the root is not a directory, so removing the `is_dir()` short circuit preserves discovery.
- `slopsniffer.ingest.codex.xǁCodexParserǁparse_line__mutmut_53` (survived): Equivalent: `_rows` yields rows only for two record types already in `KINDS`. For unknown types, calling it yields nothing and has no side effects.
- `slopsniffer.ingest.codex.xǁCodexParserǁparse_line__mutmut_6` (survived): Equivalent: Python normalizes the codec names `utf-8` and `UTF-8` to the same codec, including for malformed input.

### slopsniffer.ingest.common


### slopsniffer.ingest.droid

- `slopsniffer.ingest.droid.x__insert_usage__mutmut_22` (survived): Equivalent: each retained `_sidecar_usage` result is a six-element tuple, so all transposed rows have identical width and `zip` strictness has no effect.
- `slopsniffer.ingest.droid.x__insert_usage__mutmut_24` (survived): Equivalent: each retained `_sidecar_usage` result is a six-element tuple, so all transposed rows have identical width and `zip` strictness has no effect.
- `slopsniffer.ingest.droid.x__insert_usage__mutmut_25` (survived): Equivalent: each retained `_sidecar_usage` result is a six-element tuple, so all transposed rows have identical width and `zip` strictness has no effect.
- `slopsniffer.ingest.droid.x__insert_usage__mutmut_8` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.droid.x__insert_usage__mutmut_9` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.droid.x__sidecar_usage__mutmut_6` (survived): Equivalent: Python normalizes the codec names `utf-8` and `UTF-8` to the same codec, including for malformed input.
- `slopsniffer.ingest.droid.x_source__mutmut_6` (survived): Equivalent: `Path.rglob('*.jsonl')` yields no matches when the root is not a directory, so removing the `is_dir()` short circuit preserves discovery.
- `slopsniffer.ingest.droid.xǁDroidParserǁparse_line__mutmut_6` (survived): Equivalent: Python normalizes the codec names `utf-8` and `UTF-8` to the same codec, including for malformed input.

### slopsniffer.ingest.engine

- `slopsniffer.ingest.engine.x__event_columns__mutmut_2` (survived): Equivalent: every batch event is a fixed four-element tuple `(event_id, offset, length, event)`, so all iterables passed to the transposing `zip` have equal length.
- `slopsniffer.ingest.engine.x__event_columns__mutmut_4` (survived): Equivalent: every batch event is a fixed four-element tuple `(event_id, offset, length, event)`, so all iterables passed to the transposing `zip` have equal length.
- `slopsniffer.ingest.engine.x__event_columns__mutmut_5` (survived): Equivalent: every batch event is a fixed four-element tuple `(event_id, offset, length, event)`, so all iterables passed to the transposing `zip` have equal length.
- `slopsniffer.ingest.engine.x__fail__mutmut_14` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__fail__mutmut_15` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__fail__mutmut_7` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__mark_missing__mutmut_17` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__mark_missing__mutmut_20` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__mark_missing__mutmut_21` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__mark_missing__mutmut_7` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__mark_missing__mutmut_8` (survived): Equivalent: the uppercased filter additionally scans rows already marked missing. Nonexistent paths are only assigned the same missing status again, and existing paths are left alone; rows and all other source-file fields are unchanged. SQL names remain case-insensitive.
- `slopsniffer.ingest.engine.x__owned_tables__mutmut_10` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__owned_tables__mutmut_13` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__owned_tables__mutmut_14` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__owned_tables__mutmut_4` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__owned_tables__mutmut_5` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__owned_tables__mutmut_7` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__prepare__mutmut_10` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__prepare__mutmut_11` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__prepare__mutmut_20` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__prepare__mutmut_21` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__prepare__mutmut_26` (survived): Equivalent: this is only the temporary offset in the new-file `_Stored` value. New files return offset zero explicitly and snapshot updates do not read that temporary field; failure handling reads only the file id.
- `slopsniffer.ingest.engine.x__prepare__mutmut_34` (survived): Equivalent: this temporary status is used only by the unchanged-file check, where both alternatives remain unequal to `complete`. The new-file branch writes the separate literal `new` status before returning.
- `slopsniffer.ingest.engine.x__prepare__mutmut_7` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__prepare__mutmut_8` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__reingest__mutmut_13` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__reingest__mutmut_14` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__reingest__mutmut_25` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__reingest__mutmut_26` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__run_pool__mutmut_38` (survived): Equivalent: both returning and breaking after reporting exhausted pending requests execute the same finally cleanup; the function has no code after that cleanup.
- `slopsniffer.ingest.engine.x__run_pool__mutmut_46` (survived): Equivalent: typing.cast returns its value unchanged at runtime; changing only its type argument does not change the ready connection.
- `slopsniffer.ingest.engine.x__update_snapshot__mutmut_10` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__update_snapshot__mutmut_6` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__update_snapshot__mutmut_7` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x__update_snapshot__mutmut_9` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x_locate_event__mutmut_10` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x_locate_event__mutmut_11` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x_locate_event__mutmut_7` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.engine.x_locate_event__mutmut_8` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.

### slopsniffer.ingest.passthrough

- `slopsniffer.ingest.passthrough.xǁPassthroughParserǁparse_line__mutmut_6` (survived): Equivalent: Python normalizes the codec names `utf-8` and `UTF-8` to the same codec, including for malformed input.

### slopsniffer.ingest.pickle

- `slopsniffer.ingest.pickle.x__state__mutmut_6` (survived): Equivalent: Python normalizes the codec names `utf-8` and `UTF-8` to the same codec, including for malformed input.
- `slopsniffer.ingest.pickle.x_source__mutmut_11` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.pickle.x_source__mutmut_13` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.pickle.x_source__mutmut_15` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.pickle.x_source__mutmut_23` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.pickle.x_source__mutmut_25` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.pickle.x_source__mutmut_27` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.pickle.x_source__mutmut_53` (survived): Equivalent: `logs` is constructed exclusively from three-element tuples, so every iterable passed to the transposing `zip` has the same length.
- `slopsniffer.ingest.pickle.x_source__mutmut_55` (survived): Equivalent: `logs` is constructed exclusively from three-element tuples, so every iterable passed to the transposing `zip` has the same length.
- `slopsniffer.ingest.pickle.x_source__mutmut_56` (survived): Equivalent: `logs` is constructed exclusively from three-element tuples, so every iterable passed to the transposing `zip` has the same length.
- `slopsniffer.ingest.pickle.x_source__mutmut_62` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.pickle.x_source__mutmut_63` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.pickle.xǁPickleParserǁparse_line__mutmut_15` (survived): Equivalent: after a JSON decoding failure both `None` and the empty string are non-dicts, so they produce the same text event with no parsed JSON or rows.
- `slopsniffer.ingest.pickle.xǁPickleParserǁparse_line__mutmut_9` (survived): Equivalent: Python normalizes the codec names `utf-8` and `UTF-8` to the same codec, including for malformed input.

### slopsniffer.ingest.reader

- `slopsniffer.ingest.reader.x_event_id__mutmut_7` (survived): Equivalent: Python normalizes the codec names `utf-8` and `UTF-8` to the same codec, including for malformed input.
- `slopsniffer.ingest.reader.x_read_batch__mutmut_23` (survived): Equivalent: at `offset == limit`, `readline(0)` returns the same empty bytes as the original branch, with no file-position change.
- `slopsniffer.ingest.reader.x_read_batch__mutmut_24` (survived): Equivalent: both empty bytes and `b'XXXX'` fail the newline check and set EOF before parsing. This sentinel's contents are never stored or returned.
- `slopsniffer.ingest.reader.x_read_batch__mutmut_3` (survived): Equivalent: the initial `Batch.end` is overwritten with `offset` on every successful return. Failure returns a separate new batch; the initial value is never observed.

### slopsniffer.ingest.skills

- `slopsniffer.ingest.skills.x__plugin_name__mutmut_1` (survived): Equivalent for calls from `inventory`: the plugin-path filter requires a nested `skills/<name>/SKILL.md` path. The skill directory's parent chain includes the root; both conditions are false below that root and true at the root, where both versions stop.
- `slopsniffer.ingest.skills.x__plugin_name__mutmut_10` (survived): Equivalent: Python normalizes the codec names `utf-8` and `UTF-8` to the same codec, including for malformed input.
- `slopsniffer.ingest.skills.x__skill__mutmut_9` (survived): Equivalent: Python normalizes the codec names `utf-8` and `UTF-8` to the same codec, including for malformed input.
- `slopsniffer.ingest.skills.x_source__mutmut_23` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.skills.x_source__mutmut_24` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.skills.x_source__mutmut_26` (survived): Equivalent: the inventory writer constructs a six-element tuple for every skill, so all iterables passed to `zip` have equal length.
- `slopsniffer.ingest.skills.x_source__mutmut_28` (survived): Equivalent: the inventory writer constructs a six-element tuple for every skill, so all iterables passed to `zip` have equal length.
- `slopsniffer.ingest.skills.x_source__mutmut_29` (survived): Equivalent: the inventory writer constructs a six-element tuple for every skill, so all iterables passed to `zip` have equal length.
- `slopsniffer.ingest.skills.x_source__mutmut_5` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.
- `slopsniffer.ingest.skills.x_source__mutmut_6` (survived): Equivalent: only SQL keyword or identifier case changes; DuckDB resolves these names case-insensitively and the statement has the same effect.

## slopsniffer.proposals (fresh mutation-rest run)

- `slopsniffer.proposals.writer.xǁWriterǁreset__mutmut_3` — Presentation-only: changes the unsafe-target diagnostic; the same files are refused before deletion.
- `slopsniffer.proposals.writer.xǁWriterǁreset__mutmut_7` — Presentation-only: changes the unsafe-target diagnostic; the same files are refused before deletion.
- `slopsniffer.proposals.drafter.x__usable__mutmut_15` — Equivalent: typing.cast returns its second argument unchanged at runtime.
- `slopsniffer.proposals.clusters.x_root_cause__mutmut_9` — Equivalent: the inserted uppercase X characters are discarded by the lowercase-only [a-z]+ tokenizer, leaving the same root cause.
- `slopsniffer.proposals.clusters.x__skills_by_name__mutmut_5` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.proposals.clusters.x__skills_by_name__mutmut_6` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.proposals.clusters.x_surfaces__mutmut_7` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.proposals.clusters.x_surfaces__mutmut_8` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.proposals.clusters.x_surfaces__mutmut_19` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.proposals.clusters.x_surfaces__mutmut_20` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.proposals.clusters.x_surfaces__mutmut_22` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.proposals.clusters.x_surfaces__mutmut_23` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.proposals.clusters.x_surfaces__mutmut_45` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.proposals.clusters.x_surfaces__mutmut_46` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.proposals.clusters.x_surfaces__mutmut_48` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.proposals.clusters.x_surfaces__mutmut_51` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.

## slopsniffer.signals (fresh mutation-rest run)

- `slopsniffer.signals.runner.x__sql__mutmut_3` — Equivalent: resources.files(None) infers this caller’s package, slopsniffer.signals, which is the explicit original anchor.
- `slopsniffer.signals.runner.x__sql__mutmut_5` — Equivalent: UTF-8 codec names are case-insensitive.
- `slopsniffer.signals.runner.x__rollup__mutmut_17` — Equivalent: every DuckDB row has exactly the cursor description’s number of columns, so disabling zip length validation cannot change a result.
- `slopsniffer.signals.runner.x__rollup__mutmut_20` — Equivalent: every DuckDB row has exactly the cursor description’s number of columns, so disabling zip length validation cannot change a result.
- `slopsniffer.signals.runner.x__rollup__mutmut_21` — Equivalent: every DuckDB row has exactly the cursor description’s number of columns, so disabling zip length validation cannot change a result.

## slopsniffer.outcomes (fresh mutation-rest run)


- `slopsniffer.outcomes.github.xǁGhǁ_run__mutmut_8` — Equivalent: subprocess.run treats check=None as false, and omitting check uses the same false default.
- `slopsniffer.outcomes.github.xǁGhǁ_run__mutmut_14` — Equivalent: subprocess.run treats check=None as false, and omitting check uses the same false default.
- `slopsniffer.outcomes.github.xǁGhǁgraphql__mutmut_18` — Equivalent: both None and the empty string are non-dicts, so invalid JSON reaches the same GhFailed branch with the same diagnostic.
- `slopsniffer.outcomes.github.x_pr_query__mutmut_5` — Equivalent on the repo contract owner/name: its single slash gives identical partition and rpartition results.
- `slopsniffer.outcomes.linkage.x_origin_repo__mutmut_8` — Equivalent: subprocess.run treats check=None as false, and omitting check uses the same false default.
- `slopsniffer.outcomes.linkage.x_origin_repo__mutmut_13` — Equivalent: subprocess.run treats check=None as false, and omitting check uses the same false default.

## slopsniffer.grading (fresh mutation-rest run)

- `slopsniffer.grading.grader.x_grade__mutmut_18` — Presentation-only: changes the prefix of a unique temporary grading workspace, which is still isolated and removed after the run.
- `slopsniffer.grading.grader.x_grade__mutmut_19` — Presentation-only: changes the prefix of a unique temporary grading workspace, which is still isolated and removed after the run.
- `slopsniffer.grading.grader.x_grade__mutmut_20` — Presentation-only: changes the prefix of a unique temporary grading workspace, which is still isolated and removed after the run.
- `slopsniffer.grading.grader.x__graded_rubrics__mutmut_7` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.grading.grader.x__record_call__mutmut_6` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.grading.grader.x__record_call__mutmut_7` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.grading.grader.x__record_call__mutmut_9` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.grading.grader.x__record_call__mutmut_11` — Equivalent: DuckDB SQL keywords and unquoted identifiers are case-insensitive; string values and bindings are unchanged.
- `slopsniffer.grading.backends.x_verdict_schema__mutmut_18` — Equivalent: typing.cast returns the schema unchanged regardless of its first, type-only argument.
- `slopsniffer.grading.backends.x_validate__mutmut_12` — Equivalent: typing.cast returns the candidate unchanged regardless of its first, type-only argument.
- `slopsniffer.grading.backends.x_validate__mutmut_23` — Presentation-only: changes the separator in the foreign-citation diagnostic; the same IDs are rejected and no verdict is returned.
- `slopsniffer.grading.backends.x__run__mutmut_11` — Equivalent: subprocess.run treats check=None as false, matching check=False.
- `slopsniffer.grading.backends.x__run__mutmut_18` — Equivalent: omitting check uses subprocess.run’s false default.
- `slopsniffer.grading.backends.x__text__mutmut_8` — Equivalent: UTF-8 codec names are case-insensitive.
- `slopsniffer.grading.backends.x__finish__mutmut_3` — Presentation-only: validating an absent answer changes its invalid-output diagnostic from no output to a schema error; outcome, verdict, usage and recorded output are unchanged.
- `slopsniffer.grading.backends.x__finish__mutmut_5` — Presentation-only: changes the no-output diagnostic; the invocation still has the same failure outcome and no verdict.
- `slopsniffer.grading.backends.x__finish__mutmut_6` — Presentation-only: capitalizes the no-output diagnostic; the invocation still has the same failure outcome and no verdict.
- `slopsniffer.grading.backends.x_describe__mutmut_5` — Presentation-only: includes one more trailing character of a long process-error diagnostic; process classification and all recorded facts are unchanged.
- `slopsniffer.grading.backends.x_exec_codex__mutmut_7` — Presentation-only: changes the temporary schema basename; the same full path is written, passed to the CLI and cleaned up.
- `slopsniffer.grading.backends.x_exec_codex__mutmut_8` — Presentation-only: capitalizes the temporary schema basename; the same full path is written, passed to the CLI and cleaned up.
- `slopsniffer.grading.backends.x_exec_codex__mutmut_13` — Presentation-only: changes the temporary answer basename; the same full path is passed to the CLI, read and cleaned up.
- `slopsniffer.grading.backends.x_exec_codex__mutmut_14` — Presentation-only: capitalizes the temporary answer basename; the same full path is passed to the CLI, read and cleaned up.
- `slopsniffer.grading.backends.x_exec_codex__mutmut_57` — Equivalent: UTF-8 codec names are case-insensitive.
- `slopsniffer.grading.backends.x_exec_codex__mutmut_58` — Equivalent: when the answer file is absent, both the empty fallback and XXXX fail JSON parsing and produce output=None; existing answer files are unchanged.
- `slopsniffer.grading.backends.x_exec_codex__mutmut_61` — Equivalent: parsing an empty or whitespace-only answer returns None, which is also the original skipped-parse result.
- `slopsniffer.grading.backends.x__unfence__mutmut_20` — Equivalent at the sole JSON consumer: without a newline, both the original empty fallback and XXXX are invalid JSON and yield no answer.
- `slopsniffer.grading.rubrics.x__read__mutmut_6` — Equivalent: resources.files(None) infers this caller’s package, slopsniffer.grading, matching the explicit anchor.
- `slopsniffer.grading.rubrics.x__read__mutmut_12` — Equivalent: UTF-8 codec names are case-insensitive.
- `slopsniffer.grading.rubrics.x_parse_rubric__mutmut_3` — Presentation-only: omits the rubric name from missing-frontmatter diagnostics; the same inputs still raise RubricError.
- `slopsniffer.grading.rubrics.x_parse_rubric__mutmut_8` — Presentation-only: omits the rubric name from frontmatter-validation diagnostics; accepted fields and rejected inputs are unchanged.
- `slopsniffer.grading.rubrics.x_parse_rubric__mutmut_16` — Presentation-only: removes the missing-field diagnostic; the same absent name/description still raises RubricError.
- `slopsniffer.grading.rubrics.x_parse_rubric__mutmut_19` — Presentation-only: removes the no-labels diagnostic; the same label-less input still raises RubricError.
- `slopsniffer.grading.rubrics.x_parse_rubric__mutmut_24` — Equivalent: UTF-8 codec names are case-insensitive, so the hash bytes are identical.
- `slopsniffer.grading.rubrics.x__split__mutmut_8` — Presentation-only: an immediately closed, empty frontmatter block can receive a different RubricError message; no accepted rubric changes.
- `slopsniffer.grading.rubrics.x__split__mutmut_9` — Presentation-only: removes the missing-frontmatter diagnostic; the same malformed blocks still raise RubricError.
- `slopsniffer.grading.rubrics.x__scalar__mutmut_5` — Presentation-only: removes the malformed quoted-value diagnostic; the same JSON decoding failures still raise RubricError.
- `slopsniffer.grading.rubrics.x__scalar__mutmut_6` — Equivalent: a valid JSON value starting with a double quote is always a string; malformed quoted JSON raises before this unreachable branch.
- `slopsniffer.grading.rubrics.x__scalar__mutmut_17` — Presentation-only: removes the unsupported-scalar diagnostic; the same forbidden prefixes and empty values still raise RubricError.
- `slopsniffer.grading.rubrics.x__parse_front__mutmut_3` — Equivalent: the initial None and False states are both falsy and the state is only read as a boolean.
- `slopsniffer.grading.rubrics.x__parse_front__mutmut_16` — Presentation-only: omits the rubric name from malformed label-start diagnostics; the parsed labels and validation decisions are unchanged.
- `slopsniffer.grading.rubrics.x__parse_front__mutmut_29` — Presentation-only: omits the rubric name from malformed label-continuation diagnostics; validation decisions and parsed labels are unchanged.
- `slopsniffer.grading.rubrics.x__parse_front__mutmut_42` — Equivalent: resetting the labels-state flag to None or False has the same falsy behavior; the flag is not returned.
- `slopsniffer.grading.rubrics.x__parse_front__mutmut_54` — Presentation-only: omits the rubric name from top-level scalar diagnostics; accepted values and rejected inputs are unchanged.
- `slopsniffer.grading.rubrics.x__parse_front__mutmut_58` — Presentation-only: removes the top-level field-type diagnostic; non-string fields still raise RubricError.
- `slopsniffer.grading.rubrics.x__parse_front__mutmut_60` — Presentation-only: removes the unsupported-line diagnostic; the same malformed lines still raise RubricError.
- `slopsniffer.grading.rubrics.x__parse_front__mutmut_62` — Presentation-only: omits the rubric name from label-validation diagnostics; label values and validation decisions are unchanged.
- `slopsniffer.grading.rubrics.x__assign__mutmut_6` — Presentation-only: removes the missing label-separator diagnostic; the same malformed assignment still raises RubricError.
- `slopsniffer.grading.rubrics.x__assign__mutmut_9` — Presentation-only: omits the rubric name from label scalar diagnostics; assigned values and rejected inputs are unchanged.
- `slopsniffer.grading.rubrics.x__label__mutmut_19` — Presentation-only: removes the label-key diagnostic; missing or extra label keys still raise RubricError.
- `slopsniffer.grading.rubrics.x__label__mutmut_23` — Presentation-only: removes the label-type diagnostic; invalid label types still raise RubricError.
- `slopsniffer.grading.selection.x_rank__mutmut_7` — Equivalent: DuckDB named-parameter bindings are case-insensitive; the bound values and query are unchanged.
- `slopsniffer.grading.selection.x_rank__mutmut_10` — Equivalent: DuckDB named-parameter bindings are case-insensitive; the bound values and query are unchanged.
- `slopsniffer.grading.render.x__pieces__mutmut_2` — Equivalent: at exactly the segment cap, re-splitting produces one segment with the same event ID, header and entire body; no continuation is emitted.


## History-bounded ingestion (schema v3)

- `slopsniffer.grading.grader.x__store_grade__mutmut_10` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.grading.grader.x__store_grade__mutmut_12` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.grading.grader.x__store_grade__mutmut_13` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.grading.grader.x__store_grade__mutmut_15` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.grading.grader.x__store_grade__mutmut_16` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.grading.grader.x__store_grade__mutmut_18` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.grading.grader.x__store_grade__mutmut_19` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.grading.grader.x__store_grade__mutmut_21` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.grading.grader.x__store_grade__mutmut_23` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.grading.grader.x__store_grade__mutmut_25` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.grading.grader.x__store_grade__mutmut_6` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.grading.grader.x__store_grade__mutmut_7` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.grading.grader.x__store_grade__mutmut_9` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.claude.x__call_rows__mutmut_137` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.claude.x__call_rows__mutmut_139` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.claude.x__call_rows__mutmut_141` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.claude.x__call_rows__mutmut_143` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.claude.x__call_rows__mutmut_149` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.claude.x__call_rows__mutmut_152` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.claude.x__call_rows__mutmut_66` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.claude.x__call_rows__mutmut_68` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.claude.x__call_rows__mutmut_70` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.claude.x__call_rows__mutmut_72` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.claude.x__call_rows__mutmut_76` — Equivalent: `ensure_ascii=None` is falsy, just like False, so JSON output is unchanged.
- `slopsniffer.ingest.claude.x__json_string__mutmut_4` — Equivalent: `ensure_ascii=None` is falsy, just like False, so JSON output is unchanged.
- `slopsniffer.ingest.engine.xǁ_Writerǁ_insert__mutmut_13` — Equivalent: batch errors are typed `(offset, message)` pairs, so every transposed row has equal length regardless of zip strictness.
- `slopsniffer.ingest.engine.xǁ_Writerǁ_insert__mutmut_15` — Equivalent: batch errors are typed `(offset, message)` pairs, so every transposed row has equal length regardless of zip strictness.
- `slopsniffer.ingest.engine.xǁ_Writerǁ_insert__mutmut_16` — Equivalent: batch errors are typed `(offset, message)` pairs, so every transposed row has equal length regardless of zip strictness.
- `slopsniffer.ingest.engine.xǁ_Writerǁ_insert__mutmut_28` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.engine.xǁ_Writerǁ_insert__mutmut_29` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.engine.xǁ_Writerǁ_insert__mutmut_31` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.
- `slopsniffer.ingest.engine.xǁ_Writerǁ_insert__mutmut_32` — Equivalent: only SQL keyword, function, table or column identifier case changes; DuckDB resolves these case-insensitively.

Changed-function run: 433 mutants, 349 killed, 84 equivalent survivors; no timeouts or untested mutants.
The focused selection covers writer/rollback/blob contracts, grading persistence, Claude field and tool-result semantics, and split-session sequencing.

Manual mutations also killed removal of within-batch blob DISTINCT, the stored-hash anti-join, grade and PR key matching, and the v3 migration rebuild. The v2 schema fails the 64 MB history regression and exact index membership test; the original JSON finalizer fails both 128 MB row/byte-batch regressions.

## Bounded finalization

Stale entries for changed functions were removed. The scoped run covers common helpers,
Codex/Droid fields/derivation/finalize, Claude's shared extraction wiring, and engine ingest
failure reporting. See [ingest-finalize-memory.md](ingest-finalize-memory.md) for resource
contracts and hand-mutation methodology.

Test-audit marks: **R** for the memory/batch-cap regression, isolated session failure/replay,
crash-after-body-commit replay, missing-total identity, interleaved raw identity isolation,
spool closure, and existing semantic
finalizer contracts. Each protects observable derived-row identity or the explicit memory/
publication contract. Production callers are the registered harness finalizers and engine
ingest. No tests or production seams were deleted. All hand mutations were restored by checksum.

Scoped result: **1,024 killed / 199 equivalent or presentation-only / 0 uncovered /
0 timeouts**, plus nine killed hand mutations. The 28 non-equivalent opening survivors
were rerun after adding negative/value, raw-null continuation, diagnostic-context, and
prior-error-count contracts; all 28 were killed. Mutation test selection was temporarily
`tests/ingest/`, with `--max-children 8`; `setup.cfg` was restored by SHA-256 verification.
The full repository suite is a separate release gate.

Context-manager variants are not generated by this mutmut version: `_session` is covered
by the early-publication and partial-cleanup hand mutations; `tool_tables` by removing
spool ownership/closure. Baselines were green before every hand mutation and production
files were restored by checksum.

- `slopsniffer.ingest.claude.x__derive__mutmut_10` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__derive__mutmut_11` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__fields__mutmut_12` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__fields__mutmut_6` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__fields__mutmut_8` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_10` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_12` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_13` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_15` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_16` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_18` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_19` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_21` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_22` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_3` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_4` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_6` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_7` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.claude.x__session_fields__mutmut_9` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_10` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_11` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_13` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_14` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_25` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_27` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_29` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_31` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_47` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_48` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_50` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_52` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_54` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_56` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__derive__mutmut_7` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__fields__mutmut_105` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__fields__mutmut_107` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__fields__mutmut_113` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__fields__mutmut_121` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__fields__mutmut_123` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__fields__mutmut_164` — JSON-typed tool input preserves the same value; only legal whitespace/Unicode escaping changes (or ensure_ascii=None equals False).
- `slopsniffer.ingest.codex.x__fields__mutmut_166` — JSON-typed tool input preserves the same value; only legal whitespace/Unicode escaping changes (or ensure_ascii=None equals False).
- `slopsniffer.ingest.codex.x__fields__mutmut_167` — JSON-typed tool input preserves the same value; only legal whitespace/Unicode escaping changes (or ensure_ascii=None equals False).
- `slopsniffer.ingest.codex.x__fields__mutmut_210` — Codex has no error flag; this optional always-None spool field is read with get(), so absent/renamed remains SQL NULL.
- `slopsniffer.ingest.codex.x__fields__mutmut_211` — Codex has no error flag; this optional always-None spool field is read with get(), so absent/renamed remains SQL NULL.
- `slopsniffer.ingest.codex.x__fields__mutmut_62` — Cumulative-total serialization is internal equality data only; these deterministic injective serializers preserve exactly the same equality relation.
- `slopsniffer.ingest.codex.x__fields__mutmut_63` — Cumulative-total serialization is internal equality data only; these deterministic injective serializers preserve exactly the same equality relation.
- `slopsniffer.ingest.codex.x__fields__mutmut_65` — Cumulative-total serialization is internal equality data only; these deterministic injective serializers preserve exactly the same equality relation.
- `slopsniffer.ingest.codex.x__fields__mutmut_66` — Cumulative-total serialization is internal equality data only; these deterministic injective serializers preserve exactly the same equality relation.
- `slopsniffer.ingest.codex.x__fields__mutmut_69` — Cumulative-total serialization is internal equality data only; these deterministic injective serializers preserve exactly the same equality relation.
- `slopsniffer.ingest.codex.x__fields__mutmut_70` — Cumulative-total serialization is internal equality data only; these deterministic injective serializers preserve exactly the same equality relation.
- `slopsniffer.ingest.codex.x__fields__mutmut_71` — Cumulative-total serialization is internal equality data only; these deterministic injective serializers preserve exactly the same equality relation.
- `slopsniffer.ingest.codex.x__fields__mutmut_81` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__fields__mutmut_83` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__fields__mutmut_87` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__fields__mutmut_93` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.codex.x__fields__mutmut_99` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__drop_working_tables__mutmut_13` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__drop_working_tables__mutmut_18` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__drop_working_tables__mutmut_2` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__drop_working_tables__mutmut_6` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__drop_working_tables__mutmut_8` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__isolated_session__mutmut_15` — Only the diagnostic logger namespace changes; the full message, traceback, recorded error, and affected paths are unchanged.
- `slopsniffer.ingest.common.x__isolated_session__mutmut_16` — Only XX decoration of the diagnostic message changes; harness, session, traceback, stored error, and paths are retained.
- `slopsniffer.ingest.common.x__isolated_session__mutmut_24` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__isolated_session__mutmut_25` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__isolated_session__mutmut_27` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__isolated_session__mutmut_28` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__isolated_session__mutmut_35` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__isolated_session__mutmut_36` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__json_events__mutmut_10` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__json_events__mutmut_11` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__json_events__mutmut_13` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__json_events__mutmut_14` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__json_events__mutmut_16` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__json_events__mutmut_17` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__json_events__mutmut_45` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__json_events__mutmut_46` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__json_events__mutmut_47` — Only Python transfer chunking changes; the SQL result is already bounded by the same event row/byte caps.
- `slopsniffer.ingest.common.x__json_events__mutmut_60` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__json_events__mutmut_62` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__json_events__mutmut_7` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x__json_events__mutmut_8` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_costs__mutmut_11` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_costs__mutmut_12` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_10` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_12` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_13` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_142` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_143` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_145` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_147` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_148` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_15` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_150` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_151` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_153` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_154` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_156` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_157` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_16` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_161` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_162` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_18` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_19` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_21` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_22` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_24` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_25` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_27` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_28` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_30` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_31` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_33` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_34` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_36` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_37` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_39` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_40` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_43` — The modified cursor still precedes all nonnegative (row_group, byte_group) pairs, so every batch is visited unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_44` — The modified cursor still precedes all nonnegative (row_group, byte_group) pairs, so every batch is visited unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_45` — The modified cursor still precedes all nonnegative (row_group, byte_group) pairs, so every batch is visited unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_51` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_52` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_54` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_55` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_6` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_68` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_69` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_71` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_72` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_86` — The Row carrier table label is unused by the direct bounded INSERT; only its values are consumed.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_9` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_90` — The Row carrier table label is unused by the direct bounded INSERT; only its values are consumed.
- `slopsniffer.ingest.common.x_insert_tools__mutmut_91` — The Row carrier table label is unused by the direct bounded INSERT; only its values are consumed.
- `slopsniffer.ingest.common.x_json_batches__mutmut_10` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_101` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_103` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_111` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_112` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_12` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_13` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_15` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_16` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_18` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_21` — The modified cursor still precedes all nonnegative (row_group, byte_group) pairs, so every batch is visited unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_22` — The modified cursor still precedes all nonnegative (row_group, byte_group) pairs, so every batch is visited unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_23` — The modified cursor still precedes all nonnegative (row_group, byte_group) pairs, so every batch is visited unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_29` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_30` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_32` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_33` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_47` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_48` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_6` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_67` — Only wording of the invalid-integration ValueError changes; all registered tool finalizers supply a spool, and the exception type/precondition is unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_68` — Only wording of the invalid-integration ValueError changes; all registered tool finalizers supply a spool, and the exception type/precondition is unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_69` — Only capitalization of the invalid-integration ValueError message changes; the precondition and exception type are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_7` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_73` — Spool JSON is internal and decoded before insertion; legal whitespace/Unicode escaping preserves values, and actual encoded lengths still bound each body batch.
- `slopsniffer.ingest.common.x_json_batches__mutmut_74` — Spool JSON is internal and decoded before insertion; legal whitespace/Unicode escaping preserves values, and actual encoded lengths still bound each body batch.
- `slopsniffer.ingest.common.x_json_batches__mutmut_76` — Spool JSON is internal and decoded before insertion; legal whitespace/Unicode escaping preserves values, and actual encoded lengths still bound each body batch.
- `slopsniffer.ingest.common.x_json_batches__mutmut_77` — Spool JSON is internal and decoded before insertion; legal whitespace/Unicode escaping preserves values, and actual encoded lengths still bound each body batch.
- `slopsniffer.ingest.common.x_json_batches__mutmut_79` — Spool JSON is internal and decoded before insertion; legal whitespace/Unicode escaping preserves values, and actual encoded lengths still bound each body batch.
- `slopsniffer.ingest.common.x_json_batches__mutmut_83` — Python resolves UTF-8 and utf-8 to the same codec; spool bytes and pointers are unchanged.
- `slopsniffer.ingest.common.x_json_batches__mutmut_9` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_json_string__mutmut_4` — ensure_ascii=None is falsy, exactly like False; output is unchanged.
- `slopsniffer.ingest.common.x_pending__mutmut_34` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_pending__mutmut_38` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_pending__mutmut_39` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_pending__mutmut_42` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_pending__mutmut_43` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_pending__mutmut_46` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.common.x_pending__mutmut_47` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.droid.x__derive__mutmut_34` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.droid.x__derive__mutmut_35` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.droid.x__derive__mutmut_7` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.droid.x__fields__mutmut_100` — JSON-typed tool input preserves the same value; only legal whitespace/Unicode escaping changes (or ensure_ascii=None equals False).
- `slopsniffer.ingest.droid.x__fields__mutmut_13` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.droid.x__fields__mutmut_15` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.droid.x__fields__mutmut_19` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.droid.x__fields__mutmut_93` — JSON-typed tool input preserves the same value; only legal whitespace/Unicode escaping changes (or ensure_ascii=None equals False).
- `slopsniffer.ingest.droid.x__fields__mutmut_94` — JSON-typed tool input preserves the same value; only legal whitespace/Unicode escaping changes (or ensure_ascii=None equals False).
- `slopsniffer.ingest.droid.x__fields__mutmut_96` — JSON-typed tool input preserves the same value; only legal whitespace/Unicode escaping changes (or ensure_ascii=None equals False).
- `slopsniffer.ingest.droid.x__fields__mutmut_97` — JSON-typed tool input preserves the same value; only legal whitespace/Unicode escaping changes (or ensure_ascii=None equals False).
- `slopsniffer.ingest.engine.x_ingest__mutmut_120` — Only XX decoration of the failure heading changes; harness, error message, traceback, affected paths, and counts remain intact.
- `slopsniffer.ingest.engine.x_ingest__mutmut_131` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.engine.x_ingest__mutmut_132` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.engine.x_ingest__mutmut_134` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.engine.x_ingest__mutmut_135` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.engine.x_ingest__mutmut_137` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.engine.x_ingest__mutmut_138` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.engine.x_ingest__mutmut_140` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.engine.x_ingest__mutmut_141` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.engine.x_ingest__mutmut_15` — Only the informational wording for an unregistered harness changes; that harness is still named and registered work and summary data are unchanged.
- `slopsniffer.ingest.engine.x_ingest__mutmut_17` — Only the informational wording for an unregistered harness changes; that harness is still named and registered work and summary data are unchanged.
- `slopsniffer.ingest.engine.x_ingest__mutmut_95` — SQL keywords/functions and table/column identifiers (including quoted metadata row columns) are case-insensitive in DuckDB; rows and values are unchanged.
- `slopsniffer.ingest.engine.xǁFinalizeFailuresǁ__init____mutmut_2` — Only the internal aggregate exception wording changes; engine reporting consumes paths, and the original per-file exception remains logged/stored.
- `slopsniffer.ingest.engine.xǁFinalizeFailuresǁ__init____mutmut_3` — Only the internal aggregate exception wording changes; engine reporting consumes paths, and the original per-file exception remains logged/stored.
- `slopsniffer.ingest.engine.xǁFinalizeFailuresǁ__init____mutmut_4` — Only the internal aggregate exception wording changes; engine reporting consumes paths, and the original per-file exception remains logged/stored.

## Post-ingest analysis memory

The final selected scope checked 1,928 generated variants: 1,666 killed and 262
reviewed exemptions below; no selected variant is untested or left timed out.
All runs used `--max-children 8`. Nineteen additional production-call-site/SQL
mutations were killed, covering decorated context managers, physical fences,
cleanup, Unicode byte accounting, eager serialization, every neighboring-window
partition, per-batch commits and a single history-wide transaction.

Earlier exploratory timeouts were rechecked with deadline-protected tests and
killed. Test selection was temporarily narrowed for expensive mutation runs and
restored byte-for-byte before the full suite. The accepted equivalences follow.

- `slopsniffer.analysis.x__batches__mutmut_10` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__batches__mutmut_11` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__batches__mutmut_13` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__batches__mutmut_14` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__body_predicate__mutmut_29` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__body_predicate__mutmut_31` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__body_predicate__mutmut_7` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__body_predicate__mutmut_8` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__with_inputs__mutmut_21` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__with_inputs__mutmut_31` — None and False take the same false branch.
- `slopsniffer.analysis.x__with_inputs__mutmut_32` — WITH RECURSIVE also accepts nonrecursive CTEs; actual recursive CTEs retain the keyword.
- `slopsniffer.analysis.x__with_inputs__mutmut_4` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__with_inputs__mutmut_51` — WITH RECURSIVE also accepts nonrecursive CTEs; actual recursive CTEs retain the keyword.
- `slopsniffer.analysis.x__with_inputs__mutmut_53` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__with_inputs__mutmut_55` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__with_inputs__mutmut_61` — Only diagnostic wording for unsupported SQL changes; the same ValueError is raised.
- `slopsniffer.analysis.x__with_inputs__mutmut_62` — Only diagnostic wording for unsupported SQL changes; the same ValueError is raised.
- `slopsniffer.analysis.x__with_inputs__mutmut_63` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x__with_inputs__mutmut_64` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_create_replacement__mutmut_10` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_create_replacement__mutmut_13` — Only the missing-target diagnostic changes; the same ValueError is raised before writing.
- `slopsniffer.analysis.x_create_replacement__mutmut_7` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_create_replacement__mutmut_8` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_batches__mutmut_10` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_batches__mutmut_11` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_batches__mutmut_17` — A nonempty selected page always enters its first loop iteration, which overwrites this initializer before use.
- `slopsniffer.analysis.x_event_batches__mutmut_18` — A nonempty selected page always enters its first loop iteration, which overwrites this initializer before use.
- `slopsniffer.analysis.x_event_batches__mutmut_20` — A nonempty selected page always enters its first loop iteration, which overwrites this initializer before use.
- `slopsniffer.analysis.x_event_batches__mutmut_7` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_batches__mutmut_8` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_scoped__mutmut_10` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_scoped__mutmut_12` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_scoped__mutmut_14` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_scoped__mutmut_40` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_scoped__mutmut_46` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_scoped__mutmut_47` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_scoped__mutmut_52` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_scoped__mutmut_53` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_event_scoped__mutmut_8` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_10` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_11` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_13` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_15` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_16` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_18` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_20` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_21` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_23` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_25` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_26` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_28` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_3` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_30` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_31` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_33` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_34` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_36` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_38` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_39` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_5` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_6` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.analysis.x_scoped__mutmut_8` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.grading.selection.x_select_worst__mutmut_11` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.grading.selection.x_select_worst__mutmut_13` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.grading.selection.x_select_worst__mutmut_15` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.grading.selection.x_select_worst__mutmut_17` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.grading.selection.x_select_worst__mutmut_19` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.grading.selection.x_select_worst__mutmut_21` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.grading.selection.x_select_worst__mutmut_24` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.grading.selection.x_select_worst__mutmut_26` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.grading.selection.x_select_worst__mutmut_4` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.grading.selection.x_select_worst__mutmut_7` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.grading.selection.x_select_worst__mutmut_9` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.github.x_refresh__mutmut_18` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.github.x_refresh__mutmut_19` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.github.xǁFetcherǁ__init____mutmut_5` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.github.xǁFetcherǁ__init____mutmut_6` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.github.xǁFetcherǁdetect_reverts__mutmut_26` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.github.xǁFetcherǁdetect_reverts__mutmut_27` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.github.xǁFetcherǁdetect_reverts__mutmut_36` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.github.xǁFetcherǁdetect_reverts__mutmut_37` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.github.xǁFetcherǁdetect_reverts__mutmut_38` — A two-row cursor chunk is still bounded; each candidate is decoded individually and the same running minimum is computed.
- `slopsniffer.outcomes.github.xǁFetcherǁdetect_reverts__mutmut_66` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.github.xǁFetcherǁdetect_reverts__mutmut_67` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.github.xǁFetcherǁfetch__mutmut_46` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.github.xǁFetcherǁfetch__mutmut_47` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_109` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_11` — importlib.resources infers the identical caller package when its argument is None.
- `slopsniffer.outcomes.linkage.x_link__mutmut_112` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_113` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_115` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_116` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_118` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_119` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_121` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_122` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_124` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_125` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_127` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_130` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_131` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_134` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_139` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_140` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_143` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_144` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_147` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_148` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_150` — count(*) always returns exactly one non-null scalar row, including on an empty table; the fallback is unreachable.
- `slopsniffer.outcomes.linkage.x_link__mutmut_153` — count(*) always returns exactly one non-null scalar row, including on an empty table; the fallback is unreachable.
- `slopsniffer.outcomes.linkage.x_link__mutmut_17` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_20` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_21` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_27` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_28` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_38` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_4` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_48` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_49` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_5` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_64` — The branch pass still selects an empty physical event interval (start exceeds end).
- `slopsniffer.outcomes.linkage.x_link__mutmut_65` — The branch pass additionally extracts one event; final deduplication merges its identical event-local links. Extra work remains one event.
- `slopsniffer.outcomes.linkage.x_link__mutmut_68` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_96` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.outcomes.linkage.x_link__mutmut_97` — Uppercasing the branch exclusions repeats narrow branch facts in event batches; final GROUP BY removes those identical facts, preserving output and batch bounds.
- `slopsniffer.outcomes.linkage.x_link__mutmut_99` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.x__failure_rows__mutmut_10` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.x__failure_rows__mutmut_11` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.x__failure_rows__mutmut_21` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.x__failure_rows__mutmut_24` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.x__failure_rows__mutmut_25` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.x__failure_rows__mutmut_27` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.x__failure_rows__mutmut_28` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.x__failure_rows__mutmut_42` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.x__failure_rows__mutmut_44` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.x__failure_rows__mutmut_46` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.x__failure_rows__mutmut_47` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.xǁ_StoredFailuresǁ__iter____mutmut_10` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.xǁ_StoredFailuresǁ__iter____mutmut_12` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.xǁ_StoredFailuresǁ__iter____mutmut_13` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.xǁ_StoredFailuresǁ__iter____mutmut_15` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.xǁ_StoredFailuresǁ__iter____mutmut_16` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.xǁ_StoredFailuresǁ__iter____mutmut_29` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.xǁ_StoredFailuresǁ__iter____mutmut_3` — Spool ordinals are constructed starting at zero; initial anchors -1 and -2 select exactly the same rows.
- `slopsniffer.proposals.clusters.xǁ_StoredFailuresǁ__iter____mutmut_31` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.xǁ_StoredFailuresǁ__iter____mutmut_33` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.clusters.xǁ_StoredFailuresǁ__iter____mutmut_9` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.writer.xǁWriterǁwrite__mutmut_45` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.writer.xǁWriterǁwrite__mutmut_51` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.writer.xǁWriterǁwrite__mutmut_59` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.writer.xǁWriterǁwrite__mutmut_8` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.x__draft__mutmut_6` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.x__draft__mutmut_8` — Every nonempty evidence batch assigns backend_reason before use; early returns do not consume the initializer.
- `slopsniffer.proposals.x__draft__mutmut_9` — Every nonempty evidence batch assigns backend_reason before use; early returns do not consume the initializer.
- `slopsniffer.proposals.x__insert__mutmut_10` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.x__insert__mutmut_18` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.x__insert__mutmut_6` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.x__insert__mutmut_7` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.x__insert__mutmut_9` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.x__propose__mutmut_22` — Only the unique temporary-directory prefix changes; persisted paths and proposal evidence are unchanged.
- `slopsniffer.proposals.x__propose__mutmut_23` — Only the unique temporary-directory prefix changes; persisted paths and proposal evidence are unchanged.
- `slopsniffer.proposals.x__propose__mutmut_24` — Only the unique temporary-directory prefix changes; no persisted path or output depends on it.
- `slopsniffer.proposals.x__store__mutmut_10` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.x__store__mutmut_33` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.x__store__mutmut_34` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.x__store__mutmut_37` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.x__store__mutmut_41` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.proposals.x__store__mutmut_42` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__proposals__mutmut_11` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__proposals__mutmut_12` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__proposals__mutmut_4` — Only two narrow metadata rows are prefetched; wide evidence still uses one physical-row lookup at a time.
- `slopsniffer.report.x__stored_leaderboard__mutmut_13` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_14` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_16` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_17` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_19` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_20` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_3` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_32` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_33` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_35` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_36` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_37` — Changing the narrow cursor chunk to two rows preserves severity order and Python summation.
- `slopsniffer.report.x__stored_leaderboard__mutmut_4` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_6` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.report.x__stored_leaderboard__mutmut_7` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_11` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_12` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_13` — A two-row cursor chunk stays within the already selected row/byte span; JSON trees are still decoded and released one at a time.
- `slopsniffer.signals.runner.x__event_flags__mutmut_37` — The JSON encoder treats None and False identically for ensure_ascii.
- `slopsniffer.signals.runner.x__event_flags__mutmut_44` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_45` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_47` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_48` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_50` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_51` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_57` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_58` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_60` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_61` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_63` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_64` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_66` — Each appended record is constructed with exactly six fields; zip strictness cannot change transposition.
- `slopsniffer.signals.runner.x__event_flags__mutmut_68` — Each appended record is constructed with exactly six fields; zip strictness cannot change transposition.
- `slopsniffer.signals.runner.x__event_flags__mutmut_69` — Each appended record is constructed with exactly six fields; zip strictness cannot change transposition.
- `slopsniffer.signals.runner.x__event_flags__mutmut_8` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__event_flags__mutmut_9` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__signal_rows__mutmut_10` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__signal_rows__mutmut_23` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__signal_rows__mutmut_28` — Both pagination key tuples are constructed with exactly three fields; zip strictness cannot change their mapping.
- `slopsniffer.signals.runner.x__signal_rows__mutmut_31` — Both pagination key tuples are constructed with exactly three fields; zip strictness cannot change their mapping.
- `slopsniffer.signals.runner.x__signal_rows__mutmut_38` — Both pagination key tuples are constructed with exactly three fields; zip strictness cannot change their mapping.
- `slopsniffer.signals.runner.x__signal_rows__mutmut_50` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__signal_rows__mutmut_59` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__signal_rows__mutmut_60` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__signal_rows__mutmut_9` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x__sql__mutmut_3` — importlib.resources infers the identical caller package when its argument is None.
- `slopsniffer.signals.runner.x__sql__mutmut_5` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_106` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_107` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_110` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_112` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_123` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_124` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_137` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_150` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_151` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_164` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_165` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_186` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_187` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_206` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_207` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_217` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_218` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_221` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_229` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_230` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_233` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_234` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_236` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_237` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_242` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_243` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_30` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_37` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_38` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_40` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_41` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_44` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_45` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_47` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_48` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_86` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_87` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_90` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_92` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_compute__mutmut_96` — Compaction SQL consumes only the current signal_event_flags table; physical start/end positions are unused for that reduced input.
- `slopsniffer.signals.runner.x_compute__mutmut_97` — Compaction SQL consumes only the current signal_event_flags table; physical start/end positions are unused for that reduced input.
- `slopsniffer.signals.runner.x_report__mutmut_19` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_report__mutmut_20` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.
- `slopsniffer.signals.runner.x_report__mutmut_22` — Case changes affect DuckDB keywords, identifiers or bound parameter names, case-insensitive source matching, or the UTF-8 codec alias; values and bounded selection are unchanged.

## Pricing, precision and report validation

A serial controlled
mutation run killed **66 of 66 selected production/SQL mutants**, with zero
survivors, errors or new behavioral exemptions. This is a selected behavioral
mutation pass, not a claim to have enumerated every possible automatic mutant.

The baseline command was:

```sh
uv run pytest tests/test_value_contracts.py tests/test_value_precision.py tests/cli/test_config.py tests/test_schema.py -q -x
```

The abandonment gate mutation additionally used its existing single-file skill
suite. Citation normalization, foreign-id rejection and raw-receipt preservation
used the existing fake-executable backend suite with a focused `-k` selection.
Each selected command had a green baseline before its mutations. The runner held
a shared heavy-run lock for the whole pass. Production
files were backed up, restored in `finally`, and checked by SHA-256. A private
bytecode-cache prefix prevents same-size edits/restorations from reusing stale
bytecode. The restored baseline passed.

Mutation checks exercise production call sites and identify the killing tests. The scoped independent read-only check
confirmed that the test-file moves retained assertions, negatives and fixture
registration. An intermediate role-text mutant survived because assistant turns
already establish a response by role; that redundant text branch was removed.
The final pass instead mutates the actual response-role predicate and kills it.
No behavioral survivor was reclassified as an exemption to clear this pass.

| Production boundary | Mutations killed |
| --- | --- |
| `src/slopsniffer/config.py` | `config_cache_write`, `config_legacy_rates` |
| `src/slopsniffer/grading/backends.py` | `grader_display_citation`, `grader_foreign_citation`, `grader_raw_receipt`, `grader_bracket_citation` |
| `src/slopsniffer/ingest/engine.py` | `ingest_reprices_history`, `title_ingest_wiring` |
| `src/slopsniffer/ingest/metadata.py` | `custom_title_priority`, `latest_title` |
| `src/slopsniffer/outcomes/linkage.py` | `direct_pr_gate`, `pr_dedupe` |
| `src/slopsniffer/pricing.py` | `cache_read_rate`, `cache_write_rate`, `output_rate`, `date_suffix`, `exact_override`, `partial_coverage`, `reported_cost`, `missing_write_rate`, `coverage_complete_method`, `unpriced_token_volume` |
| `src/slopsniffer/report/__init__.py` | `report_enrichment` |
| `src/slopsniffer/report/findings.py` | `pickle_explanation`, `pickle_metadata`, `excerpt_injection`, `resume_command`, `claude_subagent_parent`, `claude_subagent_transcript`, `native_subagent_resume`, `excerpt_actual_clause`, `excerpt_newline_boundary` |
| `src/slopsniffer/report/html.py` | `title_escape`, `explanation_escape`, `subagent_parent_visible`, `leaderboard_discloses_information` |
| `src/slopsniffer/signals/context_overflow.sql` | `compaction_own_work`, `compaction_is_information` |
| `src/slopsniffer/signals/edit_churn.sql` | `verification_kind`, `churn_failure_gate` |
| `src/slopsniffer/signals/never_fired_skill.sql` | `inventory_is_information` |
| `src/slopsniffer/signals/orphaned_session.sql` | `orphan_requires_silence` |
| `src/slopsniffer/signals/prelude.sql` | `parallel_call_identity` |
| `src/slopsniffer/signals/rerun_loop.sql` | `rerun_result_identity`, `rerun_informational`, `rerun_mutation_intent`, `rerun_noop`, `rerun_failed_check` |
| `src/slopsniffer/signals/rollups.sql` | `rollup_partial` |
| `src/slopsniffer/signals/runner.py` | `verification_position`, `structured_exit`, `top_excludes_information`, `provider_notice_wiring`, `retry_result_sequence_wiring` |
| `src/slopsniffer/signals/skill_abandoned.sql` | `skill_requires_error`, `skill_transient_failure` |
| `src/slopsniffer/signals/text.py` | `instructions_are_context` |
| `src/slopsniffer/signals/turn_inputs.sql` | `notification_gate`, `correction_clause`, `recovery_is_not_correction`, `provider_notice_filter` |
| `src/slopsniffer/signals/unchanged_retry.sql` | `retry_transient`, `retry_after_observed_failure` |
| `src/slopsniffer/signals/user_correction.sql` | `assistant_code_is_response` |
| `src/slopsniffer/status.py` | `status_override` |
| `src/slopsniffer/upgrade_v4.sql` | `title_schema_migration` |

The precision review and its correlated-sample limitations are recorded in
[signal-precision.md](signal-precision.md). Historical catalogs above belong to
their earlier snapshots; their generated mutmut ids are not asserted to identify
mutants after these source edits. This section owns the current value changes.
## Crush, record recovery, inventory and progress

**All 51 targeted hand mutations were killed**, with zero survivors, errors or
timeouts. The shared heavy-run lock serialized the whole campaign.
The unmutated selected baseline passed 51 tests. Each variant changed a production
site, ran its behavioral tests, restored a private saved source copy and verified
the restored SHA-256. Production replacements are verified by the behavioral contracts described below. This is targeted
hand mutation, not an exhaustive generated mutmut run.

The campaign covers SQLite WAL inclusion and snapshot deadlines; session and
child-row watermarks; replacement, exported child tables and orphan detection;
failed-export cleanup; timestamps, roles, authoritative costs, errors, summary
turns and mixed-model totals; usage cleanup and publication after body commits;
raw row/byte limits; relationship replay cleanup; MCP names/errors; web observation
deduplication; backfill invalidation and inherited replay exclusion; committed
recovery batches; workflow ownership; worktree branches; direct skill roots;
Crush registration and inventory ordering/reporting; progress stdout separation,
throttling, finalization counters and phase ETA, including unknown recovery totals and completed phases; shell/edit/unanswered-call signal
mappings; and grouping identical unused skill definitions.

Recovery tests protect against a defect where a session-wide web-call existence check
hid a different missing id-less observation. The repaired shipping finalizer is exercised with both an existing and a missing
observation, a completed call, a partial call, inherited replay and a revision-1
store. Mutations restoring the broad existence check, disabling the revision
upgrade, or removing recovery from the finalizer all fail that contract.

Intentional equivalences and non-mutated presentation/defense details:

- Source-file-id bounds are logically implied by the event-id join. Removing them
  changes scan cost rather than derived identity. Fresh same-clone A/B measurements
  compare complete identities, while existing interleaved/physical-span resource
  contracts and small-memory finalization cover correctness. No wall-time
  threshold was added to a unit test.
- A read-write SQLite open alone is observationally equivalent while all executed
  operations remain queries and backup. The defensive read-only mode and
  query-only pragma are retained. The WAL test independently kills immutable
  reads and proves a live writer can commit during private-snapshot export.
- Progress labels, rounding, error prose and alternate backup page-step sizes are
  presentation/defense choices. Output separation, throttling, deadlines, phase
  counters and ETA have behavioral contracts killed by the campaign.


## Integration validation

Integration validation uses serial mutation runners scoped to changed functions
and SQL. The source runner killed **12/12** behavioral mutations; the value runner,
expanded for report corrections, killed **41/41**. Every
selected baseline was green, production files were privately saved and restored
by SHA-256, and the value runner's restored baseline passed. The behavioral contracts below describe the integration coverage.

New discriminating contracts cover v2 row preservation; v3 title preservation and
legacy cost-CHECK widening; parent-title fallback and its subagent label; own-title
precedence; earliest usable user prompt after startup instructions; HTML severity
rounding while JSON preserves its raw value; and minute-resolution UTC generation
time. Crush fixtures retain a summary-only negative and exercise identical failed
checks versus successful verification, with progressive edits as the negative for
repair churn. Disabling the export's summary discriminator fails that negative.
Existing direct-finalizer contracts also kill removal of price installation.

One additional mutation changed pending Codex pricing from `codex_usage` to the
durable `usage` table and survived. It is **not counted as killed**. An independent read-only trace
confirmed output equivalence: finalization clears pending durable usage, copies
`codex_usage` into `usage` immediately before pricing, and `TOTALS` joins either
input to `lane_pending_events`. Failures/restarts repeat cleanup and derivation.
The mutation can broaden the physical scan; equivalent resource consumption is
not claimed. The shipped code retains the bounded staging-table input, and the
existing small-memory finalization regressions remain in the full suite.

## v0.2 skill and report usefulness

The serial targeted campaign killed **43/43 production mutations**, with no
survivors, invalid variants or timeouts. Its unmutated v0.2 and cache-relocation baseline passed 47
contracts. Each case cleared the affected Python bytecode cache, disabled bytecode
writes for its child test process, restored its source in `finally`, and verified
the original SHA-256. This is targeted hand mutation of the changed functions and
SQL, not an exhaustive generated mutmut run. Logs and the runner remain outside
the published tree.

| Changed boundary | Mutated behavior killed by contracts |
| --- | --- |
| `repositories.normalize`, `from_path` | Preserve `.git` suffix; discard remote identity; confuse common directory with worktree root. Synthetic SSH/HTTPS/scp URLs and real temporary Git worktrees distinguish these cases, including a subdirectory negative. |
| `repositories.install` | Stop path paging early; remove byte budget; increase row budget; advance past unconsumed metadata; reverse recorded/filesystem precedence; retain old raw-project rollup definitions. Complete identity membership, observed physical body requests and unchanged v4 database bytes cover the derivation. |
| `findings.subject` | Disable no-op grouping; discard the full long-command digest. Literal arguments, expansion/redirection negatives and same-prefix distinct long commands discriminate the normalized subjects. |
| `findings.grouped` | Admit informational rows; partition groups by session; remove kind interleaving; double-count session cost; return only one worst session; omit spool cleanup. Contracts include multiple epochs in one session, multiple sessions, competing groups, an informational negative and an unknown owner. |
| `report.build` | Disconnect grouped findings; omit repository refresh before a report without stored rollups. The public document and newly recorded metadata must retain the expected identities. |
| `status.collect`, `session_counts` | Omit the counts; count descendants as roots. Root, child, grandchild, cross-harness child and missing-parent cases must match both labelled rollup denominators. |
| `rollups.sql` | Replace root counts with tree totals; replace tree totals with root counts; count repeated working directories instead of distinct worktree roots. |
| `signals.compute`, `report`, `write_report` | Omit each entry point's repository refresh. Each is exercised after metadata arrives on an already open connection. |
| `db.apply_schema`, `open_read` | Omit initialization of the derived reader relations. Read-only v4 and current CLI consumers must still resolve projects. |
| CLI `_run_args`, `_run` | Remove the call-cap flag; discard the approved cap before grading. Parser examples and the fake grading boundary require the cap to arrive unchanged. |
| HTML `_finding` | Omit visible aggregate context. The rendered report must expose session/call counts and known affected-session cost, with navigation for the worst sessions. |
| Crush `_derive` | Prefer an older or higher-cost snapshot; publish sessions, usage or costs from all cached headers; omit latest-header view cleanup. Opposite registration/mtime order and decreasing totals require true snapshot recency. |
| Crush `finalize` | Omit session-only or cost-only duplicate repair. Completed v4 offsets must rederive exactly one logical actor and one authoritative total, while retaining raw JSON/identities and doing no work on the next clean replay. |
| `prelude.sql` | Erase newline command boundaries during normalization, turning an echo followed by a real command into an apparent literal no-op. |
| `rerun_loop.sql` | Restore the exclusion of numeric sleep; remove the idle-polling interpretation. Actual SQL signals distinguish literal waits from variable expansion, output redirection and real state-reading commands. |

The `_section` ranking prose and CSS-independent display wording are presentation
changes; changing a synonym without changing the selection policy is exempt.
The actual policy is checked by group membership/order and zero-severity negatives.
The fake backend fixture's startup allowance is test infrastructure only; timeout
classification still has deterministic backend-boundary coverage. No production
model timeout or DuckDB resource limit was raised.
