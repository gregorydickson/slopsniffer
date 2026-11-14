"""Self-contained HTML rendering of a :class:`Report`.

The page carries its own stylesheet and no ``src`` or ``href`` attribute, so it never loads
anything. Every value from the database or a diff file is escaped.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from slopsniffer.contracts import Report, ReportSections
from slopsniffer.report.paths import display_report, display_text

TITLES: Mapping[str, str] = {
    "top_findings": "Top findings",
    "rollups": "Rollups by lane, skill and project",
    "pricing_coverage": "Cost coverage by harness",
    "unpriced_models": "Unpriced models and token volume",
    "signal_leaderboard": "Signal leaderboard (includes informational observations)",
    "grade_distribution": "Grade distribution per rubric",
    "proposals": "Proposals",
    "rejected_clusters": "Rejected clusters",
}

_STYLE = """
:root { color-scheme: light dark; }
body { max-width: 1200px; font: 14px/1.4 system-ui, sans-serif; margin: 2rem;
       color: #222; background: #fff;
       overflow-wrap: anywhere; }
.table-scroll { max-width: 100%; overflow-x: auto; }
table { width: 100%; min-width: 700px; table-layout: fixed;
        border-collapse: collapse; margin: 0.5rem 0 1.5rem; }
th, td { border: 1px solid #ccc; padding: 0.25rem 0.5rem; text-align: left;
         vertical-align: top; }
