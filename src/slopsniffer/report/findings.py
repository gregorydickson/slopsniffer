"""Human explanations and session navigation from bounded top finding evidence."""

import re
import shlex
from pathlib import Path

import duckdb

from slopsniffer import analysis
from slopsniffer.contracts import Signal
from slopsniffer.signals.text import CLAUSE_PHRASES, CORRECTION_CLAUSE, OWN_TEXT_PATTERN


def explain(signal: Signal, excerpt: str | None) -> str:
    d, kind = signal["details"], signal["kind"]
    phrases = d.get("phrases")
    phrase_text = ", ".join(map(str, phrases)) if isinstance(phrases, list) else ""
    tools = d.get("tools")
    tool_text = ", ".join(map(str, tools)) if isinstance(tools, list) else str(d.get("reason"))
    if kind == "rerun_loop":
        command = str(d.get("command"))
        command = command if len(command) <= 180 else command[:177] + "…"
        return (
            f"Ran `{command}` {d.get('runs')} times without an intervening edit or changed result."
        )
    if kind == "edit_churn":
        return f"Edited {d.get('path')} {d.get('edits')} times while repeating a failed repair."
    if kind == "user_correction":
        return f"User corrected the agent ({phrase_text}): “{excerpt or 'open evidence'}”"
    if kind == "unchanged_retry":
        return f"Retried {d.get('tool')} with identical input after a deterministic failure."
    if kind == "context_overflow":
        return (
            f"Compacted context {d.get('events')} times during active work; "
            "inspect repeated context overhead."
        )
    if kind == "skill_abandoned":
        return (
            f"Invoked {d.get('skill')}; the session ended soon afterwards with an observed error."
        )
    if kind == "never_fired_skill":
        return (
            f"Installed skill {d.get('skill')} has no recorded invocation; "
            "this alone does not prove waste."
        )
    if kind == "orphaned_session" and d.get("reason") == "stale_pickle_run":
        return (
            "Pickle run is still marked active after a prolonged gap in log output. "
            f"Last log write: {d.get('last_log_write_at') or 'not recorded'}."
        )
    if kind == "orphaned_session":
        return f"Session ended with an unanswered tool call: {tool_text}."
    return f"{kind.replace('_', ' ')}: inspect the cited evidence."


