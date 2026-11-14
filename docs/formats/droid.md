# Droid session format

This reference describes the transcript and sidecar structure consumed by
`src/slopsniffer/ingest/droid.py`; it is not an upstream Factory specification.

## Layout

```
~/.factory/sessions/
  <project-dir>/                       # the session's cwd with "/" replaced by "-"
    <session-id>.jsonl                 # the transcript, one JSON object per line
    <session-id>.settings.json         # sidecar: model, settings, token totals
    <session-id>.settings.json.bak     # previous sidecar (ignored)
    <session-id>.settings.json.lock/   # lock directory while Droid writes (ignored)
```

`<session-id>` is a UUID. There are no nested transcript files: every `*.jsonl` sits
directly in a project directory. A session is exactly one transcript file.

## Transcript records

Every line is an object with a `type`. The first line is always `session_start`, and its
`id` equals the file name's stem. Every record except `session_start` has an ISO-8601
`timestamp`.

### `session_start`

Session metadata.

| Field | Meaning |
|---|---|
| `id` | session id (the file stem) |
| `title`, `owner`, `version`, `hostId`, `organizationId` | descriptive metadata |
| `cwd` | working directory at start (`lastCwd` when it later changed) |
| `callingSessionId`, `callingToolUseId` | set on a subagent: the session and tool call that spawned it |
| `parent`, `forkedAtMessageId` | set on a fork: the session it was forked from and the message it was forked at |

The parser takes `callingSessionId` as the parent, else `parent`; a session with neither is
a root.

### `message`

One conversation turn: `{id, parentId, timestamp, seq, message}`. `seq` is Droid's own
message counter. `message` holds:

| Field | Meaning |
|---|---|
| `role` | `user` or `assistant` |
| `content` | list of blocks (below) |
| `modelId`, `apiProvider`, `reasoningEffort` | on assistant messages: the model that produced it |
| `visibility` | `llm_only` / `user_only` on some messages |

Content blocks:

| Block `type` | Fields |
|---|---|
| `text` | `text` |
| `thinking` | `thinking` (often empty), `signature`, `signatureProvider`, `durationMs` |
| `tool_use` | `id`, `name`, `input` (object) — assistant messages only |
| `tool_result` | `tool_use_id`, `content` (string or list of `text` blocks), `is_error` (present on most, absent means not an error) — user messages only |

A `tool_use` pairs with the `tool_result` whose `tool_use_id` equals its `id`.

### `todo_state`

The todo list at a point in the session: `{id, timestamp, todos, messageIndex}`.

### `agent_turn_outcome`

How an agent turn ended: `{turnId, reason, resultKind}`. Observed `reason` values include
`completed`, `cancelled`, `error`, `permission_rejected`, `no_approver_available` and
several `model_*` failures.

### `compaction_state`

A context compaction: `{id, timestamp, summaryText, summaryTokens, summaryKind,
anchorMessage, removedCount, systemInfo, systemInfoText, retainedSkills, retainedImages}`.

### `session_end`

Written when the session exits cleanly (most sessions have none): `{timestamp, durationMs,
toolCount, finalText}`.

Any other `type` is stored as an event with kind `unknown`.

## Sidecar `<session-id>.settings.json`

A single JSON object, rewritten as the session runs. Fields the parser reads:

| Field | Meaning |
|---|---|
| `model` | the session's model id |
| `tokenUsage` | the session's own totals: `inputTokens`, `outputTokens`, `cacheCreationTokens`, `cacheReadTokens`, `thinkingTokens`, `factoryCredits` |

`inclusiveTokenUsage` and `childInclusiveTokenUsageBySessionId` add the subagents' tokens;
the parser does not read them, because subagent cost rolls up to the parent through the
session edges and would otherwise count twice. `thinkingTokens` is not added to output.

The transcript carries no per-message usage, so the session's one `usage` row is attached
to its `session_start` event, and cost is `tokenUsage` × the config price table (NULL with
method `unpriced` when the model is not in the table).
