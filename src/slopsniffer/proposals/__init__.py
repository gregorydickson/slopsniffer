"""L3 proposals: evidence-gated drafts of skill and rule edits, never applied to real files.

Each run rebuilds the ``proposals`` table and the ``<data_dir>/proposals`` tree from the current
grades. Clusters that clear the evidence gate are grouped by surface, and the grading backend
drafts one improved file per surface; every other cluster is recorded with why it was rejected.
"""

import hashlib
import json
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

from slopsniffer import analysis
from slopsniffer.config import Config
from slopsniffer.contracts import Proposal, ProposeSummary, RejectedCluster
from slopsniffer.contracts import Rubric as RubricName
from slopsniffer.grading.backends import EXECUTORS, ORDER
from slopsniffer.grading.rubrics import Rubric
from slopsniffer.ingest.reader import BATCH_ROWS
from slopsniffer.proposals.clusters import Cluster, Failure, Surface, stored_clusters
from slopsniffer.proposals.clusters import clusters as clusters
from slopsniffer.proposals.drafter import Drafter, Executor, build_prompt
from slopsniffer.proposals.writer import UnsafeTarget, Writer

__all__ = ["ProposeRun", "UnsafeTarget", "propose"]


@dataclass(frozen=True)
class ProposeRun:
    summary: ProposeSummary
    draft_failures: list[str]


@dataclass(frozen=True)
class _Row:
    cluster: Cluster
    reason: str | None
    path: str | None = None
    diff_path: str | None = None
    fragment: int = 0


def propose(
    con: duckdb.DuckDBPyConnection,
    config: Config,
    rubrics: Mapping[RubricName, Rubric],
    *,
    backend: str,
    executors: Mapping[str, Executor] = EXECUTORS,
) -> ProposeRun:
    with stored_clusters(con, rubrics) as found:
        return _propose(con, config, found, backend, executors)


def _propose(
    con: duckdb.DuckDBPyConnection,
    config: Config,
    found: Sequence[Cluster],
    backend: str,
    executors: Mapping[str, Executor],
) -> ProposeRun:
    rows: list[_Row] = []
    passing: dict[Surface, list[Cluster]] = {}
    for cluster in found:
        reason = cluster.rejection()
        if reason is None:
            passing.setdefault(cluster.surface, []).append(cluster)
        else:
            rows.append(_Row(cluster, reason))
    writer = Writer(config.data_dir, (c.surface.path for c in found if c.surface.path))
    writer.reset()
    proposals: list[Proposal] = []
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="slopsniffer-propose-") as tmp:
        drafter = Drafter(ORDER[backend], Path(tmp), executors)
        for surface, group in passing.items():
            proposal, reason = _draft(
                writer, drafter, surface, group, failures, batch_rows=config.ingest.batch_rows
            )
            if proposal is not None:
                proposals.append(proposal)
            paths = (None, None) if proposal is None else (proposal["path"], proposal["diff_path"])
            rows.extend(_Row(c, reason, *paths) for c in group)
    _store(con, rows, batch_rows=config.ingest.batch_rows)
    rejected: list[RejectedCluster] = [
        {"cluster_key": r.cluster.key, "reason": r.reason}
        for r in sorted(rows, key=lambda r: r.cluster.key)
        if r.reason is not None
    ]
    return ProposeRun({"proposals": proposals, "rejected": rejected}, failures)


