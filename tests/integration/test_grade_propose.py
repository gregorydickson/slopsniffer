"""``grade`` then ``propose`` end to end through fake ``codex`` and ``claude`` executables."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from slopsniffer.cli import main
from slopsniffer.config import load_config
from slopsniffer.db import open_write
from tests.helpers import CLI_SCHEMAS_DIR, load_schema
from tests.proposals.support import install_fakes, inventory_skills, invocation_event, real_home

# Session -> the skill it invoked. The failing pair shares a skill; the third fails alone.
SESSIONS = {"s-a": "review", "s-b": "review", "s-c": "deploy"}
LABELS = {
    "code-quality": "approve",
    "procedure-compliance": "noncompliant",
    "efficiency": "mostly_efficient",
    "verbosity": "proportionate",
}


def test_two_failing_sessions_sharing_a_skill_yield_one_proposal_citing_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = real_home(tmp_path)
    before = home.checksums()
    fakes = install_fakes(tmp_path, monkeypatch)
    fakes.scenario(codex=["ok"], claude=["ok"], labels=LABELS)
    config = tmp_path / "config.toml"
    config.write_text("[grading]\nchunk_tokens = 200\nchunk_overlap = 10\n")
    monkeypatch.setenv("SLOPSNIFFER_CONFIG", str(config))
    with open_write(load_config()) as con:
        for sid, skill in SESSIONS.items():
            con.execute("INSERT INTO sessions (session_id, harness) VALUES (?, 'claude')", [sid])
            for seq, event_id in enumerate((f"{sid}-e0", invocation_event(sid, skill))):
                con.execute(
                    "INSERT INTO events (event_id, session_id, seq, record_type, kind, "
                    "source_file_id, byte_offset, byte_length) "
                    "VALUES (?, ?, ?, 'user', 'message', 1, ?, 1)",
                    [event_id, sid, seq, seq],
                )
                con.execute(
                    "INSERT INTO messages VALUES (?, 'user', ?)", [event_id, f"Run {skill}."]
                )
            con.execute(
                "INSERT INTO skill_invocations VALUES (?, ?, ?, 'skill_tool', NULL)",
                [invocation_event(sid, skill), sid, skill],
            )
            con.execute(
                "INSERT INTO signals VALUES (?, 'rerun_loop', ?, 1.0, [], NULL, now())",
                [f"sig-{sid}", sid],
            )
        inventory_skills(con, home)

    assert main(["grade", "--worst", str(len(SESSIONS)), "--json"]) == 0
    graded = json.loads(capsys.readouterr().out)
    assert set(graded["selected_session_ids"]) == set(SESSIONS)

    assert main(["propose", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    Draft202012Validator(load_schema(CLI_SCHEMAS_DIR / "propose.schema.json")).validate(out)
    shared = sorted(sid for sid, skill in SESSIONS.items() if skill == "review")
    assert [(p["skill"], p["evidence_sessions"]) for p in out["proposals"]] == [("review", shared)]
    assert [r["cluster_key"].split("|")[:2] for r in out["rejected"]] == [
        ["deploy", "procedure-compliance"]
    ]
    evidence = json.loads(Path(out["proposals"][0]["path"]).with_name("evidence.json").read_text())
    assert evidence["session_ids"] == shared
    assert evidence["rubrics"] == ["procedure-compliance"]
    assert home.checksums() == before
