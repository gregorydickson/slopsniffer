-- Remove history-sized ART indexes without losing rows or column contracts.
CREATE TABLE source_files_v3 AS SELECT * FROM source_files;
DROP TABLE source_files;
CREATE TABLE source_files (
    source_file_id   BIGINT  NOT NULL DEFAULT nextval('source_file_id_seq'),
    path             VARCHAR NOT NULL,
    harness          VARCHAR NOT NULL,
    size             BIGINT,
    mtime_ns         BIGINT,
    head_sha256      VARCHAR,
    committed_offset BIGINT  NOT NULL DEFAULT 0,
    status           VARCHAR NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'resumable', 'complete', 'reingest', 'failed', 'missing')),
    last_ingested_at TIMESTAMPTZ
);
INSERT INTO source_files SELECT * FROM source_files_v3;
DROP TABLE source_files_v3;

CREATE TABLE sessions_v3 AS SELECT * FROM sessions;
DROP TABLE sessions;
CREATE TABLE sessions (
    session_id        VARCHAR NOT NULL,
    harness           VARCHAR NOT NULL,
    parent_session_id VARCHAR,
    project           VARCHAR,
    cwd               VARCHAR,
    git_branch        VARCHAR,
    model             VARCHAR,
    started_at        TIMESTAMPTZ,
    ended_at          TIMESTAMPTZ,
    source_file_id    BIGINT
);
INSERT INTO sessions BY NAME SELECT * FROM sessions_v3;
DROP TABLE sessions_v3;

CREATE TABLE events_v3 AS SELECT * FROM events;
DROP TABLE events;
CREATE TABLE events (
    event_id       VARCHAR NOT NULL,
    session_id     VARCHAR,
    seq            BIGINT,
    record_type    VARCHAR NOT NULL,
    kind           VARCHAR NOT NULL,
    ts             TIMESTAMPTZ,
    source_file_id BIGINT  NOT NULL,
    byte_offset    BIGINT  NOT NULL,
    byte_length    BIGINT  NOT NULL,
    parsed         JSON
);
INSERT INTO events SELECT * FROM events_v3;
DROP TABLE events_v3;

CREATE TABLE messages_v3 AS SELECT * FROM messages;
DROP TABLE messages;
CREATE TABLE messages (
    event_id VARCHAR NOT NULL,
    role     VARCHAR NOT NULL,
    text     VARCHAR
);
INSERT INTO messages SELECT * FROM messages_v3;
DROP TABLE messages_v3;

CREATE TABLE tool_calls_v3 AS SELECT * FROM tool_calls;
DROP TABLE tool_calls;
CREATE TABLE tool_calls (
    call_id         VARCHAR NOT NULL,
    session_id      VARCHAR NOT NULL,
    use_event_id    VARCHAR NOT NULL,
    result_event_id VARCHAR,
    tool_name       VARCHAR NOT NULL,
    input           JSON,
    result_text     VARCHAR,
    is_error        BOOLEAN,
    duration_ms     BIGINT
);
INSERT INTO tool_calls SELECT * FROM tool_calls_v3;
DROP TABLE tool_calls_v3;

CREATE TABLE skill_invocations_v3 AS SELECT * FROM skill_invocations;
DROP TABLE skill_invocations;
CREATE TABLE skill_invocations (
    event_id   VARCHAR NOT NULL,
    session_id VARCHAR NOT NULL,
    skill_name VARCHAR NOT NULL,
    via        VARCHAR NOT NULL CHECK (via IN ('skill_tool', 'slash_command')),
    args       VARCHAR
);
INSERT INTO skill_invocations SELECT * FROM skill_invocations_v3;
DROP TABLE skill_invocations_v3;

CREATE TABLE blobs_v3 AS SELECT * FROM blobs;
DROP TABLE blobs;
CREATE TABLE blobs (
    content_hash VARCHAR NOT NULL,
    content      VARCHAR NOT NULL
);
INSERT INTO blobs SELECT * FROM blobs_v3;
DROP TABLE blobs_v3;

CREATE TABLE context_blocks_v3 AS SELECT * FROM context_blocks;
DROP TABLE context_blocks;
CREATE TABLE context_blocks (
    event_id     VARCHAR NOT NULL,
    ordinal      INTEGER NOT NULL,
    kind         VARCHAR NOT NULL
        CHECK (kind IN ('claude_md', 'skill_listing', 'memory_index', 'hook_output', 'other')),
    source_path  VARCHAR,
    content_hash VARCHAR NOT NULL
);
INSERT INTO context_blocks SELECT * FROM context_blocks_v3;
DROP TABLE context_blocks_v3;

CREATE TABLE usage_v3 AS SELECT * FROM usage;
DROP TABLE usage;
CREATE TABLE usage (
    event_id      VARCHAR NOT NULL,
    input_tokens  BIGINT,
    output_tokens BIGINT,
    cache_read    BIGINT,
    cache_write   BIGINT,
    model         VARCHAR
);
INSERT INTO usage SELECT * FROM usage_v3;
DROP TABLE usage_v3;

