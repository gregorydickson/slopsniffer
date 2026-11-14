import io
from pathlib import Path
from typing import Any

import pytest

from slopsniffer.ingest import common, engine, skills
from tests.ingest.deadlines import deadline


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Run fast boundary contracts before expensive integration fixture setup.

    Mutmut supplies test identities as a set. Keep the full selection, but make a
    small discriminating contract fail before unrelated database fixtures run.
    """
    items.sort(key=lambda item: (not item.path.name.endswith("_contracts.py"), item.nodeid))


@pytest.fixture(autouse=True)
def bounded_ingest(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finite non-slow inputs must terminate, including work performed in fixtures."""
    if request.node.get_closest_marker("slow"):
        return
    pending = common.pending
    frontmatter = skills.frontmatter
    pool = engine._run_pool

    def bounded(*args: Any, **kwargs: Any) -> None:
        with deadline(120, "ingest finalization"):
            pending(*args, **kwargs)

    monkeypatch.setattr(common, "pending", bounded)

    def bounded_frontmatter(text: str) -> dict[str, str]:
        with deadline(2, "skill frontmatter"):
            return frontmatter(text)

    monkeypatch.setattr(skills, "frontmatter", bounded_frontmatter)

    def bounded_pool(*args: Any, **kwargs: Any) -> None:
        with deadline(120, "ingest pool"):
            pool(*args, **kwargs)

    monkeypatch.setattr(engine, "_run_pool", bounded_pool)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME, the data dir and the .env file at tmp so no test touches real config, data
    or a developer's local .env."""
    home = tmp_path / "home"
    home.mkdir()
    empty_env = tmp_path / "empty.env"
    empty_env.touch()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SLOPSNIFFER_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SLOPSNIFFER_ENV_FILE", str(empty_env))
    monkeypatch.delenv("SLOPSNIFFER_CONFIG", raising=False)
    monkeypatch.delenv("SLOPSNIFFER_TICKET_PATTERN", raising=False)
    return home


@pytest.fixture(params=["ascii", "utf-16"])
def non_utf8_text_defaults(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Resolve unspecified text encodings to an alternate codec."""

    def encoding(name: str | None, stacklevel: int = 2) -> str:
        return str(request.param) if name is None else name

    monkeypatch.setattr(io, "text_encoding", encoding)
