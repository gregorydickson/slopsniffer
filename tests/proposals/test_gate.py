"""The evidence gate and proposal files, through ``slopsniffer propose`` with fake backends."""

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
from jsonschema import Draft202012Validator

from slopsniffer.cli import main
from slopsniffer.config import load_config
from slopsniffer.grading.grader import SessionVerdict
from slopsniffer.proposals.clusters import UNATTRIBUTED, root_cause
from slopsniffer.proposals.writer import UnsafeTarget, Writer
from tests.grading.fake_grader import DRAFT_LINE
from tests.helpers import CLI_SCHEMAS_DIR, load_schema
from tests.proposals.support import (
    Fakes,
    Grade,
    Home,
    Session,
    failing,
    install_fakes,
    invocation_event,
    real_home,
    real_home_at,
    seed,
)

SKIPPED = "Skipped the review checklist before merging."
FORCE_PUSH = "Force-pushed over the shared branch, breaching a protective rule."
SCHEMA = load_schema(CLI_SCHEMAS_DIR / "propose.schema.json")


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return real_home(tmp_path)


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    return install_fakes(tmp_path, monkeypatch)


def propose(capsys: pytest.CaptureFixture[str], code: int = 0) -> dict[str, Any]:
    capsys.readouterr()
    assert main(["propose", "--json"]) == code
    out = json.loads(capsys.readouterr().out)
    Draft202012Validator(SCHEMA).validate(out)
    assert isinstance(out, dict)
    return out


def rows() -> set[tuple[str, str, str | None]]:
    with duckdb.connect(str(load_config().db_path), read_only=True) as con:
        found = con.execute("SELECT cluster_key, status, reason FROM proposals").fetchall()
    return {(k, s, r) for k, s, r in found}


def proposals_tree() -> set[Path]:
    root = load_config().data_dir / "proposals"
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def key(surface: str, rubric: str, reason: str, cited: tuple[str, ...] = ()) -> str:
    return f"{surface}|{rubric}|{root_cause(reason, cited)}"


