-- slopsniffer DuckDB schema. Every statement is idempotent; db.apply_schema
-- runs the whole file on each read-write open and records schema_version.
-- No ART indexes (including PRIMARY KEY/UNIQUE) on history-growing tables, even
-- source_files and sessions: their memory would grow with the corpus. Single-writer
-- transactions, committed offsets, replacement derivations and blob anti-joins own
-- uniqueness. Only skills keeps a key: it is a replaced inventory of installed files.

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER     NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE SEQUENCE IF NOT EXISTS source_file_id_seq;

CREATE TABLE IF NOT EXISTS source_files (
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

CREATE TABLE IF NOT EXISTS sessions (
    session_id        VARCHAR NOT NULL,
    harness           VARCHAR NOT NULL,
    parent_session_id VARCHAR,
    project           VARCHAR,
    cwd               VARCHAR,
    git_branch        VARCHAR,
    model             VARCHAR,
    started_at        TIMESTAMPTZ,
    ended_at          TIMESTAMPTZ,
    source_file_id    BIGINT,
    title             VARCHAR
);

CREATE TABLE IF NOT EXISTS events (
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

CREATE TABLE IF NOT EXISTS messages (
    event_id VARCHAR NOT NULL,
    role     VARCHAR NOT NULL,
    text     VARCHAR
);

CREATE TABLE IF NOT EXISTS tool_calls (
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

CREATE TABLE IF NOT EXISTS skill_invocations (
    event_id   VARCHAR NOT NULL,
    session_id VARCHAR NOT NULL,
    skill_name VARCHAR NOT NULL,
    via        VARCHAR NOT NULL CHECK (via IN ('skill_tool', 'slash_command')),
    args       VARCHAR
);

CREATE TABLE IF NOT EXISTS blobs (
    content_hash VARCHAR NOT NULL,
    content      VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS context_blocks (
    event_id     VARCHAR NOT NULL,
    ordinal      INTEGER NOT NULL,
    kind         VARCHAR NOT NULL
        CHECK (kind IN ('claude_md', 'skill_listing', 'memory_index', 'hook_output', 'other')),
    source_path  VARCHAR,
    content_hash VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    event_id      VARCHAR NOT NULL,
    input_tokens  BIGINT,
    output_tokens BIGINT,
    cache_read    BIGINT,
    cache_write   BIGINT,
    model         VARCHAR
);

CREATE TABLE IF NOT EXISTS session_cost (
    session_id VARCHAR NOT NULL,
    cost_usd   DOUBLE,
    method     VARCHAR NOT NULL CHECK (method IN ('cost_state', 'price_table', 'partial_price_table', 'unpriced'))
);

CREATE TABLE IF NOT EXISTS pickle_runs (
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

CREATE TABLE IF NOT EXISTS pickle_iterations (
    pickle_run_id  VARCHAR NOT NULL,
    iteration      INTEGER NOT NULL,
    source_file_id BIGINT,
    started_at     TIMESTAMPTZ,
    ended_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pickle_session_links (
    pickle_run_id   VARCHAR NOT NULL,
    iteration       INTEGER,
    session_id      VARCHAR NOT NULL,
    source_event_id VARCHAR
);

CREATE TABLE IF NOT EXISTS skills (
    path         VARCHAR PRIMARY KEY,
    name         VARCHAR NOT NULL,
    scope        VARCHAR NOT NULL CHECK (scope IN ('global', 'plugin', 'project')),
    description  VARCHAR,
    content_hash VARCHAR NOT NULL,
    repo         VARCHAR
);

CREATE TABLE IF NOT EXISTS ingest_errors (
    source_file_id BIGINT      NOT NULL,
    byte_offset    BIGINT,
    error          VARCHAR     NOT NULL,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id          VARCHAR NOT NULL,
    kind               VARCHAR NOT NULL,
    session_id         VARCHAR NOT NULL,
    severity           DOUBLE  NOT NULL,
    evidence_event_ids VARCHAR[] NOT NULL,
    details            JSON,
    computed_at        TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS session_links (
    session_id      VARCHAR NOT NULL,
    repo            VARCHAR,
    branch          VARCHAR,
    pr_number       INTEGER,
    commit_sha      VARCHAR,
    ticket_id       VARCHAR,
    source_event_id VARCHAR,
    method          VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS pr_outcomes (
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

CREATE TABLE IF NOT EXISTS grades (
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

CREATE TABLE IF NOT EXISTS grade_calls (
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

-- Large clusters store bounded evidence fragments under the same cluster_key;
-- proposal_id identifies the fragment. Reports union per-file sessions and dedupe
-- rejected cluster summaries. No history-growing uniqueness/ART index is needed.
CREATE TABLE IF NOT EXISTS proposals (
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

CREATE TABLE IF NOT EXISTS command_runs (
    command     VARCHAR     NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    exit_code   INTEGER,
    summary     JSON
);


CREATE TABLE IF NOT EXISTS session_relations (
    event_id VARCHAR NOT NULL,
    session_id VARCHAR NOT NULL,
    child_session_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL
);

-- Constant-sized parser migration state; invalidation and revision commit together.
CREATE TABLE IF NOT EXISTS ingest_parser_versions (
    harness VARCHAR NOT NULL,
    version INTEGER NOT NULL
);
