"""Configuration resolution.

One config file is chosen by precedence -- ``--config`` flag, then
``$SLOPSNIFFER_CONFIG``, then ``~/.config/slopsniffer/config.toml`` --
and merged over the packaged ``default_config.toml``. The packaged defaults
define the allowed keys and the type of every value.

Every ``SLOPSNIFFER_*`` variable may also come from a ``.env`` file: the one
``$SLOPSNIFFER_ENV_FILE`` names, else ``.env`` at the root of the source checkout
the package runs from. A variable set in the real environment wins over the file, and
the file never changes ``os.environ``, so child processes do not inherit it.
"""

import os
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb

from slopsniffer.errors import ConfigError

CONFIG_ENV = "SLOPSNIFFER_CONFIG"
HOME_ENV = "SLOPSNIFFER_HOME"
ENV_FILE_ENV = "SLOPSNIFFER_ENV_FILE"
TICKET_PATTERN_ENV = "SLOPSNIFFER_TICKET_PATTERN"
_ENV_LINE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)")
DB_FILENAME = "telemetry.duckdb"

# Tables whose keys are user-defined; new entries must match the shape of the packaged ones.
OPEN_TABLES: frozenset[tuple[str, ...]] = frozenset({("pricing", "models")})


class ConfigSource(StrEnum):
    FLAG = "flag"
    ENV = "env"
    USER = "user"
    PACKAGED = "packaged"


@dataclass(frozen=True)
class SourceRoots:
    claude_projects: Path
    codex_sessions: Path
    droid_sessions: Path
    pickle_sessions: tuple[Path, ...]
    skill_roots: tuple[Path, ...]
    plugin_roots: tuple[Path, ...]
    crush_projects: Path = Path("~/.local/share/crush/projects.json")


@dataclass(frozen=True)
class IngestConfig:
    workers: int
    threads: int
    memory_limit: str
    batch_rows: int
    sources: SourceRoots


@dataclass(frozen=True)
class SignalsConfig:
    rerun_min: int
    churn_min: int
    abandon_window: int
    orphan_hours: int
    correction_lexicon: tuple[str, ...]


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_mtok: float
    cache_read_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_write_usd_per_mtok: float = 0.0


@dataclass(frozen=True)
class OutcomesConfig:
    rate_reserve: int
    open_pr_ttl_hours: int
    pr_batch_size: int
    ticket_pattern: str


@dataclass(frozen=True)
class BadnessWeights:
    signal_severity: float
    ci_red_at_head: float
    changes_requested: float
    reverted: float
    closed_unmerged: float


@dataclass(frozen=True)
class GradingConfig:
    backend: str
    chunk_tokens: int
    chunk_overlap: int
    badness_weights: BadnessWeights


@dataclass(frozen=True)
class Config:
    source: ConfigSource
    path: Path | None
    data_dir: Path
    ingest: IngestConfig
    signals: SignalsConfig
    pricing: dict[str, ModelPrice]
    outcomes: OutcomesConfig
    grading: GradingConfig

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME


def packaged_defaults() -> dict[str, Any]:
    text = resources.files("slopsniffer").joinpath("default_config.toml").read_text("utf-8")
    return tomllib.loads(text)


def user_config_path() -> Path:
    return Path.home() / ".config" / "slopsniffer" / "config.toml"


def default_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "slopsniffer"


def resolve_config_file(flag: str | None) -> tuple[ConfigSource, Path | None]:
    """Pick the single config file in force. An explicitly named file must exist."""
    if flag:
        return ConfigSource.FLAG, _must_exist(Path(flag).expanduser(), "--config")
    env = env_value(CONFIG_ENV)
    if env:
        return ConfigSource.ENV, _must_exist(Path(env).expanduser(), f"${CONFIG_ENV}")
    user = user_config_path()
    if user.is_file():
        return ConfigSource.USER, user
    return ConfigSource.PACKAGED, None


def resolve_data_dir() -> Path:
    env = env_value(HOME_ENV)
    data_dir = (Path(env).expanduser() if env else default_data_dir()).resolve()
    repo = _source_repo_root()
    if repo is not None and data_dir.is_relative_to(repo):
        raise ConfigError(f"data dir {data_dir} is inside the slopsniffer repo {repo}")
    return data_dir


def env_value(name: str) -> str | None:
    """``name`` from the real environment, else from the ``.env`` file."""
    if name in os.environ:
        return os.environ[name]
    return read_env_file().get(name)


def env_file_path() -> Path | None:
    named = os.environ.get(ENV_FILE_ENV)
    if named:
        return _must_exist(Path(named).expanduser(), f"${ENV_FILE_ENV}")
    repo = _source_repo_root()
    if repo is not None and (repo / ".env").is_file():
        return repo / ".env"
    return None