def test_a_single_session_cluster_without_a_protective_breach_is_rejected_with_its_reason(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(home, [Session("s1", (failing("code-quality", "block", SKIPPED),), skills=("review",))])
    out = propose(capsys)
    cluster = key("review", "code-quality", SKIPPED)
    reason = "single session s1 and no protective-rule breach"
    assert out == {"proposals": [], "rejected": [{"cluster_key": cluster, "reason": reason}]}
    assert rows() == {(cluster, "rejected", reason)}
    assert proposals_tree() == set()
    assert fakes.calls() == []


def test_a_two_session_cluster_writes_all_three_files_and_leaves_real_files_untouched(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    before = home.checksums()
    restated = "skipped the REVIEW checklist, before merging"
    seed(
        home,
        [
            Session("s1", (failing("code-quality", "block", SKIPPED),), skills=("review",)),
            Session("s2", (failing("code-quality", "block", restated),), skills=("review",)),
        ],
    )
    out = propose(capsys)
    folder = load_config().data_dir / "proposals" / "review"
    assert out == {
        "proposals": [
            {
                "skill": "review",
                "path": str((folder / "SKILL.md").resolve()),
                "diff_path": str((folder / "change.diff").resolve()),
                "evidence_sessions": ["s1", "s2"],
            }
        ],
        "rejected": [],
    }
    assert proposals_tree() == {
        Path("review", f) for f in ("SKILL.md", "change.diff", "evidence.json")
    }
    real = home.skill_file("review")
    assert (folder / "SKILL.md").read_text().endswith(f"{DRAFT_LINE}\n")
    diff = (folder / "change.diff").read_text()
    assert diff.startswith(f"--- {real}\n")
    assert f"+{DRAFT_LINE}\n" in diff
    evidence = json.loads((folder / "evidence.json").read_text())
    cluster_key = key("review", "code-quality", SKIPPED)
    cluster_evidence = {
        "cluster_key": cluster_key,
        "rubric": "code-quality",
        "root_cause": "before checklist merging review skipped",
        "protective": False,
        "session_ids": ["s1", "s2"],
        "failures": [
            {"session_id": sid, "label": "block", "score": 0.2, "reason": reason, "event_ids": []}
            for sid, reason in [("s1", SKIPPED), ("s2", restated)]
        ],
    }
    assert evidence == {
        "skill": "review",
        "real_path": str(real),
        "session_ids": ["s1", "s2"],
        "event_ids": [],
        "rubrics": ["code-quality"],
        "backend_reason": "A missing rule.",
        "clusters": [cluster_evidence],
    }
    with duckdb.connect(str(load_config().db_path), read_only=True) as con:
        stored = con.execute(
            "SELECT proposal_id, path, diff_path, evidence FROM proposals"
        ).fetchall()
    assert [
        (pid, path, diff_path, json.loads(payload)) for pid, path, diff_path, payload in stored
    ] == [
        (
            hashlib.sha256(cluster_key.encode()).hexdigest()[:32],
            str(folder / "SKILL.md"),
            str(folder / "change.diff"),
            cluster_evidence,
        )
    ]
    assert rows() == {(key("review", "code-quality", SKIPPED), "proposed", None)}
    assert home.checksums() == before


def test_different_root_causes_do_not_pool_their_sessions(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    other = "Merged without running the integration suite."
    seed(
        home,
        [
            Session("s1", (failing("code-quality", "block", SKIPPED),), skills=("review",)),
            Session("s2", (failing("code-quality", "block", other),), skills=("review",)),
        ],
    )
    out = propose(capsys)
    assert out["proposals"] == []
    assert {r["cluster_key"] for r in out["rejected"]} == {
        key("review", "code-quality", SKIPPED),
        key("review", "code-quality", other),
    }


def test_a_single_session_protective_breach_is_proposed(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    grade = failing("procedure-compliance", "noncompliant", FORCE_PUSH)
    seed(home, [Session("s1", (grade,), skills=("deploy",))])
    out = propose(capsys)
    assert [(p["skill"], p["evidence_sessions"]) for p in out["proposals"]] == [("deploy", ["s1"])]
    assert out["rejected"] == []


def test_a_protective_phrase_outside_procedure_compliance_is_not_a_breach(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(home, [Session("s1", (failing("code-quality", "block", FORCE_PUSH),), skills=("deploy",))])
    out = propose(capsys)
    assert out["proposals"] == []
    assert [r["cluster_key"] for r in out["rejected"]] == [
        key("deploy", "code-quality", FORCE_PUSH)
    ]


NOT_FAILED = {
    "error": Grade("code-quality", None),
    "insufficient_evidence": Grade(
        "code-quality", SessionVerdict("insufficient_evidence", None, SKIPPED, [])
    ),
    "passing_score": failing("efficiency", "mostly_efficient", SKIPPED),
    "stale_rubric_hash": Grade(
        "code-quality", failing("code-quality", "block", SKIPPED).verdict, True
    ),
}


@pytest.mark.parametrize("shape", sorted(NOT_FAILED))
def test_grades_that_are_not_current_failures_form_no_cluster(
    shape: str, home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    grade = NOT_FAILED[shape]
    seed(home, [Session(sid, (grade,), skills=("review",)) for sid in ("s1", "s2")])
    assert propose(capsys) == {"proposals": [], "rejected": []}
    assert rows() == set()


def test_a_cited_invocation_attributes_the_failure_to_that_skill_alone(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    sessions = [
        Session(
            sid,
            (failing("code-quality", "block", SKIPPED, [invocation_event(sid, "review")]),),
            skills=("deploy", "review"),
        )
        for sid in ("s1", "s2")
    ]
    seed(home, sessions)
    assert [p["skill"] for p in propose(capsys)["proposals"]] == ["review"]


def test_an_uncited_failure_is_attributed_to_every_skill_the_session_invoked(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(
        home,
        [
            Session(sid, (failing("code-quality", "block", SKIPPED),), skills=("deploy", "review"))
            for sid in ("s1", "s2")
        ],
    )
    assert {p["skill"] for p in propose(capsys)["proposals"]} == {"deploy", "review"}


def test_a_session_without_skills_is_attributed_to_its_claude_md(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    before = home.checksums()
    rule = str(home.rule_file)
    seed(
        home,
        [
            Session(sid, (failing("code-quality", "block", SKIPPED),), rules=(rule,))
            for sid in ("s1", "s2")
        ],
    )
    out = propose(capsys)
    assert [(p["skill"], p["evidence_sessions"]) for p in out["proposals"]] == [
        (rule, ["s1", "s2"])
    ]
    assert Path(out["proposals"][0]["path"]).read_text().endswith(f"{DRAFT_LINE}\n")
    assert home.checksums() == before


def test_a_failure_with_no_instruction_surface_is_rejected_even_across_sessions(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(home, [Session(sid, (failing("code-quality", "block", SKIPPED),)) for sid in ("s1", "s2")])
    out = propose(capsys)
    assert out["proposals"] == []
    assert [r["cluster_key"] for r in out["rejected"]] == [
        key(UNATTRIBUTED, "code-quality", SKIPPED)
    ]


def test_a_declined_draft_rejects_the_cluster_with_the_backend_reason(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes.scenario(codex=["decline"], claude=["ok"])
    seed(
        home,
        [
            Session(sid, (failing("code-quality", "block", SKIPPED),), skills=("review",))
            for sid in ("s1", "s2")
        ],
    )
    out = propose(capsys)
    reason = "the grading backend declined: Model variance, not a gap."
    cluster = key("review", "code-quality", SKIPPED)
    assert out == {"proposals": [], "rejected": [{"cluster_key": cluster, "reason": reason}]}
    assert proposals_tree() == set()


def test_a_blank_draft_is_rejected_and_writes_nothing(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes.scenario(codex=["empty"], claude=["ok"])
    seed(
        home,
        [
            Session(sid, (failing("code-quality", "block", SKIPPED),), skills=("review",))
            for sid in ("s1", "s2")
        ],
    )
    out = propose(capsys)
    reason = "the grading backend proposed an empty file"
    cluster = key("review", "code-quality", SKIPPED)
    assert out == {"proposals": [], "rejected": [{"cluster_key": cluster, "reason": reason}]}
    assert proposals_tree() == set()


def test_a_real_skill_inside_the_proposals_tree_fails_propose_with_a_json_error(
    tmp_path: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    proposals_root = load_config().data_dir / "proposals"
    proposals_root.mkdir(parents=True)
    copied = real_home(tmp_path)
    copied.root.rename(proposals_root / "real")
    home = real_home_at(proposals_root / "real")
    before = home.checksums()
    seed(
        home,
        [
            Session(sid, (failing("code-quality", "block", SKIPPED),), skills=("review",))
            for sid in ("s1", "s2")
        ],
    )
    capsys.readouterr()
    assert main(["propose", "--json"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert (error["error"], error["command"]) == ("unsafe_target", "propose")
    assert home.checksums() == before


def test_quota_falls_back_to_claude_and_failure_of_both_exits_3(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(
        home,
        [
            Session(sid, (failing("code-quality", "block", SKIPPED),), skills=("review",))
            for sid in ("s1", "s2")
        ],
    )
    fakes.scenario(codex=["quota"], claude=["ok"])
    assert [p["skill"] for p in propose(capsys)["proposals"]] == ["review"]
    assert [c["backend"] for c in fakes.calls()] == ["codex", "claude"]
    fakes.scenario(codex=["fail"], claude=["not_json"])
    out = propose(capsys, code=3)
    assert out["proposals"] == []
    assert [r["reason"].startswith("drafting failed: ") for r in out["rejected"]] == [True]
    assert proposals_tree() == set()


def test_a_rerun_rebuilds_rows_and_files(
    home: Home, fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(
        home,
        [
            Session(sid, (failing("code-quality", "block", SKIPPED),), skills=("review",))
            for sid in ("s1", "s2")
        ],
    )
    first = propose(capsys)
    stray = load_config().data_dir / "proposals" / "gone" / "SKILL.md"
    stray.parent.mkdir()
    stray.write_text("left over")
    assert propose(capsys) == first
    assert rows() == {(key("review", "code-quality", SKIPPED), "proposed", None)}
    assert not stray.exists()


def test_the_writer_refuses_targets_outside_its_tree_and_real_files(tmp_path: Path) -> None:
    real = tmp_path / "skills" / "review" / "SKILL.md"
    real.parent.mkdir(parents=True)
    real.write_text("real")
    writer = Writer(tmp_path / "data", [str(real)])
    with pytest.raises(UnsafeTarget, match="outside the proposals directory"):
        writer.check(tmp_path / "data" / "proposals" / ".." / "escape.md")
    with pytest.raises(UnsafeTarget, match="outside the proposals directory"):
        writer.check(tmp_path / "data" / "proposals")
    writer.root.mkdir(parents=True)
    (writer.root / "review").symlink_to(real.parent)
    with pytest.raises(UnsafeTarget, match="outside the proposals directory"):
        writer.write("review", str(real), "real", "improved", {})
    assert real.read_text() == "real"


def test_the_writer_refuses_a_real_file_that_sits_inside_its_tree(tmp_path: Path) -> None:
    real = tmp_path / "data" / "proposals" / "review" / "SKILL.md"
    real.parent.mkdir(parents=True)
    real.write_text("real")
    writer = Writer(tmp_path / "data", [str(real)])
    with pytest.raises(UnsafeTarget, match="real instruction files inside"):
        writer.reset()
    with pytest.raises(UnsafeTarget, match="a real instruction file"):
        writer.write("review", str(real), "real", "improved", {})
    assert real.read_text() == "real"


def test_a_symlinked_proposals_directory_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "proposals").symlink_to(elsewhere)
    with pytest.raises(UnsafeTarget, match="not a plain directory"):
        Writer(tmp_path / "data", [])
