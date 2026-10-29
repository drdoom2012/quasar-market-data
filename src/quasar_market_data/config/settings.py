"""Typed contents of the configuration file and its pure parser (spec S20 5.1, 5.2).

``parse_settings`` does no I/O: it takes the file text and the absolute directory that contains the file. It
imports only the standard library and ``domain``. Every key is required, unknown keys are an error, and the
first problem is reported with its key path.
"""

import hashlib
import os
import re
import tomllib
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from quasar_market_data.domain.reconcile import steady_state_limit_needed
from quasar_market_data.domain.timeframe import Timeframe

SUPPORTED_VENUE: Final = "binance-usdm"
SUPPORTED_TIMEFRAMES: Final = ("5m",)
MAX_LIMIT: Final = 1000
MAX_BACKFILL_DAYS: Final = 365
MAX_PUBLICATION_DELAY_MS: Final = 300_000
MAX_LAG_THRESHOLD_MS: Final = 900_000
MAX_SYMBOL_CHARS: Final = 64
DEFAULT_CONFIG_PATH: Final = Path("config/quasar.toml")

_SYMBOL: Final = re.compile(r"[^/:\s\x00-\x1f\x7f]+/USDT:USDT")


class ConfigError(ValueError):
    def __init__(self, key: str, problem: str) -> None:
        super().__init__(f"{key}: {problem}")
        self.key = key
        self.problem = problem


@dataclass(frozen=True, order=True)
class Instrument:
    venue: str
    symbol: str
    timeframe: str


@dataclass(frozen=True)
class Rule:
    publication_delay_ms: int
    lag_threshold_ms: int


@dataclass(frozen=True)
class Venue:
    id: str
    timeframes: tuple[str, ...]
    symbols: tuple[str, ...]


@dataclass(frozen=True)
class Backfill:
    enabled: bool
    days: int


@dataclass(frozen=True)
class CollectorSettings:
    limit: int
    max_concurrency: int
    min_request_interval_ms: int
    backfill: Backfill


@dataclass(frozen=True)
class ApiSettings:
    host: str
    port: int


@dataclass(frozen=True)
class Settings:
    database_path: Path
    rule: Rule
    venues: tuple[Venue, ...]
    collector: CollectorSettings
    api: ApiSettings

    def instruments(self) -> tuple[Instrument, ...]:
        return tuple(
            sorted(
                Instrument(v.id, symbol, timeframe)
                for v in self.venues
                for timeframe in v.timeframes
                for symbol in v.symbols
            )
        )


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


def _kind(value: object) -> str:
    return type(value).__name__


def _check_keys(table: dict[str, Any], path: str, allowed: Collection[str]) -> None:
    for name in table:
        if name not in allowed:
            raise ConfigError(_join(path, name), "unknown key")


def _get(table: dict[str, Any], path: str, name: str) -> Any:
    if name not in table:
        raise ConfigError(_join(path, name), "missing")
    return table[name]


def _table(parent: dict[str, Any], path: str, name: str) -> dict[str, Any]:
    value = _get(parent, path, name)
    if not isinstance(value, dict):
        raise ConfigError(_join(path, name), f"must be a table, got {_kind(value)}")
    return value


def _int(table: dict[str, Any], path: str, name: str, low: int, high: int | None = None) -> int:
    value = _get(table, path, name)
    key = _join(path, name)
    if type(value) is not int:  # bool is a subclass of int and must not pass
        raise ConfigError(key, f"must be an integer, got {_kind(value)}")
    if value < low or (high is not None and value > high):
        bound = f">= {low}" if high is None else f"in {low}..{high}"
        raise ConfigError(key, f"must be {bound}, got {value}")
    return value


def _bool(table: dict[str, Any], path: str, name: str) -> bool:
    value = _get(table, path, name)
    if type(value) is not bool:
        raise ConfigError(_join(path, name), f"must be true or false, got {_kind(value)}")
    return value


def _str(table: dict[str, Any], path: str, name: str) -> str:
    value = _get(table, path, name)
    if type(value) is not str:
        raise ConfigError(_join(path, name), f"must be a string, got {_kind(value)}")
    return value


def _str_array(table: dict[str, Any], path: str, name: str) -> tuple[str, ...]:
    value = _get(table, path, name)
    key = _join(path, name)
    if not isinstance(value, list):
        raise ConfigError(key, f"must be an array of strings, got {_kind(value)}")
    for index, item in enumerate(value):
        if type(item) is not str:
            raise ConfigError(f"{key}[{index}]", f"must be a string, got {_kind(item)}")
    return tuple(value)


def _database_path(root: dict[str, Any], base_dir: Path) -> Path:
    table = _table(root, "", "database")
    _check_keys(table, "database", {"path"})
    raw = _str(table, "database", "path")
    if raw == "":
        raise ConfigError("database.path", "must not be empty")
    if "\x00" in raw:
        raise ConfigError("database.path", "must not contain a NUL character")
    return Path(os.path.normpath(base_dir / raw))  # an absolute raw replaces base_dir; no ~ expansion


