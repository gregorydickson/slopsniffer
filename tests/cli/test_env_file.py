from pathlib import Path

import pytest

from slopsniffer import config as config_module
from slopsniffer.config import ENV_FILE_ENV, TICKET_PATTERN_ENV, load_config
from slopsniffer.errors import ConfigError


def env_file(path: Path, text: str) -> Path:
    path.write_text(text, "utf-8")
    return path


def test_ticket_pattern_is_off_by_default() -> None:
    assert load_config().outcomes.ticket_pattern == ""


def test_config_file_sets_the_ticket_pattern(tmp_path: Path) -> None:
    flag = tmp_path / "c.toml"
    flag.write_text('[outcomes]\nticket_pattern = "ACME-\\\\d+"\n', "utf-8")
    assert load_config(str(flag)).outcomes.ticket_pattern == r"ACME-\d+"


def test_environment_overrides_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = tmp_path / "c.toml"
    flag.write_text('[outcomes]\nticket_pattern = "FROMTOML-\\\\d+"\n', "utf-8")
    monkeypatch.setenv(TICKET_PATTERN_ENV, r"FROMENV-\d+")
    assert load_config(str(flag)).outcomes.ticket_pattern == r"FROMENV-\d+"


def test_env_file_supplies_values_and_the_real_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = env_file(
        tmp_path / ".env",
        "# local settings\n\nexport SLOPSNIFFER_TICKET_PATTERN='DOT-\\d+'\n",
    )
    monkeypatch.setenv(ENV_FILE_ENV, str(dotenv))
    assert load_config().outcomes.ticket_pattern == r"DOT-\d+"
    monkeypatch.setenv(TICKET_PATTERN_ENV, r"REAL-\d+")
    assert load_config().outcomes.ticket_pattern == r"REAL-\d+"


def test_env_file_can_name_the_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "from-dotenv"
    monkeypatch.delenv(config_module.HOME_ENV)
    monkeypatch.setenv(ENV_FILE_ENV, str(env_file(tmp_path / ".env", f"SLOPSNIFFER_HOME={data}\n")))
    assert load_config().data_dir == data.resolve()


def test_checkout_env_file_is_read_when_none_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    env_file(checkout / ".env", "SLOPSNIFFER_TICKET_PATTERN=CHK-\\d+\n")
    monkeypatch.delenv(ENV_FILE_ENV)
    monkeypatch.setattr(config_module, "_source_repo_root", lambda: checkout)
    assert load_config().outcomes.ticket_pattern == r"CHK-\d+"
    monkeypatch.setattr(config_module, "_source_repo_root", lambda: None)
    assert load_config().outcomes.ticket_pattern == ""


def test_named_env_file_must_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FILE_ENV, str(tmp_path / "missing.env"))
    with pytest.raises(ConfigError, match="does not exist"):
        load_config()


@pytest.mark.parametrize("line", ["JUSTAWORD", "=value", "BAD KEY=1"])
def test_malformed_env_line_is_rejected(
    line: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_FILE_ENV, str(env_file(tmp_path / ".env", f"OK=1\n{line}\n")))
    with pytest.raises(ConfigError, match=r"\.env:2"):
        load_config()


@pytest.mark.parametrize("pattern", ["ACME-(\\d+", "(?<=x)ACME-\\d+"])
def test_pattern_the_database_cannot_run_is_rejected(
    pattern: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TICKET_PATTERN_ENV, pattern)
    with pytest.raises(ConfigError, match="ticket_pattern"):
        load_config()
