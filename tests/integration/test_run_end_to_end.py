"""``run`` end to end over a fixture home holding every lane, with a fake ``gh``."""

from pathlib import Path

import pytest

from tests.cli.support import FILE_LANES, PR, cli, fixture_home, install_gh, source_files, validate
from tests.cli.test_cli_skeleton import git_status
from tests.ingest.support import event_ids, query


def test_a_second_run_skips_every_file_and_keeps_the_event_set(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = git_status()
    roots = fixture_home(isolated_home)
    expected = source_files(roots)
    gh = install_gh(tmp_path, monkeypatch)

    code, first = cli(capsys, "run")
    assert code == 0, first
    validate("run", first)
    ingested = {f for (f,) in query("SELECT path FROM source_files WHERE status = 'complete'")}
    assert ingested == set().union(*expected.values())
    assert {h for h in FILE_LANES if first["ingest"]["harnesses"][h]["files_new"]} == set(
        FILE_LANES
    )
    assert first["signals"]["signals_written"] == sum(first["signals"]["by_kind"].values())
    assert set(first["signals"]["by_kind"]) and any(first["signals"]["by_kind"].values())
    assert set().union(*gh.queried_prs()) == {PR}
    assert query("SELECT repo, pr_number, state FROM pr_outcomes") == [(*PR, "MERGED")]
    events = event_ids()
    gh.clear_log()

    code, second = cli(capsys, "run")
    assert code == 0, second
    validate("run", second)
    for harness in FILE_LANES:
        counts = second["ingest"]["harnesses"][harness]
        assert counts["files_skipped_unchanged"] == len(expected[harness]), harness
        assert {k for k, v in counts.items() if v} == {"files_seen", "files_skipped_unchanged"}
    assert event_ids() == events
    assert second["signals"] == first["signals"]
    assert (second["outcomes"]["prs_cached"], second["outcomes"]["prs_fetched"]) == (1, 0)
    assert set().union(*gh.queried_prs()) == set()
    assert git_status() == before
