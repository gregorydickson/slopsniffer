# Bounded Codex and Droid finalization

Whole-session JSON extraction, windows over bodies and correlated result expansion
can exceed the memory pool even for a single large session. Source bytes and event
counts alone do not bound native JSON expansion. Finalization therefore bounds
body consumption by the same row/byte budgets as ingestion, while retaining
narrow metadata for session-wide pairing.

## Implementation

- Share the row/byte batching mechanism with Claude in `common.py`. Session windows keep
  narrow metadata, never raw JSON. Resolve physical positions afresh for each raw batch;
  row-id ranges avoid reading a whole JSON vector before an event-id join filters it.
- Decode one event at a time in Python. Codex/Droid extraction stores scalar metadata and
  tool identities. Preserve missing cumulative totals versus explicit JSON `null`.
- Spool tool bodies to an anonymous temporary file. Pair first uses/results by narrow ids
  and file offsets, then read and insert bounded body batches. This avoids both retained
  session-sized bodies and repeated decompression for scattered tool events.
- Commit body batches like the ingest writer. Publish the session row, costs, and durable
  sequence markers last. A crash before publication leaves the session pending; retry
  replaces partial output. A caught DuckDB session error removes partial derived output, records
  an `ingest_errors` row for each affected file, and continues the queue. The engine reports
  only those files and still returns failure.
- Temporary spools close on success/error and disappear on process exit. No ART indexes
  or history-sized Python body collections were introduced.

## Regression evidence

The fixed tests compare complete derived-row membership against a 512MB derivation at
64MB, exercise row and byte caps independently, and include a larger raw-vector case.
They observe the actual requested raw batch sizes as well as successful derivation:
DuckDB can sometimes stream an oversized request without failing, which alone would not
prove compliance with the writer's batch contract.

Recovery tests cover errors entering and exiting a session, successful sessions before
and after the failure, exactly the failed file/error membership, and identical replay.
Real child-process termination immediately after a body commit proves an unpublished
session is retried with no duplicate calls.

Hand mutations (baseline green, production call sites changed, SHA-256 restoration):

- Remove raw row/byte caps: killed by the row/byte resource assertions.
- Remove body-batch commits: killed by the real crash/replay tests.
- Re-raise a session error instead of continuing: killed by isolated-failure tests.
- Publish Codex or Droid sessions before body batches: both killed by crash tests.
- Conflate missing totals with JSON null: killed by the cumulative-usage identity test.
- Publish durable sequence values before body commits: killed by crash/replay tests.
- Omit partial tool-call cleanup: killed by the failed-session call membership assertion.
- Omit spool ownership/closure: killed by the real file object's closed-state assertion.

The last three explicitly mutate the changed context-manager functions: this mutmut
version does not generate their variants.

Mutmut results and accepted equivalents are recorded in [mutation-exemptions.md](mutation-exemptions.md).

## Low-limit storage boundary

A synthetic fixture with many wide results can reach DuckDB's autocheckpoint
floor at 64MB after a durable commit. The ingest writer can encounter the same
floor while seeding such a fixture. This is separate from JSON expansion; no
checkpoint/compression settings or memory limits are tuned to mask it. Required
64MB regressions exercise large raw history and derived bodies below that floor.
