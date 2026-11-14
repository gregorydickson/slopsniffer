from pathlib import Path
from typing import Any

import pytest

from slopsniffer.cli import main
from slopsniffer.config import (
    CONFIG_ENV,
    HOME_ENV,
    ConfigSource,
    ModelPrice,
    default_data_dir,
    load_config,
    packaged_defaults,
    user_config_path,
)
from slopsniffer.errors import ConfigError
from tests.helpers import REPO_ROOT


def write_toml(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return path


def rerun_min_file(path: Path, value: int) -> Path:
    return write_toml(path, f"[signals]\nrerun_min = {value}\n")


def test_precedence_flag_then_env_then_user_then_packaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = packaged_defaults()["signals"]["rerun_min"]
    user_value, env_value, flag_value = packaged + 1, packaged + 2, packaged + 3
    user = rerun_min_file(user_config_path(), user_value)
    env = rerun_min_file(tmp_path / "env.toml", env_value)
    flag = rerun_min_file(tmp_path / "flag.toml", flag_value)
    monkeypatch.setenv(CONFIG_ENV, str(env))

    layered = load_config(str(flag))
    assert (layered.source, layered.path, layered.signals.rerun_min) == (
        ConfigSource.FLAG,
        flag,
        flag_value,
    )

    layered = load_config(None)
    assert (layered.source, layered.path, layered.signals.rerun_min) == (
        ConfigSource.ENV,
        env,
        env_value,
    )

    monkeypatch.delenv(CONFIG_ENV)
    layered = load_config(None)
    assert (layered.source, layered.path, layered.signals.rerun_min) == (
        ConfigSource.USER,
        user,
        user_value,
    )

    user.unlink()
    layered = load_config(None)
    assert (layered.source, layered.path, layered.signals.rerun_min) == (
        ConfigSource.PACKAGED,
        None,
        packaged,
    )


def test_override_file_keeps_unset_keys_at_packaged_defaults(tmp_path: Path) -> None:
    defaults = packaged_defaults()
    flag = rerun_min_file(tmp_path / "flag.toml", defaults["signals"]["rerun_min"] + 1)
    config = load_config(str(flag))
    assert config.signals.churn_min == defaults["signals"]["churn_min"]
    assert config.outcomes.rate_reserve == defaults["outcomes"]["rate_reserve"]


def test_packaged_defaults_name_every_prd_threshold() -> None:
    defaults: dict[str, Any] = packaged_defaults()
    named = {
        ("signals", "rerun_min"),
        ("signals", "churn_min"),
        ("signals", "abandon_window"),
        ("signals", "orphan_hours"),
        ("signals", "correction_lexicon"),
        ("pricing", "models"),
        ("outcomes", "rate_reserve"),
        ("outcomes", "open_pr_ttl_hours"),
        ("grading", "badness_weights"),
        ("grading", "chunk_tokens"),
        ("grading", "chunk_overlap"),
        ("ingest", "memory_limit"),
        ("ingest", "workers"),
    }
    missing = {path for path in named if path[1] not in defaults.get(path[0], {})}
    assert missing == set()
    assert (defaults["outcomes"]["rate_reserve"], defaults["ingest"]["memory_limit"]) == (
        1000,
        "512MB",
    )
    assert defaults["ingest"]["workers"] == 4


@pytest.mark.parametrize("layer", ["flag", "env"])
def test_explicitly_named_missing_file_is_an_error(
    layer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope.toml"
    rerun_min_file(user_config_path(), 99)
    if layer == "env":
        monkeypatch.setenv(CONFIG_ENV, str(missing))
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(str(missing) if layer == "flag" else None)


def test_missing_user_file_falls_back_to_packaged() -> None:
    assert not user_config_path().exists()
    assert load_config(None).source is ConfigSource.PACKAGED


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    flag = write_toml(tmp_path / "c.toml", "[signals]\nrerun_minimum = 3\n")
    with pytest.raises(ConfigError, match=r"unknown config key signals\.rerun_minimum"):
        load_config(str(flag))


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ('[signals]\nrerun_min = "3"\n', "signals.rerun_min"),
        ("[signals]\nrerun_min = true\n", "signals.rerun_min"),
        ("[signals]\ncorrection_lexicon = [1, 2]\n", "signals.correction_lexicon"),
        ("signals = 3\n", "signals"),
    ],
)
def test_wrong_type_is_rejected(tmp_path: Path, body: str, key: str) -> None:
    flag = write_toml(tmp_path / "c.toml", body)
    with pytest.raises(ConfigError, match=key.replace(".", r"\.")):
        load_config(str(flag))


def test_integer_accepted_where_default_is_float(tmp_path: Path) -> None:
    flag = write_toml(tmp_path / "c.toml", "[grading.badness_weights]\nreverted = 9\n")
    assert load_config(str(flag)).grading.badness_weights.reverted == 9.0


def test_new_pricing_model_must_match_packaged_entry_shape(tmp_path: Path) -> None:
    packaged_models = packaged_defaults()["pricing"]["models"]
    template = next(iter(packaged_models.values()))
    full = "".join(f"{key} = 2.0\n" for key in template)
    good = write_toml(tmp_path / "good.toml", f'[pricing.models."new-model"]\n{full}')
    assert set(load_config(str(good)).pricing) == set(packaged_models) | {"new-model"}
    assert load_config(str(good)).pricing["new-model"] == ModelPrice(2, 2, 2, 2)

    legacy = write_toml(
        tmp_path / "legacy.toml",
        '[pricing.models."legacy-model"]\ninput_usd_per_mtok = 3\n'
        "cache_read_usd_per_mtok = 0.3\noutput_usd_per_mtok = 10\n",
    )
    assert load_config(str(legacy)).pricing["legacy-model"] == ModelPrice(3, 0.3, 10, 0)

    first_key = next(iter(template))
    partial = write_toml(tmp_path / "bad.toml", f'[pricing.models."new-model"]\n{first_key} = 1\n')
    with pytest.raises(ConfigError, match="must set exactly"):
        load_config(str(partial))


def test_data_dir_from_env_and_db_inside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "elsewhere"))
    config = load_config(None)
    assert config.data_dir == (tmp_path / "elsewhere").resolve()
    assert config.db_path == config.data_dir / "telemetry.duckdb"


def test_data_dir_defaults_under_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HOME_ENV)
    assert load_config(None).data_dir == default_data_dir().resolve()
    assert default_data_dir() == Path.home() / ".local" / "share" / "slopsniffer"


def test_data_dir_inside_repo_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    inside = REPO_ROOT / "data-in-repo"
    monkeypatch.setenv(HOME_ENV, str(inside))
    assert main(["ingest", "--json"]) == 2
    assert not inside.exists()


def test_cli_reports_config_error_as_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["status", "--json", "--config", str(tmp_path / "nope.toml")]) == 2
    assert '"error": "config"' in capsys.readouterr().out
