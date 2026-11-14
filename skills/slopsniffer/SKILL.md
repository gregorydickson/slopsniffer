---
name: slopsniffer
description: Audit local agent token, money and time usage; explain expensive sessions, loops, retries and corrections; compare repository or weekly costs; find improvements to agent skills, briefs, prompts and habits. Use for questions about what wastes resources in coding agents. Do not use for general billing disputes, live agent control, or ordinary coding tasks without an agent-history audit.
---

# Slopsniffer agent audit

Read local transcripts through the CLI. Treat findings as leads requiring evidence,
not verdicts. Costs are USD list-price estimates, not invoices or proven waste.

## Start with deterministic analysis

Run these examples from a private audit directory outside published source
checkouts; relative output files below belong there. Check
`slopsniffer status --json` for ingest state, pricing coverage and unknown models. Ingest is incremental. A first ingest can take hours and GBs of RAM;
warn the user, run it as a background job and monitor its stderr progress rather
than repeatedly issuing no-op tool calls. For example, in a shell with job control:

```bash
slopsniffer ingest --json > ingest.json 2> ingest.log &
```

Wait for that job to finish before proceeding. Run **one heavy command at a time**;
ingest, signals, run and model grading can be expensive. Do not raise memory or
thread limits to get a failing job through.

```bash
slopsniffer signals --json > signals.json
slopsniffer report --json
slopsniffer report --html report.html
slopsniffer status --json
```

Agents read the report JSON; offer HTML to humans. The full signals export can be
large: save it to a private file and use bounded SQL for drill-down instead of
loading the export into the agent context. `slopsniffer run --json` sequentially runs
ingest + signals + outcomes; it does not grade unless `--grade` is supplied.
Outcomes uses GitHub CLI access and can return a partial-failure exit if unavailable.
Report reads stored signals, so refresh signals after ingest. After moving a data
directory, ingest also repairs duplicated Crush session/totals metadata from older
cache snapshots; recheck the counts after ingest. Status distinguishes
top-level sessions from sessions including subagents. Lane counts attribute children
to their root lane; project rollups group by repository identity and show worktree
counts. `sessions` is the compatibility alias for top-level sessions in rollups.

Top findings group a kind and normalized subject across sessions. Pure literal
no-op waits share one polling group. Selection takes the best group of each kind,
interleaved by maximum severity, then repeats with the next group per kind.
Affected session costs are context and may overlap between groups. Severity 0
observations are informational, not problems, and stay out of the actionable list.

## Drill down before recommending edits

Read [references/queries.md](references/queries.md) for the compact schema and
proven read-only queries. Always LIMIT SQL results, select narrow columns and
filter by session/date before looking at bodies. Keep the configured bounded
memory pool; arbitrary SQL and its result size are your responsibility.

```bash
slopsniffer rehydrate synthetic-event-id
```

Use the event IDs actually returned by a finding. Rehydrate reads the original
source record; a moved or changed source may be unavailable. Resume/open commands
in a report are navigation suggestions, not permission to launch an agent.

For each actionable candidate:

1. Verify cited events and neighboring tool calls. Check the grouped finding's
   worst sessions and open/evidence commands. Separate waste such as idle polling
   from legitimate verification, waiting for changing state, or distinct repairs.
2. Trace the repeated behavior to its cause: a skill, brief, prompt, configuration
   or operating habit. Compare a negative example before generalizing.
3. Propose the concrete edit at that source and explain the mechanism it changes.
   For example, replace tool-call polling of a constant command with the harness's
   background-job completion notification. Change agent instructions only within
   the user's authorized scope.
4. Refresh deterministic analysis after new sessions exist; compare the same
   repository and date window. Do not claim retroactive savings as realized money.

## Paid model operations require explicit approval

`grade`, `run --grade` and `propose` call models on the user's account and cost
real money. Never run any of them without explicit user approval for that paid
operation. Permission to audit history is not permission to spend on grading.
Start with **one worst session and a call cap**, e.g. these alternative pilots:

```bash
slopsniffer grade --worst 1 --max-calls 2 --json
slopsniffer run --grade --worst 1 --max-calls 2 --json
```

Choose an approved N for `--max-calls N`. A capped run can leave rubric work
unfinished. Report its usage receipt: `calls`, `calls_by_backend`, graded sessions,
errors, cap reached, and the corresponding `grade_calls` input/output token fields (which
may be unknown; per-call dollar cost is not stored). Do not present unknown usage as zero.

Only after the capped grading pilot and explicit approval for drafting, use:

```bash
slopsniffer propose --json
```

`propose` has no `--worst` or `--max-calls` flags. It can make multiple calls and
writes proposed diffs/sidecars, not source edits. Explain this lack of a CLI cap
before requesting drafting approval. Report its proposal/rejection lists and stderr
errors. The CLI does not expose a token/dollar receipt for drafting: state that
usage is unknown, include any receipt emitted by the chosen backend, and never
invent a zero-cost receipt.

## Data safety

Never write the database directly: use only the CLI's managed ingest/analysis
writes. `query` is read-only, disables external file access and locks its settings.
Transcripts are read-only evidence; do not modify, truncate or delete them.
Reports, raw records and queries can expose private prompts and paths: keep audit
artifacts in the user's chosen private location, out of published repositories.