def _draft(
    writer: Writer,
    drafter: Drafter,
    surface: Surface,
    group: Sequence[Cluster],
    failures: list[str],
    *,
    batch_rows: int = BATCH_ROWS,
) -> tuple[Proposal | None, str | None]:
    """The proposal for ``surface``, or None and why its clusters are rejected."""
    assert surface.path is not None
    try:
        current = Path(surface.path).read_text("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"the real file {surface.path} cannot be read: {exc}"
    improved = current
    backend_reason = ""
    for batch in _groups(group, batch_rows):
        draft, problem = drafter.draft(build_prompt(surface, improved, batch))
        if draft is None:
            failures.append(f"{surface.name}: {problem}")
            return None, f"drafting failed: {problem}"
        if not draft.propose:
            return None, f"the grading backend declined: {draft.reason}"
        if not draft.text.strip():
            return None, "the grading backend proposed an empty file"
        if draft.text == improved:
            return None, "the grading backend returned the file unchanged"
        improved, backend_reason = draft.text, draft.reason
    sessions = sorted({sid for c in group for sid in c.session_ids})
    evidence = {
        "skill": surface.name,
        "real_path": surface.path,
        "session_ids": sessions,
        "event_ids": sorted({e for c in group for f in c.failures for e in f.event_ids}),
        "rubrics": sorted({c.rubric for c in group}),
        "backend_reason": backend_reason,
        "clusters": (_cluster_evidence(c, stream=True) for c in group),
    }
    written = writer.write(surface.name, surface.path, current, improved, evidence)
    proposal: Proposal = {
        "skill": surface.name,
        "path": str(written.path),
        "diff_path": str(written.diff_path),
        "evidence_sessions": sessions,
    }
    return proposal, None


def _cluster_evidence(cluster: Cluster, *, stream: bool = False) -> dict[str, object]:
    evidence: dict[str, object] = {
        "cluster_key": cluster.key,
        "rubric": cluster.rubric,
        "root_cause": cluster.root_cause,
        "protective": cluster.protective,
        "session_ids": cluster.session_ids,
        "failures": _failure_evidence(cluster),
    }
    if not stream:
        evidence["failures"] = list(_failure_evidence(cluster))
    return evidence


def _failure_evidence(cluster: Cluster) -> Iterator[dict[str, object]]:
    for f in cluster.failures:
        yield {
            "session_id": f.session_id,
            "label": f.label,
            "score": f.score,
            "reason": f.reason,
            "event_ids": list(f.event_ids),
        }


def _groups(group: Sequence[Cluster], batch_rows: int) -> Iterator[list[Cluster]]:
    def weighted() -> Iterator[tuple[tuple[Cluster, Failure], int]]:
        for cluster in group:
            for failure in cluster.failures:
                # Charge the serialized singleton, including escaped Unicode,
                # identities and cluster metadata; merging can only reduce it.
                singleton = Cluster(cluster.surface, cluster.rubric, cluster.root_cause, (failure,))
                length = len(json.dumps(_cluster_evidence(singleton)))
                yield (cluster, failure), length

    for batch in analysis.sized_batches(weighted(), batch_rows):
        grouped: dict[tuple[Surface, RubricName, str], list[Failure]] = {}
        for cluster, failure in batch:
            grouped.setdefault((cluster.surface, cluster.rubric, cluster.root_cause), []).append(
                failure
            )
        yield [Cluster(s, r, c, tuple(f)) for (s, r, c), f in grouped.items()]


def _store(
    con: duckdb.DuckDBPyConnection, rows: Sequence[_Row], *, batch_rows: int = BATCH_ROWS
) -> None:
    # Commit each reduced cluster's evidence independently; a full-history write
    # transaction must not retain all serialized proposal JSON at once.
    analysis.create_replacement(con, "proposals", "analysis_proposals")
    try:
        for row in rows:
            for fragment, group in enumerate(_groups([row.cluster], batch_rows)):
                _insert(con, [_Row(group[0], row.reason, row.path, row.diff_path, fragment)])
        con.begin()
        try:
            con.execute("DROP TABLE proposals")
            con.execute("ALTER TABLE analysis_proposals RENAME TO proposals")
            con.commit()
        except BaseException:
            con.rollback()
            raise
    finally:
        con.execute("DROP TABLE IF EXISTS analysis_proposals")


def _insert(con: duckdb.DuckDBPyConnection, rows: Sequence[_Row]) -> None:
    con.executemany(
        "INSERT INTO analysis_proposals (proposal_id, cluster_key, skill, rubric, status, reason, "
        "path, diff_path, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, now())",
        [
            [
                hashlib.sha256(
                    (r.cluster.key if r.fragment == 0 else f"{r.cluster.key}\0{r.fragment}").encode(
                        "utf-8"
                    )
                ).hexdigest()[:32],
                r.cluster.key,
                r.cluster.surface.name,
                r.cluster.rubric,
                "rejected" if r.reason is not None else "proposed",
                r.reason,
                r.path,
                r.diff_path,
                json.dumps(_cluster_evidence(r.cluster)),
            ]
            for r in rows
        ],
    )
