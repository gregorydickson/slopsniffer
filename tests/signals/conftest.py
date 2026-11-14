from pathlib import Path

import pytest

from tests.signals import support


@pytest.fixture
def root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Path:
    """The signals corpus, ingested through every lane."""
    return support.corpus(tmp_path, monkeypatch, capsys)
