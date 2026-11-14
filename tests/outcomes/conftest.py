"""The ``fake_gh`` fixture: every outcomes test runs against the fake ``gh``, never the real one."""

import os
from pathlib import Path

import pytest

from tests.helpers import fake_bin
from tests.outcomes.support import FAKE_GH, FakeGh, short_gh_timeout


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    short_gh_timeout(monkeypatch)
    bin_dir = fake_bin("gh", FAKE_GH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    fake = FakeGh(tmp_path / "gh-scenario.json", tmp_path / "gh-calls.jsonl")
    monkeypatch.setenv("FAKE_GH_SCENARIO", str(fake.scenario_path))
    monkeypatch.setenv("FAKE_GH_LOG", str(fake.log_path))
    fake.scenario()
    return fake