def _rule(root: dict[str, Any]) -> Rule:
    table = _table(root, "", "rule")
    _check_keys(table, "rule", {"publication_delay_ms", "lag_threshold_ms"})
    return Rule(
        _int(table, "rule", "publication_delay_ms", 0, MAX_PUBLICATION_DELAY_MS),
        _int(table, "rule", "lag_threshold_ms", 0, MAX_LAG_THRESHOLD_MS),
    )


def _venue(entry: object, index: int) -> Venue:
    path = f"venues[{index}]"
    if not isinstance(entry, dict):
        raise ConfigError(path, f"must be a table, got {_kind(entry)}")
    _check_keys(entry, path, {"id", "timeframes", "symbols"})
    venue_id = _str(entry, path, "id")
    if venue_id != SUPPORTED_VENUE:
        raise ConfigError(f"{path}.id", f"{venue_id!r} is not supported yet (S22); only {SUPPORTED_VENUE!r}")
    timeframes = _str_array(entry, path, "timeframes")
    if timeframes != SUPPORTED_TIMEFRAMES:
        raise ConfigError(
            f"{path}.timeframes", f"{list(timeframes)} is not supported yet (S22); only {list(SUPPORTED_TIMEFRAMES)}"
        )
    symbols = _str_array(entry, path, "symbols")
    key = f"{path}.symbols"
    if not symbols:
        raise ConfigError(key, "must not be empty")
    for index_s, symbol in enumerate(symbols):
        if len(symbol) > MAX_SYMBOL_CHARS or _SYMBOL.fullmatch(symbol) is None:
            raise ConfigError(f"{key}[{index_s}]", f"{symbol!r} is not of the form BASE/USDT:USDT")
    if len(set(symbols)) != len(symbols):
        raise ConfigError(key, "must not contain duplicates")
    return Venue(venue_id, timeframes, symbols)


def _venues(root: dict[str, Any]) -> tuple[Venue, ...]:
    value = _get(root, "", "venues")
    if not isinstance(value, list):
        raise ConfigError("venues", f"must be an array of tables, got {_kind(value)}")
    if not value:
        raise ConfigError("venues", "must list exactly one venue, got none")
    if len(value) > 1:
        raise ConfigError("venues", f"{len(value)} venues is not supported yet (S22); exactly one")
    return (_venue(value[0], 0),)


def _collector(root: dict[str, Any]) -> CollectorSettings:
    table = _table(root, "", "collector")
    _check_keys(table, "collector", {"limit", "max_concurrency", "min_request_interval_ms", "backfill"})
    limit = _int(table, "collector", "limit", 1, MAX_LIMIT)
    max_concurrency = _int(table, "collector", "max_concurrency", 1)
    interval = _int(table, "collector", "min_request_interval_ms", 0)
    backfill_table = _table(table, "collector", "backfill")
    _check_keys(backfill_table, "collector.backfill", {"enabled", "days"})
    backfill = Backfill(
        _bool(backfill_table, "collector.backfill", "enabled"),
        _int(backfill_table, "collector.backfill", "days", 1, MAX_BACKFILL_DAYS),
    )
    return CollectorSettings(limit, max_concurrency, interval, backfill)


def _api(root: dict[str, Any]) -> ApiSettings:
    table = _table(root, "", "api")
    _check_keys(table, "api", {"host", "port"})
    host = _str(table, "api", "host")
    if host == "":
        raise ConfigError("api.host", "must not be empty")
    return ApiSettings(host, _int(table, "api", "port", 1, 65535))


def parse_settings(text: str, *, base_dir: Path) -> Settings:
    if not base_dir.is_absolute():
        raise ValueError(f"base_dir must be absolute, got {base_dir}")
    try:
        root = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("file", f"not valid TOML: {exc}") from exc
    _check_keys(root, "", {"database", "rule", "venues", "collector", "api"})
    database_path = _database_path(root, base_dir)
    rule = _rule(root)
    venues = _venues(root)
    collector = _collector(root)
    api = _api(root)
    needed = steady_state_limit_needed(rule.publication_delay_ms, Timeframe(SUPPORTED_TIMEFRAMES[0]).milliseconds)
    if collector.limit < needed:
        raise ConfigError(
            "collector.limit", f"publication_delay_ms {rule.publication_delay_ms} needs limit >= {needed}"
        )
    return Settings(database_path, rule, venues, collector, api)


@dataclass(frozen=True)
class LoadedSettings:
    path: Path
    sha256: str
    settings: Settings

    def start_line(self) -> str:
        return f"config path={self.path} sha256={self.sha256} database={self.settings.database_path}"


def load_settings(path: Path) -> LoadedSettings:
    """Read the file once, hash those bytes, parse them; relative paths resolve against the real file's directory."""
    resolved = path
    try:
        resolved = path.resolve()
        raw = resolved.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, RuntimeError, ValueError) as exc:  # RuntimeError: a symlink loop; ValueError: an embedded NUL
        reason = (exc.strerror or str(exc)) if isinstance(exc, OSError) else str(exc)
        raise ConfigError("file", f"cannot read {resolved}: {reason}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError("file", f"{resolved} is not valid UTF-8: {exc}") from exc
    settings = parse_settings(text, base_dir=resolved.parent)
    parent = settings.database_path.parent
    if not parent.is_dir():
        raise ConfigError("database.path", f"directory {parent} does not exist")
    return LoadedSettings(resolved, hashlib.sha256(raw).hexdigest(), settings)
