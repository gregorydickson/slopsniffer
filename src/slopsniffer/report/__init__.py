"""The analysis report: one document whose section keys are also the HTML section ids."""

import json
from collections.abc import Mapping
from datetime import datetime
from typing import get_type_hints

import duckdb

from slopsniffer import pricing, signals
from slopsniffer.config import Config, load_config
from slopsniffer.contracts import (
    Proposal,
    RejectedCluster,
    Report,
    ReportSections,
    RollupDimension,
    Signal,
)
from slopsniffer.contracts import Rubric as RubricName
from slopsniffer.grading.rubrics import Rubric
from slopsniffer.report.findings import ranked
from slopsniffer.report.paths import display_report

SECTIONS: tuple[str, ...] = tuple(get_type_hints(ReportSections))
TOP_FINDINGS = 20
ROLLUP_DIMENSIONS: tuple[RollupDimension, ...] = ("lane", "skill", "project", "week")

# Catalog scalar; grade groups contain only rubric/hash/label scalars. Proposal
# reads contain reduced proposal output, never transcript/tool/context bodies.
_ROLLUP_EXISTS = "SELECT count(*) FROM duckdb_functions() WHERE function_name = 'rollup'"
_GRADES = """
SELECT rubric, rubric_hash, label, count(*) FROM grades
WHERE status = 'graded' GROUP BY ALL ORDER BY ALL
"""
_PROPOSED = """
SELECT rowid, skill, path, diff_path FROM proposals
WHERE status = 'proposed' ORDER BY skill, path, proposal_id
"""
_REJECTED = """
SELECT DISTINCT cluster_key, reason FROM proposals WHERE status = 'rejected' ORDER BY cluster_key
"""


def build(
    con: duckdb.DuckDBPyConnection,
    rubrics: Mapping[RubricName, Rubric],
    now: datetime,
    config: Config | None = None,
) -> Report:
    pricing.install(con, config or load_config())
    found, rollups = _signals(con, now)
    sections: ReportSections = {
        "top_findings": ranked(con, _top(found)),
        "pricing_coverage": pricing.coverage(con),
        "unpriced_models": pricing.unpriced(con),
        "rollups": rollups,
        "signal_leaderboard": _stored_leaderboard(con),
        "grade_distribution": _grade_distribution(con, rubrics),
        "proposals": _proposals(con),
        "rejected_clusters": _rejected(con),
    }
    return display_report({"generated_at": now.isoformat(), "sections": sections})


def _signals(
    con: duckdb.DuckDBPyConnection, now: datetime
) -> tuple[list[Signal], dict[str, list[dict[str, object]]]]:
    """Requested top findings and rollups; both empty before signals were computed (the rollup
    macro only exists once they have been)."""
    row = con.execute(_ROLLUP_EXISTS).fetchone()
    if not row or not row[0]:
        return [], {dim: [] for dim in ROLLUP_DIMENSIONS}
    computed = signals.report(con, signals.Filters(), now, top=TOP_FINDINGS)
    rollups: dict[str, list[dict[str, object]]] = {
        dim: computed["rollups"][dim] for dim in ROLLUP_DIMENSIONS
    }
    return computed["signals"], rollups


def _top(found: list[Signal]) -> list[Signal]:
    return sorted(found, key=lambda s: (-s["severity"], s["signal_id"]))[:TOP_FINDINGS]


def _leaderboard(found: list[Signal]) -> list[dict[str, object]]:
    by_kind: dict[str, list[Signal]] = {}
    for s in found:
        by_kind.setdefault(s["kind"], []).append(s)
    ranked = sorted(by_kind.items(), key=lambda kv: (-sum(s["severity"] for s in kv[1]), kv[0]))
    return [
        {
            "kind": kind,
            "signals": len(members),
            "sessions": len({s["session_id"] for s in members}),
            "total_severity": sum(s["severity"] for s in members),
            "max_severity": max(s["severity"] for s in members),
        }
        for kind, members in ranked
    ]


def _stored_leaderboard(con: duckdb.DuckDBPyConnection) -> list[dict[str, object]]:
    # Counts/maxima group only narrow scalar facts. Stream severity in the old
    # report order through Python sum(), preserving its float-rounding behavior.
    known = (
        "session_id IN (SELECT session_id FROM sessions UNION ALL "
        "SELECT pickle_run_id FROM pickle_runs UNION ALL SELECT path FROM skills)"
    )
    found: list[tuple[str, int, int, float, float]] = []
    for kind, n, sessions, maximum in con.execute(
        "SELECT kind, count(*), count(DISTINCT session_id), max(severity) "
        "FROM signals WHERE " + known + " GROUP BY kind ORDER BY kind"
    ).fetchall():
        total = sum(
            float(severity)
            for (severity,) in con.sql(
                "SELECT severity FROM signals WHERE kind = $1 AND "
                + known
                + " ORDER BY session_id, signal_id",
                params=[kind],
            ).iter_rows(batch_size=1)
        )
        found.append((kind, n, sessions, total, float(maximum)))
    return [
        {
            "kind": kind,
            "signals": n,
            "sessions": sessions,
            "total_severity": total,
            "max_severity": maximum,
        }
        for kind, n, sessions, total, maximum in sorted(found, key=lambda row: (-row[3], row[0]))
    ]


def _grade_distribution(
    con: duckdb.DuckDBPyConnection, rubrics: Mapping[RubricName, Rubric]
) -> dict[str, dict[str, int]]:
    """Labels of the grades made under each rubric's current text."""
    found: dict[str, dict[str, int]] = {name: {} for name in rubrics}
    for rubric, digest, label, n in con.execute(_GRADES).fetchall():
        if rubric in rubrics and rubrics[rubric].sha256 == digest:
            found[rubric][label] = int(n)
    return found


def _proposals(con: duckdb.DuckDBPyConnection) -> list[Proposal]:
    """One proposal per drafted file; its clusters' evidence sessions unioned."""
    by_file: dict[tuple[str, str, str], set[str]] = {}
    # Decode one proposal's reduced evidence at a time; retain only session ids.
    for position, skill, path, diff_path in con.sql(_PROPOSED).iter_rows(batch_size=1):
        (evidence,) = con.execute(
            "SELECT evidence::VARCHAR FROM proposals WHERE rowid = ?", [position]
        ).fetchall()[0]
        sessions = by_file.setdefault((skill, path, diff_path), set())
        sessions.update(json.loads(evidence or "{}").get("session_ids", []))
    return [
        {"skill": skill, "path": path, "diff_path": diff, "evidence_sessions": sorted(sessions)}
        for (skill, path, diff), sessions in by_file.items()
    ]


def _rejected(con: duckdb.DuckDBPyConnection) -> list[RejectedCluster]:
    return [
        {"cluster_key": key, "reason": reason} for key, reason in con.execute(_REJECTED).fetchall()
    ]