def read_env_file() -> dict[str, str]:
    """``KEY=value`` lines; blank lines, ``#`` comments and an ``export`` prefix are allowed, and
    one pair of matching quotes around a value is removed. Values are taken literally."""
    path = env_file_path()
    if path is None:
        return {}
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text("utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.fullmatch(line)
        if match is None:
            raise ConfigError(f"{path}:{number}: expected KEY=value")
        value = match[2].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[match[1]] = value
    return values


def load_config(flag: str | None = None) -> Config:
    source, path = resolve_config_file(flag)
    merged = packaged_defaults()
    if path is not None:
        merged = _merge(merged, _read_toml(path), ())
    pattern = env_value(TICKET_PATTERN_ENV)
    if pattern is not None:
        merged["outcomes"]["ticket_pattern"] = pattern
    return _build(merged, source, path, resolve_data_dir())


def _check_ticket_pattern(pattern: str) -> None:
    """Linkage runs the pattern in DuckDB (RE2), so it is checked there, not by Python's ``re``."""
    if not pattern:
        return
    try:
        duckdb.connect().execute("SELECT regexp_matches('', ?)", [pattern]).fetchall()
    except duckdb.Error as exc:
        raise ConfigError(f"outcomes.ticket_pattern is not a valid RE2 pattern: {exc}") from exc


def _must_exist(path: Path, named_by: str) -> Path:
    if not path.is_file():
        raise ConfigError(f"config file named by {named_by} does not exist: {path}")
    return path


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _source_repo_root() -> Path | None:
    """The git checkout this package is running from, if any (editable installs)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / ".git").exists():
            return parent
    return None


def _dotted(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _merge(default: Any, override: Any, path: tuple[str, ...]) -> Any:
    if not isinstance(default, dict):
        _check_type(default, override, path)
        return override
    if not isinstance(override, dict):
        raise ConfigError(f"config key {_dotted(path)} must be a table")
    merged = dict(default)
    for key, value in override.items():
        key_path = (*path, key)
        if key in default:
            merged[key] = _merge(default[key], value, key_path)
        elif path in OPEN_TABLES:
            template = next(iter(default.values()))
            if (
                not isinstance(value, dict)
                or not set(value) <= set(template)
                or not {"input_usd_per_mtok", "cache_read_usd_per_mtok", "output_usd_per_mtok"}
                <= set(value)
            ):
                raise ConfigError(
                    f"config entry {_dotted(key_path)} must set exactly {sorted(template)}"
                )
            merged[key] = _merge(template, value, key_path)
        else:
            raise ConfigError(f"unknown config key {_dotted(key_path)}")
    return merged


def _check_type(default: Any, value: Any, path: tuple[str, ...]) -> None:
    expected = type(default)
    ok = type(value) is expected or (expected is float and type(value) is int)
    if ok and isinstance(default, list) and default:
        element = type(default[0])
        ok = all(type(item) is element for item in value)
    if not ok:
        raise ConfigError(
            f"config key {_dotted(path)} must be {expected.__name__}, got {type(value).__name__}"
        )


def _paths(values: list[str]) -> tuple[Path, ...]:
    return tuple(Path(v).expanduser() for v in values)


def _build(raw: dict[str, Any], source: ConfigSource, path: Path | None, data_dir: Path) -> Config:
    ingest, signals, outcomes, grading = (
        raw["ingest"],
        raw["signals"],
        raw["outcomes"],
        raw["grading"],
    )
    if ingest["threads"] < 1:
        raise ConfigError("ingest.threads must be >= 1")
    _check_ticket_pattern(outcomes["ticket_pattern"])
    sources = ingest["sources"]
    return Config(
        source=source,
        path=path,
        data_dir=data_dir,
        ingest=IngestConfig(
            workers=ingest["workers"],
            threads=ingest["threads"],
            memory_limit=ingest["memory_limit"],
            batch_rows=ingest["batch_rows"],
            sources=SourceRoots(
                crush_projects=Path(sources["crush_projects"]).expanduser(),
                claude_projects=Path(sources["claude_projects"]).expanduser(),
                codex_sessions=Path(sources["codex_sessions"]).expanduser(),
                droid_sessions=Path(sources["droid_sessions"]).expanduser(),
                pickle_sessions=_paths(sources["pickle_sessions"]),
                skill_roots=_paths(sources["skill_roots"]),
                plugin_roots=_paths(sources["plugin_roots"]),
            ),
        ),
        signals=SignalsConfig(
            rerun_min=signals["rerun_min"],
            churn_min=signals["churn_min"],
            abandon_window=signals["abandon_window"],
            orphan_hours=signals["orphan_hours"],
            correction_lexicon=tuple(signals["correction_lexicon"]),
        ),
        pricing={
            model: ModelPrice(**{k: float(v) for k, v in price.items()})
            for model, price in raw["pricing"]["models"].items()
        },
        outcomes=OutcomesConfig(**outcomes),
        grading=GradingConfig(
            backend=grading["backend"],
            chunk_tokens=grading["chunk_tokens"],
            chunk_overlap=grading["chunk_overlap"],
            badness_weights=BadnessWeights(
                **{k: float(v) for k, v in grading["badness_weights"].items()}
            ),
        ),
    )
