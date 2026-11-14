"""CLI output contracts.

The JSON Schemas in ``schemas/`` are the source of truth; these TypedDicts
mirror them, and ``tests/test_contracts.py`` asserts each TypedDict's field set
equals its schema's property set.
"""

from typing import Literal, NotRequired, TypedDict

Harness = Literal["claude", "codex", "droid", "pickle", "crush", "skills"]
Rubric = Literal["efficiency", "code-quality", "procedure-compliance", "verbosity"]
RollupDimension = Literal["lane", "skill", "project", "day", "week"]


class HarnessIngest(TypedDict):
    files_seen: int
    files_skipped_unchanged: int
    files_resumed: int
    files_reingested: int
    files_new: int
    files_failed: list[str]
    events_written: int
    ingest_errors: int


class IngestSummary(TypedDict):
    started_at: str
    finished_at: str
    harnesses: dict[Harness, HarnessIngest]


class Signal(TypedDict):
    signal_id: str
    kind: str
    session_id: str
    harness: Harness
    project: str | None
    severity: float
    evidence_event_ids: list[str]
    details: dict[str, object]


class SignalsReport(TypedDict):
    window: dict[str, str | None]
    signals: list[Signal]
    rollups: dict[RollupDimension, list[dict[str, object]]]


class SignalsRunSummary(TypedDict):
    signals_written: int
    by_kind: dict[str, int]


class OutcomesSummary(TypedDict):
    links_written: int
    prs_fetched: int
    prs_cached: int
    rate_limited: bool
    rate_remaining: int | None


class ChunkVerdict(TypedDict):
    rubric: Rubric
    label: str
    score: float
    reason: str
    cited_event_ids: list[str]


class GradeSummary(TypedDict):
    selected_session_ids: list[str]
    calls: int
    calls_by_backend: dict[str, int]
    chunks_errored: int
    sessions_graded: int


class Proposal(TypedDict):
    skill: str
    path: str
    diff_path: str
    evidence_sessions: list[str]


class RejectedCluster(TypedDict):
    cluster_key: str
    reason: str


class ProposeSummary(TypedDict):
    proposals: list[Proposal]
    rejected: list[RejectedCluster]


class ReportSections(TypedDict):
    top_findings: list[dict[str, object]]
    pricing_coverage: NotRequired[list[dict[str, object]]]
    unpriced_models: NotRequired[list[dict[str, object]]]
    rollups: dict[str, list[dict[str, object]]]
    signal_leaderboard: list[dict[str, object]]
    grade_distribution: dict[str, dict[str, int]]
    proposals: list[Proposal]
    rejected_clusters: list[RejectedCluster]


class Report(TypedDict):
    generated_at: str
    sections: ReportSections


class QueryResult(TypedDict):
    columns: list[str]
    rows: list[list[object]]


class RehydrateResult(TypedDict):
    event_id: str
    source_path: str
    byte_offset: int
    byte_length: int
    raw_base64: str


class HarnessStatus(TypedDict):
    files_by_status: NotRequired[dict[str, int]]
    unknown_record_types: dict[str, int]


class Status(TypedDict):
    session_counts: list[dict[str, object]]
    db_path: str
    db_size_bytes: int
    schema_version: int
    harnesses: dict[Harness, HarnessStatus]
    last_runs: dict[str, str | None]
    pricing_coverage: list[dict[str, object]]
    unpriced_models: list[dict[str, object]]


class RunSummary(TypedDict):
    ingest: IngestSummary
    signals: SignalsRunSummary
    outcomes: OutcomesSummary
    grade: NotRequired[GradeSummary]


class CliError(TypedDict):
    error: str
    message: str
    command: str | None
    holder_pid: NotRequired[int | None]