CREATE TABLE session_cost_v3 AS SELECT * FROM session_cost;
DROP TABLE session_cost;
CREATE TABLE session_cost (
    session_id VARCHAR NOT NULL,
    cost_usd   DOUBLE,
    method     VARCHAR NOT NULL CHECK (method IN ('cost_state', 'price_table', 'unpriced'))
);
INSERT INTO session_cost SELECT * FROM session_cost_v3;
DROP TABLE session_cost_v3;

CREATE TABLE pickle_runs_v3 AS SELECT * FROM pickle_runs;
DROP TABLE pickle_runs;
CREATE TABLE pickle_runs (
    pickle_run_id     VARCHAR NOT NULL,
    session_dir       VARCHAR NOT NULL,
    active            BOOLEAN,
    step              VARCHAR,
    iteration         INTEGER,
    max_iterations    INTEGER,
    current_ticket    VARCHAR,
    start_time_epoch  BIGINT,
    working_dir       VARCHAR,
    last_log_write_at TIMESTAMPTZ,
    source_file_id    BIGINT
);
INSERT INTO pickle_runs SELECT * FROM pickle_runs_v3;
DROP TABLE pickle_runs_v3;

CREATE TABLE pickle_iterations_v3 AS SELECT * FROM pickle_iterations;
DROP TABLE pickle_iterations;
CREATE TABLE pickle_iterations (
    pickle_run_id  VARCHAR NOT NULL,
    iteration      INTEGER NOT NULL,
    source_file_id BIGINT,
    started_at     TIMESTAMPTZ,
    ended_at       TIMESTAMPTZ
);
INSERT INTO pickle_iterations SELECT * FROM pickle_iterations_v3;
DROP TABLE pickle_iterations_v3;

CREATE TABLE signals_v3 AS SELECT * FROM signals;
DROP TABLE signals;
CREATE TABLE signals (
    signal_id          VARCHAR NOT NULL,
    kind               VARCHAR NOT NULL,
    session_id         VARCHAR NOT NULL,
    severity           DOUBLE  NOT NULL,
    evidence_event_ids VARCHAR[] NOT NULL,
    details            JSON,
    computed_at        TIMESTAMPTZ NOT NULL
);
INSERT INTO signals SELECT * FROM signals_v3;
DROP TABLE signals_v3;

CREATE TABLE pr_outcomes_v3 AS SELECT * FROM pr_outcomes;
DROP TABLE pr_outcomes;
CREATE TABLE pr_outcomes (
    repo              VARCHAR NOT NULL,
    pr_number         INTEGER NOT NULL,
    title             VARCHAR,
    state             VARCHAR,
    merged_at         TIMESTAMPTZ,
    review_decision   VARCHAR,
    reviews           JSON,
    ci_rollup_at_head VARCHAR,
    head_sha          VARCHAR,
    reverted_by_pr    INTEGER,
    fetched_at        TIMESTAMPTZ NOT NULL
);
INSERT INTO pr_outcomes SELECT * FROM pr_outcomes_v3;
DROP TABLE pr_outcomes_v3;

CREATE TABLE grades_v3 AS SELECT * FROM grades;
DROP TABLE grades;
CREATE TABLE grades (
    session_id      VARCHAR NOT NULL,
    rubric          VARCHAR NOT NULL,
    rubric_hash     VARCHAR NOT NULL,
    status          VARCHAR NOT NULL CHECK (status IN ('graded', 'error')),
    label           VARCHAR,
    score           DOUBLE,
    reason          VARCHAR,
    cited_event_ids VARCHAR[],
    graded_at       TIMESTAMPTZ NOT NULL
);
INSERT INTO grades SELECT * FROM grades_v3;
DROP TABLE grades_v3;

CREATE TABLE grade_calls_v3 AS SELECT * FROM grade_calls;
DROP TABLE grade_calls;
CREATE TABLE grade_calls (
    call_id       VARCHAR NOT NULL,
    session_id    VARCHAR NOT NULL,
    chunk_index   INTEGER NOT NULL,
    backend       VARCHAR NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL,
    duration_ms   BIGINT,
    input_tokens  BIGINT,
    output_tokens BIGINT,
    exit_code     INTEGER,
    status        VARCHAR NOT NULL,
    output        JSON,
    error         VARCHAR
);
INSERT INTO grade_calls SELECT * FROM grade_calls_v3;
DROP TABLE grade_calls_v3;

CREATE TABLE proposals_v3 AS SELECT * FROM proposals;
DROP TABLE proposals;
CREATE TABLE proposals (
    proposal_id VARCHAR NOT NULL,
    cluster_key VARCHAR NOT NULL,
    skill       VARCHAR,
    rubric      VARCHAR,
    status      VARCHAR NOT NULL CHECK (status IN ('proposed', 'rejected')),
    reason      VARCHAR,
    path        VARCHAR,
    diff_path   VARCHAR,
    evidence    JSON,
    created_at  TIMESTAMPTZ NOT NULL
);
INSERT INTO proposals SELECT * FROM proposals_v3;
DROP TABLE proposals_v3;
