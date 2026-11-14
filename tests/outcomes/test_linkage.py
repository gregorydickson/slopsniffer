from pathlib import Path

import pytest

from slopsniffer.config import TICKET_PATTERN_ENV
from tests.ingest.support import query
from tests.outcomes.support import FakeGh, link_corpus, outcomes
from tests.signals.support import labelled

POS = "link-pos"
NOREPO = "link-norepo"
CODEX = "0000bbbb-0000-4000-8000-000000000002"
DROID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
BRANCH = "dev/acme-1234-widgets"
TICKETS = r"ACME-\d+"
REPO = "acme/widgets"

type Link = tuple[str, str | None, str | None, int | None, str | None, str | None, str | None, str]


def links() -> list[Link]:
    return [
        tuple(row)
        for row in query(
            "SELECT session_id, repo, branch, pr_number, commit_sha, ticket_id, source_event_id, "
            "method FROM session_links"
        )
    ]


@pytest.fixture
def corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: FakeGh,
) -> dict[str, str]:
    monkeypatch.setenv(TICKET_PATTERN_ENV, TICKETS)
    root = link_corpus(tmp_path, monkeypatch, capsys)
    code, doc = outcomes(capsys)
    assert code == 0, doc
    assert doc["links_written"] == len(links())
    return labelled(root)


def test_link_set_equals_the_extracted_links(corpus: dict[str, str]) -> None:
    ids = corpus
    assert set(links()) == {
        (POS, REPO, BRANCH, None, None, None, None, "git_branch"),
        (POS, None, None, None, None, "ACME-1234", None, "ticket_branch"),
        (POS, REPO, BRANCH, None, "1a2b3c4", None, ids["commit-res"], "git_commit"),
        (POS, REPO, None, 42, None, None, ids["ghpr-res"], "gh_pr_create"),
        (POS, REPO, None, 43, None, None, ids["gt-res"], "gt_submit"),
        (POS, None, None, None, None, "ACME-1234", ids["u-ticket"], "ticket_mention"),
        (POS, None, None, None, None, "ACME-88", ids["a-ticket"], "ticket_mention"),
        (NOREPO, None, "main", None, None, None, None, "git_branch"),
        (CODEX, None, "feature/x", None, None, None, None, "git_branch"),
        (CODEX, "acme/gadgets", None, 5, None, None, ids["cx-res"], "gh_pr_create"),
        (DROID, REPO, "main", None, "abcdef0", None, ids["droid-commit"], "git_commit"),
    }


def test_url_inside_a_pasted_document_is_not_linked(corpus: dict[str, str]) -> None:
    pasted = query("SELECT text FROM messages WHERE event_id = ?", [corpus["pasted"]])
    assert "github.com/evil/other/pull/9" in pasted[0][0]
    assert {link for link in links() if link[1] == "evil/other"} == set()
    linked_events = {link[6] for link in links()}
    assert {corpus["pasted"], corpus["cat-res"]}.isdisjoint(linked_events)


def test_injected_context_mention_is_not_linked(corpus: dict[str, str]) -> None:
    text = query("SELECT text FROM messages WHERE event_id = ?", [corpus["u-ticket"]])[0][0]
    assert "ACME-2201" in text
    assert "ACME-2201" not in {link[5] for link in links()}


TICKET_METHODS = {"ticket_branch", "ticket_mention"}


def test_lowercase_branch_ticket_is_normalised_to_the_mentioned_form(
    corpus: dict[str, str],
) -> None:
    assert "acme-1234" in BRANCH
    assert {link[5] for link in links() if link[7] == "ticket_branch"} == {"ACME-1234"}


def test_no_pattern_links_no_tickets_and_keeps_every_other_link(
    corpus: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with_tickets = set(links())
    monkeypatch.delenv(TICKET_PATTERN_ENV)
    code, doc = outcomes(capsys)
    assert code == 0, doc
    assert set(links()) == {link for link in with_tickets if link[7] not in TICKET_METHODS}
    assert {link[7] for link in with_tickets} >= TICKET_METHODS


def test_pattern_matching_only_inside_a_longer_key_links_nothing(
    corpus: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(TICKET_PATTERN_ENV, r"CME-\d+")
    code, doc = outcomes(capsys)
    assert code == 0, doc
    assert {link[7] for link in links()}.isdisjoint(TICKET_METHODS)


def test_a_rerun_rebuilds_the_same_links_without_duplicates(
    corpus: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    before = links()
    code, doc = outcomes(capsys)
    assert code == 0, doc
    after = links()
    assert sorted(after, key=repr) == sorted(before, key=repr)
    assert len(after) == len(set(after))