th { background: #f3f3f3; }
pre { background: #f8f8f8; border: 1px solid #ddd; padding: 0.5rem; overflow-x: auto; }
.overview { background: #eef4fa; border-radius: 12px; padding: 1rem; }
.overview strong { font-size: 1.4rem; }
.finding { border: 1px solid #ccc; border-radius: 12px; padding: 1rem; margin: 1rem 0; }
.finding h3 { margin: 0 0 .5rem; font-size: 1.15rem; }
.meta { color: #666; }
.explanation { font-size: 1.05rem; }
summary { cursor: pointer; }
details table { table-layout: auto; min-width: 100%; }
.empty { color: #777; font-style: italic; }
@media (max-width: 600px) { body { margin: 1rem; } .finding { padding: .75rem; } }
@media (prefers-color-scheme: dark) {
 body { background: #15191f; color: #e5e9ef; }
 .overview { background: #202b39; }
 th { background: #242c36; } pre { background: #1c232c; }
 th, td, pre, .finding { border-color: #475364; } .meta, .empty { color: #b8c2ce; }
}
"""


def write(path: Path, report: Report) -> None:
    """Render ``report`` to ``path`` atomically, with each proposal's diff read from disk."""
    page = render(
        report, {p["diff_path"]: _diff(p["diff_path"]) for p in report["sections"]["proposals"]}
    )
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(page, "utf-8")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _diff(path: str) -> str:
    try:
        return Path(path).expanduser().read_text("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"(diff unavailable: {exc})"


def render(report: Report, diffs: Mapping[str, str]) -> str:
    """The page; ``diffs`` maps each proposal's ``diff_path`` to the text shown for it."""
    report = display_report(report)
    diffs = {display_text(k): display_text(v) for k, v in diffs.items()}
    sections = report["sections"]
    body = "\n".join(
        f'<section id="{key}">\n<h2>{escape(TITLES[key])}</h2>\n{_section(sections, key, diffs)}'
        "\n</section>"
        for key in TITLES
        if key in sections
    )
    try:
        generated = datetime.fromisoformat(report["generated_at"]).astimezone(UTC)
        generated_text = generated.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        generated_text = report["generated_at"]
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>slopsniffer report</title>\n<style>{_STYLE}</style>\n</head>\n<body>\n"
        f"<h1>slopsniffer report</h1>\n<p>Generated {escape(generated_text)}</p>\n"
        f"{_overview(sections.get('pricing_coverage', []))}{body}\n</body>\n</html>\n"
    )


def _section(sections: ReportSections, key: str, diffs: Mapping[str, str]) -> str:
    if key == "rollups":
        return "\n".join(
            f"<h3>{escape(dim)}</h3>\n{_table(rows)}" for dim, rows in sections["rollups"].items()
        )
    if key == "grade_distribution":
        return "\n".join(
            f"<h3>{escape(rubric)}</h3>\n"
            + _table([{"label": label, "grades": n} for label, n in labels.items()])
            for rubric, labels in sections["grade_distribution"].items()
        )
    if key == "proposals":
        return (
            "\n".join(
                f"<h3>{escape(p['skill'])}</h3>\n<p>Draft: {escape(p['path'])}<br>"
                f"Evidence sessions: {escape(', '.join(p['evidence_sessions']))}</p>\n"
                f"<pre>{escape(diffs.get(p['diff_path'], ''))}</pre>"
                for p in sections["proposals"]
            )
            or _EMPTY
        )
    if key == "rejected_clusters":
        return _table(sections["rejected_clusters"])
    if key == "signal_leaderboard":
        return _table(sections["signal_leaderboard"])
    if key in ("pricing_coverage", "unpriced_models"):
        return _table(
            sections["pricing_coverage"]
            if key == "pricing_coverage"
            else sections["unpriced_models"]
        )
    findings = sections["top_findings"]
    if any("explanation" in row for row in findings):
        return (
            "<p>Best group per kind interleaved by maximum severity, then subsequent groups "
            "per kind. Wasted tokens and dollars cannot be attributed "
            "reliably to these events. Session costs are context, not claimed waste. "
            "Zero-severity observations are informational and excluded here. "
            "Amounts are USD list-price estimates; partial totals exclude unknown usage.</p>"
            + "\n".join(_finding(row) for row in findings)
        )
    return _table(findings)


_EMPTY = '<p class="empty">None.</p>'


def _table(rows: Sequence[Mapping[str, object]]) -> str:
    if not rows:
        return _EMPTY
    columns = list(dict.fromkeys(column for row in rows for column in row))
    head = "".join(f"<th>{escape(c)}</th>" for c in columns)
    body = "\n".join(
        "<tr>"
        + "".join(
            f"<td>{escape(_severity(row.get(c)) if 'severity' in c else _cell(row.get(c)))}</td>"
            for c in columns
        )
        + "</tr>"
        for row in rows
    )
    return f'<div class="table-scroll"><table>\n<tr>{head}</tr>\n{body}\n</table></div>'


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, sort_keys=True)


def _finding(row: Mapping[str, object]) -> str:
    title = row.get("title") or f"{row.get('project') or 'Unknown project'} · Untitled session"
    session_count = row.get("session_count")
    if isinstance(session_count, int) and session_count > 1:
        title = f"{row.get('kind')} · {row.get('subject')}"
    cost = row.get("cost_usd")
    money = (
        f"${cost:,.2f}"
        if isinstance(cost, (float, int)) and row.get("cost_status") != "unpriced"
        else "unknown cost"
    )
    duration = row.get("duration_seconds")
    elapsed = (
        f"{duration / 60:,.0f} min" if isinstance(duration, (float, int)) else "duration unknown"
    )
    metadata = " · ".join(
        str(row.get(k) or "unknown") for k in ("project", "branch", "harness", "started_at")
    )
    if row.get("harness") == "claude" and row.get("parent_session_id"):
        metadata += f" · parent: {row['parent_session_id']}"
    commands = row.get("open_commands")
    command_text = "\n".join(map(str, commands)) if isinstance(commands, list) else ""
    worst = row.get("worst_sessions")
    context_cost = row.get("affected_sessions_cost_usd")
    context_money = f"${context_cost:,.2f}" if isinstance(context_cost, (int, float)) else "unknown"
    summary = (
        f"<p>{escape(str(row.get('finding_count')))} findings · "
        f"{escape(str(session_count))} sessions"
        + (
            f" · {escape(str(row['call_count']))} calls"
            if row.get("call_count") is not None
            else ""
        )
        + f" · Affected sessions: {escape(context_money)} known cost (context, not waste); "
        f"{escape(str(row.get('affected_sessions_unpriced')))} incompletely priced sessions.</p>"
        if session_count is not None
        else ""
    )
    return (
        '<article class="finding">'
        f'<h3>{escape(str(title))}</h3><p class="meta">{escape(metadata)}</p>'
        f"<p><strong>{escape(money)}</strong> ({escape(str(row.get('cost_status')))}) · "
        f"{escape(elapsed)} · severity {escape(_severity(row.get('severity')))}</p>"
        f'<p class="explanation">{escape(str(row.get("explanation")))}</p>'
        + summary
        + f"<pre>{escape(command_text)}</pre><p>Source: "
        f"{escape(str(row.get('source_path') or 'unavailable'))}</p>"
        "<details><summary>Evidence and rehydrate commands</summary>"
        + _table([row])
        + "</details>"
        + (
            "<details><summary>Worst affected sessions</summary>" + _table(worst) + "</details>"
            if isinstance(worst, list)
            else ""
        )
        + "</article>"
    )


def _overview(coverage: Sequence[Mapping[str, object]]) -> str:
    if not coverage:
        return ""

    def total(key: str) -> float:
        return sum(
            float(value) for row in coverage if isinstance(value := row.get(key), (int, float))
        )

    sessions, priced = total("sessions"), total("priced_sessions")
    incomplete = total("partial_sessions") + total("unpriced_sessions")
    label = "Partial known spend" if incomplete else "Known spend"
    return (
        '<aside class="overview">'
        f"<strong>{label}: ${total('known_cost_usd'):,.2f}</strong>"
        f"<p>{priced:,.0f} of {sessions:,.0f} sessions fully priced. "
        f"{incomplete:,.0f} sessions have incomplete or missing costs.</p>"
        "<p>USD list-price estimates, plus reported costs where available. "
        "Unknown usage is excluded. Session spend is context, not claimed waste.</p></aside>"
    )


def _severity(value: object) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") if isinstance(value, (int, float)) else "unknown"