def enrich(con: duckdb.DuckDBPyConnection, signal: Signal) -> dict[str, object]:
    sid = signal["session_id"]
    row = con.execute(
        """
SELECT s.title, s.git_branch, strftime(timezone('UTC', s.started_at), '%Y-%m-%dT%H:%M:%SZ'),
       epoch(s.ended_at) - epoch(s.started_at),
       c.cost_usd, c.method, f.path, s.parent_session_id
FROM sessions s LEFT JOIN session_cost c USING (session_id)
LEFT JOIN source_files f USING (source_file_id) WHERE s.session_id = ?
""",
        [sid],
    ).fetchone()
    title, branch, start, duration, cost, method, path, parent = row or (None,) * 8
    pickle_dir = None
    if row is None and signal["harness"] == "pickle":
        run = con.execute(
            "SELECT current_ticket, step, "
            "strftime(timezone('UTC', to_timestamp(start_time_epoch)), "
            "'%Y-%m-%dT%H:%M:%SZ'), epoch(last_log_write_at) - start_time_epoch, "
            "coalesce(f.path, r.session_dir), r.session_dir FROM pickle_runs r "
            "LEFT JOIN source_files f USING (source_file_id) WHERE pickle_run_id = ?",
            [sid],
        ).fetchone()
        if run:
            ticket, step, start, duration, path, pickle_dir = run
            title = f"Pickle {ticket or sid}" + (f" · {step}" if step else "")
    excerpt = None
    if signal["kind"] == "user_correction":
        for eid in signal["evidence_event_ids"]:
            if con.execute(
                "SELECT 1 FROM messages WHERE event_id = ? AND role = 'user'", [eid]
            ).fetchone():
                (text,) = analysis.physical_row(con, "messages", "text", "event_id = ?", [eid])
                excerpt = correction_excerpt(text or "", signal["details"].get("phrases"))
    if not title or not title.strip():
        parent_title = (
            con.execute("SELECT title FROM sessions WHERE session_id = ?", [parent]).fetchone()
            if parent
            else None
        )
        if parent_title and parent_title[0] and parent_title[0].strip():
            title = f"Subagent of {parent_title[0]}"
        else:
            prompt = None
            offset = 0
            while not prompt:
                prompts = con.execute(
                    "SELECT m.event_id FROM messages m JOIN events e USING (event_id) "
                    "WHERE e.session_id = ? AND m.role = 'user' "
                    "ORDER BY e.seq NULLS LAST, e.source_file_id, e.byte_offset "
                    "LIMIT 64 OFFSET ?",
                    [sid, offset],
                ).fetchall()
                if not prompts:
                    break
                offset += len(prompts)
                for (eid,) in prompts:
                    (text,) = analysis.physical_row(con, "messages", "text", "event_id = ?", [eid])
                    own = re.sub(OWN_TEXT_PATTERN, "", text or "")
                    prompt = " ".join(own.split())[:160]
                    if prompt:
                        break
            title = (
                f"{signal['project'] or 'Unknown project'} · "
                f"{prompt or 'Session without a recorded user prompt'}"
            )
    harness = signal["harness"]
    resume = {"claude": "claude --resume", "codex": "codex resume", "droid": "droid --resume"}.get(
        harness
    )
    resume_id = parent if signal["harness"] == "claude" and parent else sid
    commands = [f"{resume} {shlex.quote(resume_id)}"] if resume and row else []
    if harness == "claude" and parent and path:
        commands.append(f"less {_shell_path(path)}")
    if pickle_dir:
        if signal["evidence_event_ids"]:
            commands = [f"slopsniffer rehydrate {shlex.quote(signal['evidence_event_ids'][0])}"]
        else:
            commands = (
                [f"cat {_shell_path(path)}"]
                if path != pickle_dir
                else [f"ls {_shell_path(pickle_dir)}"]
            )
    return dict(signal) | {
        "title": title,
        "branch": branch,
        "parent_session_id": parent,
        "started_at": start,
        "duration_seconds": duration,
        "cost_usd": cost,
        "cost_status": "complete"
        if method in ("price_table", "cost_state") and cost is not None
        else "partial"
        if method == "partial_price_table"
        else "unpriced",
        "explanation": explain(signal, excerpt),
        "source_path": path or (sid if harness == "skills" else None),
        "open_commands": commands,
        "evidence_commands": [
            f"slopsniffer rehydrate {shlex.quote(e)}" for e in signal["evidence_event_ids"]
        ],
        "ranking_basis": "severity (wasted tokens/cost cannot be attributed reliably)",
    }


def ranked(con: duckdb.DuckDBPyConnection, found: list[Signal]) -> list[dict[str, object]]:
    return [enrich(con, s) for s in sorted(found, key=lambda s: (-s["severity"], s["signal_id"]))]


def correction_excerpt(text: str, phrases: object) -> str:
    own = re.sub(OWN_TEXT_PATTERN, "", text)
    own = re.sub(r"[^\S\n]+", " ", own).strip()
    positions = []
    for phrase in phrases if isinstance(phrases, list) else []:
        if str(phrase).lower() in CLAUSE_PHRASES:
            starts = [match.start(1) for match in re.finditer(CORRECTION_CLAUSE, own)]
            positions.extend(starts or [own.lower().find(str(phrase).lower())])
        else:
            positions.append(own.lower().find(str(phrase).lower()))
    position = min((pos for pos in positions if pos >= 0), default=0)
    start = max(0, position - 80)
    return (
        ("…" if start else "")
        + " ".join(own[start : start + 300].split())
        + ("…" if len(own) > start + 300 else "")
    )


def _shell_path(path: str) -> str:
    # Keep a home-relative command executable even when the path needs quotes.
    try:
        relative = Path(path).relative_to(Path.home())
    except ValueError:
        return shlex.quote(path)
    return '"$HOME"/' + shlex.quote(str(relative))
