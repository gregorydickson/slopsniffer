"""Outcome test helpers: a fake ``gh`` on PATH, the linkage corpus, synthetic PR sessions and
the ``outcomes --json`` run."""

import json
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from slopsniffer.cli import main
from slopsniffer.contracts import Harness
from slopsniffer.ingest import claude, codex, droid, engine
from tests.helpers import CLI_SCHEMAS_DIR, REPO_ROOT, load_schema, schema_registry
from tests.outcomes.fake_gh import PR_ALIAS

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "outcomes"
FAKE_GH = Path(__file__).with_name("fake_gh.py")
REPO_PLACEHOLDER = "__REPO__"
ORIGIN = "git@github.com:Acme/Widgets.git"

type PrKey = tuple[str, int]


@dataclass(frozen=True)
class FakeGh:
    """Handle on the fake ``gh`` the ``fake_gh`` fixture puts first on PATH."""

    scenario_path: Path
    log_path: Path

    def scenario(self, **scenario: Any) -> None:
        self.scenario_path.write_text(json.dumps(scenario))

    def calls(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def graphql_queries(self) -> list[str]:
        return [
            json.loads(c["stdin"])["query"]
            for c in self.calls()
            if c["argv"][:2] == ["api", "graphql"]
        ]

    def queried_prs(self) -> list[set[PrKey]]:
        """The PR set of every GraphQL query, in call order."""
        return [
            {(f"{owner}/{name}", int(n)) for _, owner, name, n in PR_ALIAS.findall(q)}
            for q in self.graphql_queries()
        ]

    def clear_log(self) -> None:
        self.log_path.unlink(missing_ok=True)


def write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **outcomes: int) -> None:
    path = tmp_path / "config.toml"
    body = "".join(f"{key} = {value}\n" for key, value in outcomes.items())
    path.write_text(f"[outcomes]\n{body}")
    monkeypatch.setenv("SLOPSNIFFER_CONFIG", str(path))


def git_repo(path: Path, origin: str = ORIGIN) -> Path:
    path.mkdir(parents=True)
    for args in (["init", "-q"], ["remote", "add", "origin", origin]):
        subprocess.run(["git", "-C", str(path), *args], check=True, timeout=30)
    return path


def ingest(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    sources: Mapping[Harness, engine.HarnessSource],
) -> None:
    for harness, source in sources.items():
        monkeypatch.setitem(engine.REGISTRY, harness, source)
    code = main(["ingest", "--harness", ",".join(sources), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0, doc


def link_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Path:
    """Copy the linkage corpus with every cwd placeholder pointing at a real git checkout whose
    origin is :data:`ORIGIN`, ingest it and return the corpus root."""
    root = tmp_path / "outcomes-corpus"
    shutil.copytree(FIXTURES, root)
    checkout = git_repo(tmp_path / "checkout")
    for path in root.rglob("*.jsonl"):
        path.write_text(path.read_text().replace(REPO_PLACEHOLDER, str(checkout)))
    ingest(
        capsys,
        monkeypatch,
        {
            claude.HARNESS: claude.source(root / "claude"),
            codex.HARNESS: codex.source(root / "codex"),
            droid.HARNESS: droid.source(root / "droid"),
        },
    )
    return root


def pr_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    prs: Iterable[PrKey],
) -> None:
    """Ingest one synthetic Claude session that opened every PR in ``prs`` with ``gh pr create``."""
    root = tmp_path / "pr-sessions"
    write_pr_session(root / "-prs" / "prs.jsonl", prs)
    ingest(capsys, monkeypatch, {claude.HARNESS: claude.source(root)})


def write_pr_session(path: Path, prs: Iterable[PrKey]) -> None:
    """A Claude transcript at ``path`` whose session opened every PR in ``prs``."""
    lines = []
    for i, (repo, number) in enumerate(prs):
        base = {"sessionId": "prs", "cwd": "/nonexistent/prs", "timestamp": "2026-11-09T10:00:00Z"}
        use = {
            "type": "tool_use",
            "id": f"t{i}",
            "name": "Bash",
            "input": {"command": "gh pr create"},
        }
        result = {
            "type": "tool_result",
            "tool_use_id": f"t{i}",
            "content": f"https://github.com/{repo}/pull/{number}",
        }
        lines.append(
            {
                **base,
                "uuid": f"a{i}",
                "type": "assistant",
                "message": {"role": "assistant", "content": [use]},
            }
        )
        lines.append(
            {
                **base,
                "uuid": f"u{i}",
                "type": "user",
                "message": {"role": "user", "content": [result]},
            }
        )
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def pr_node(
    number: int,
    title: str = "",
    state: str = "OPEN",
    *,
    merged_at: str | None = None,
    body: str = "",
    head: str = "head0",
    last_commit: str | None = None,
    ci: str | None = "SUCCESS",
    reviews: Iterable[tuple[str, str]] = (),
    review_decision: str | None = None,
) -> dict[str, Any]:
    """A PullRequest node as the fetcher's query returns it."""
    rollup = {"state": ci} if ci is not None else None
    commit = {"oid": last_commit or head, "statusCheckRollup": rollup}
    return {
        "number": number,
        "title": title or f"PR {number}",
        "body": body,
        "state": state,
        "mergedAt": merged_at,
        "reviewDecision": review_decision,
        "headRefOid": head,
        "reviews": {
            "nodes": [
                {"author": {"login": who}, "state": verdict, "submittedAt": "2026-11-09T10:00:00Z"}
                for who, verdict in reviews
            ]
        },
        "commits": {"nodes": [{"commit": commit}]},
    }


def outcomes(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict[str, Any]]:
    """``outcomes --json``; a summary document must match its schema."""
    code = main(["outcomes", "--json", *args])
    doc: dict[str, Any] = json.loads(capsys.readouterr().out)
    if "error" not in doc:
        schema = load_schema(CLI_SCHEMAS_DIR / "outcomes.schema.json")
        Draft202012Validator(schema, registry=schema_registry()).validate(doc)
    return code, doc


def short_gh_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hang guard only; deadline tests use explicit mock clocks."""
    from functools import partial

    from slopsniffer import outcomes
    from slopsniffer.outcomes.github import Gh

    monkeypatch.setattr(outcomes, "Gh", partial(Gh, timeout=30))
