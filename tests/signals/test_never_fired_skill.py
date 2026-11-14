from pathlib import Path

import pytest

from tests.signals import support


def skill_path(root: Path, name: str) -> str:
    return str(root / "skills" / "global" / name / "SKILL.md")


def test_an_inventoried_skill_never_invoked_is_signalled_without_evidence(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = support.run(capsys)
    assert support.evidence(report, "never_fired_skill") == {
        (skill_path(root, "lonely"), frozenset())
    }
    assert {s["harness"] for s in report["signals"] if s["kind"] == "never_fired_skill"} == {
        "skills"
    }


def test_an_invoked_skill_is_not_signalled_unless_its_invocations_precede_the_window(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    demo = skill_path(root, "demo")
    assert demo not in support.subjects(support.run(capsys), "never_fired_skill")
    after_every_invocation = support.run(capsys, "--since", "2027-01-01")
    assert demo in support.subjects(after_every_invocation, "never_fired_skill")


def test_duplicate_unused_definitions_and_unambiguous_plugin_aliases() -> None:
    from datetime import UTC, datetime

    from slopsniffer.config import load_config
    from slopsniffer.db import open_write
    from slopsniffer.signals.runner import compute

    config = load_config()
    with open_write(config) as con:
        con.execute(
            "INSERT INTO skills VALUES "
            "('/a/SKILL.md','unused','global',NULL,'same',NULL),"
            "('/b/SKILL.md','unused','project',NULL,'same','/work'),"
            "('/c/SKILL.md','different','global',NULL,'other',NULL),"
            "('/plug/SKILL.md','plugin:called','plugin',NULL,'called',NULL)"
        )
        con.execute(
            "INSERT INTO source_files (source_file_id,path,harness) VALUES (17,'test','claude')"
        )
        con.execute("INSERT INTO sessions (session_id,harness) VALUES ('s','claude')")
        con.execute(
            "INSERT INTO events (event_id,session_id,seq,record_type,kind,source_file_id,"
            "byte_offset,byte_length) VALUES ('e','s',1,'user','message',17,0,1)"
        )
        con.execute("INSERT INTO skill_invocations VALUES ('e','s','called','skill_tool',NULL)")
        compute(con, config, None, datetime(2026, 11, 12, tzinfo=UTC))
        assert set(
            con.execute("SELECT session_id FROM signals WHERE kind='never_fired_skill'").fetchall()
        ) == {("/a/SKILL.md",), ("/c/SKILL.md",)}
