# Signal precision methodology

For each kind, select a small deterministic sample with
`ORDER BY hash(signal_id) LIMIT 15`, then read cited raw events and tool results.
Private case identities and transcript receipts are not distributed.

A true positive means avoidable repeated work, an actual user correction, or an
old incomplete session or deterministic terminal skill error worth opening. Ordinary progressive implementation, deliberate
mutation experiments, external-state polling, successful recovery, installed inventory
and routine compaction are not automatically waste. Ambiguous cases are conservatively
false positives, not assumed successes. This is a small, single-reviewer descriptive
sample, not a population estimate or an independent held-out evaluation. Repeated forked correction sessions replay the same human request; they are not
independent users.

Baseline precision by kind:

| Kind | Reviewed | True positives | False positives | Before precision |
| --- | ---: | ---: | ---: | ---: |
| edit_churn | 15 | 0 | 15 | 0.0% |
| context_overflow | 15 | 0 | 15 | 0.0% |
| rerun_loop | 15 | 5 | 10 | 33.3% |
| never_fired_skill | 15 | 0 | 15 | 0.0% |
| unchanged_retry | 15 | 10 | 5 | 66.7% |
| user_correction | 15 | 3 | 12 | 20.0% |
| orphaned_session | 15 | 15 | 0 | 100.0% |
| skill_abandoned | 15 | 0 | 15 | 0.0% |

After precision uses the same adjudicated sample and reports the retained
true and false positives. An empty denominator is N/A, never 100% precision. Reduced
recall is an intentional tradeoff and is not represented as an improvement in precision.

Baseline cases were compared with final signals using kind/session and the file, command or skill
subject. Reruns also require overlapping cited events; retries, skill errors and
orphans require the original first event; corrections require the original user
turn. Informational compaction/inventory cases match by subject. “After” below
means retained actionable alerts in that same adjudicated sample:

| Kind | Before TP / FP | Before precision | After TP / FP | After precision | Baseline TP no longer alerted |
| --- | ---: | ---: | ---: | ---: | ---: |
| edit_churn | 0 / 15 | 0.0% | 0 / 0 | N/A | 0 |
| context_overflow | 0 / 15 | 0.0% | 0 / 0 | N/A | 0 |
| rerun_loop | 5 / 10 | 33.3% | 5 / 0 | 100.0% | 0 |
| never_fired_skill | 0 / 15 | 0.0% | 0 / 0 | N/A | 0 |
| unchanged_retry | 10 / 5 | 66.7% | 5 / 0 | 100.0% | 5 |
| user_correction | 3 / 12 | 20.0% | 3 / 0 | 100.0% | 0 |
| orphaned_session | 15 / 0 | 100.0% | 15 / 0 | 100.0% | 0 |
| skill_abandoned | 0 / 15 | 0.0% | 0 / 0 | N/A | 0 |

The five lost baseline retry positives are three search timeouts, a refused
background polling command, and an artifact publish that subsequently succeeded.
The detector intentionally demands a repeated identical nontransient failure.
These exclusions reduce recall; they are not counted as corrected false positives.

A fresh sample of actionable signals produced these aggregate review results.
Kinds without an actionable sample have no precision denominator:

| Kind | Fresh TP / FP | Fresh precision |
| --- | ---: | ---: |
| edit_churn | 0 / 0 | N/A |
| context_overflow | 0 / 0 | N/A |
| rerun_loop | 15 / 0 | 100.0% |
| never_fired_skill | 0 / 0 | N/A |
| unchanged_retry | 15 / 0 | 100.0% |
| user_correction | 11 / 4 | 73.3% |
| orphaned_session | 15 / 0 | 100.0% |
| skill_abandoned | 2 / 0 | 100.0% |

These descriptive samples are small and correlated; they are not a held-out
population estimate. Earlier samples informed the ordinary-work negatives.
Inventory changes are not attributed to detector precision.

Residual correction false positives include pasted refutation text containing
“you didn't”, operational recovery followed by “try again”, and permission or
sandbox-setting changes. Literal phrases cannot establish who caused an error or
whether a request is feedback versus operations. Structural exclusions improve
precision without establishing semantic certainty for every phrase match.

The encoded distinctions are:

- **edit_churn:** require repeated identical, nontransient failing verification between
  edits to the file. Exclude explicit mutation commands. Five successful progressive edits
  no longer qualify. The configured edit minimum is unchanged.
- **rerun_loop:** compare result hashes within an edit epoch. Changing results imply
  progress. Exclude commands waiting on external processes, jobs, logs and checks.
  Only identical deterministic verifier failures or literal output/exit builtins
  remain actionable; successful reinspection and explicit mutation work remain
  information. Repeated `echo idle`/`echo yield` calls still qualify.
- **unchanged_retry:** require identical input and repeated identical failures, excluding
  transient failures and external-state waits. A retry that succeeds does not qualify. The next call must be issued after the
  first result returns; identical failures in one parallel burst are not reactive retries.
- **user_correction:** exclude machine-generated teammate/task notifications and network
  notices before phrase matching. Strip injected instructions, quoted text and code
  from user feedback while retaining code-only assistant responses. Require a direct
  clause for “that is wrong” rather than a relative clause describing review scope.
  A workspace cleanup, reconnection or restart followed by “try again” is recovery.
  Keep the actual user's excerpt in the report, preserving clause boundaries.
- **skill_abandoned:** require an observed error near the end. Identified transient
  tool failures are excluded: an external timeout may leave a delegated job running.
  Native error records remain evidence; they do not have the tool-result transient
  classification. Skill guidance, a handoff or a successful response may legitimately
  finish without another tool call.
- **context_overflow:** exclude startup/inherited compaction and deduplicate boundary/
  summary records between intervals of tool work. Compaction is retained as information
  with zero severity, since even a real compaction does not establish avoidable waste.
- **never_fired_skill:** inventory alone establishes neither applicability nor paid
  exposure. Retain it as information with zero severity; exclude it from top findings.
- **orphaned_session:** require silence beyond `orphan_hours` for an unanswered terminal
  tool call. This is an interruption clue, not proof that all session spend was wasted.

Result bodies are reduced in row/byte-bounded batches to hashes and failure flags before
session SQL. No history-sized list of transcript bodies is retained. Tests include
ordinary-work negatives, distinct cache rates, mixed known/unknown model usage and a
32 MB repricing regression. The existing 64 MB analysis regression remains a release gate.
