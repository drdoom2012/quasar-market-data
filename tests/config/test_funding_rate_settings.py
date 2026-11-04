"""The optional ``[funding_rate]`` table (ADR-0004 P-12): fail closed, every key named in its error."""

from pathlib import Path

import pytest

from quasar_market_data.collector import fetch_funding_rate_once
from quasar_market_data.config.settings import ConfigError, FundingRateSettings, parse_settings
from quasar_market_data.domain.funding_rate import MAX_FUNDING_RATE_PUBLICATION_DELAY_MS
from tests.config_files import TEN_SYMBOLS, config_text

BASE = Path("/cfg")
REPO = Path(__file__).resolve().parents[2]

VALID_TABLE = {
    "enabled": "true",
    "publication_delay_ms": "30000",
    "round_offset_ms": "60000",
    "budget_per_5min": "150",
    "backfill_days": "1",
    "recheck_ms": "86400000",
}


def table(**overrides: str | None) -> str:
    values = {**VALID_TABLE}
    for key, value in overrides.items():
        if value is None:
            del values[key]
        else:
            values[key] = value
    return "[funding_rate]\n" + "".join(f"{key} = {value}\n" for key, value in values.items())


def symbols(count: int) -> tuple[str, ...]:
    return tuple(f"S{i:04d}/USDT:USDT" for i in range(count))


def text(table_text: str, symbol_list: tuple[str, ...] = TEN_SYMBOLS) -> str:
    return config_text(symbols=symbol_list) + "\n" + table_text


def parse(table_text: str, symbol_list: tuple[str, ...] = TEN_SYMBOLS) -> object:
    return parse_settings(text(table_text, symbol_list), base_dir=BASE).funding_rate


def fails(table_text: str, symbol_list: tuple[str, ...] = TEN_SYMBOLS) -> ConfigError:
    with pytest.raises(ConfigError) as error:
        parse_settings(text(table_text, symbol_list), base_dir=BASE)
    return error.value


def test_an_absent_table_means_disabled() -> None:
    assert parse_settings(config_text(), base_dir=BASE).funding_rate is None


def test_a_valid_table_is_parsed() -> None:
    assert parse(table()) == FundingRateSettings(True, 30_000, 60_000, 150, 1, 86_400_000)


def test_the_repository_configs_still_parse_with_funding_off() -> None:
    for name in ("quasar.toml", "quasar.compose.toml"):
        path = REPO / "config" / name
        if path.exists():
            assert parse_settings(path.read_text(), base_dir=REPO / "config").funding_rate is None


@pytest.mark.parametrize("key", list(VALID_TABLE))
def test_every_key_is_required(key: str) -> None:
    error = fails(table(**{key: None}))
    assert error.key == f"funding_rate.{key}" and "missing" in error.problem


def test_an_unknown_key_is_an_error() -> None:
    error = fails(table() + "extra = 1\n")
    assert error.key == "funding_rate.extra" and "unknown" in error.problem


def test_the_table_must_be_a_table() -> None:
    with pytest.raises(ConfigError) as raised:
        parse_settings("funding_rate = 3\n" + config_text(), base_dir=BASE)  # a top-level key goes before any table
    assert raised.value.key == "funding_rate" and "table" in raised.value.problem


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("budget_per_5min", "49"),
        ("budget_per_5min", "251"),
        ("budget_per_5min", '"150"'),
        ("budget_per_5min", "150.0"),
        ("budget_per_5min", "true"),
        ("backfill_days", "0"),
        ("backfill_days", "31"),  # ADR-0004 A-4: at most 30 days
        ("backfill_days", "365"),
        ("backfill_days", "3650"),
        ("backfill_days", "1.0"),
        ("recheck_ms", "3599999"),
        ("recheck_ms", "604800001"),
        ("round_offset_ms", "59999"),
        ("round_offset_ms", "600001"),
        ("publication_delay_ms", "-1"),
        ("publication_delay_ms", "595001"),
        ("enabled", "1"),
        ("enabled", '"true"'),
    ],
)
def test_out_of_range_and_wrongly_typed_values_name_the_key(key: str, value: str) -> None:
    error = fails(table(**{key: value}))
    assert error.key == f"funding_rate.{key}"


def test_the_backfill_depth_is_at_most_30_days_and_a_larger_one_is_a_configuration_error() -> None:
    assert parse(table(backfill_days="1")) is not None
    assert parse(table(backfill_days="30")) is not None
    error = fails(table(backfill_days="31"))
    assert error.key == "funding_rate.backfill_days" and "30" in error.problem
    assert fails(table(enabled="false", backfill_days="31")).key == "funding_rate.backfill_days"  # also when disabled


def test_the_offset_must_be_at_least_the_delay_plus_five_seconds() -> None:
    assert parse(table(publication_delay_ms="55000", round_offset_ms="60000")) is not None
    error = fails(table(publication_delay_ms="55001", round_offset_ms="60000"))
    assert error.key == "funding_rate.round_offset_ms" and "60001" in error.problem


def test_a_disabled_table_is_range_validated_but_never_capacity_refused() -> None:
    assert parse(table(enabled="false", budget_per_5min="50"), symbols(275)) is not None
    assert fails(table(enabled="false", budget_per_5min="49")).key == "funding_rate.budget_per_5min"


def test_an_enabled_table_above_the_sustainable_capacity_is_refused_naming_the_values() -> None:
    assert parse(table(budget_per_5min="50"), symbols(274)) is not None  # S_sustained = 274
    error = fails(table(budget_per_5min="50"), symbols(275))
    assert error.key == "funding_rate.budget_per_5min"
    for text_ in ("275", "274", "50", "60000"):
        assert text_ in error.problem


def test_the_capacity_warning_appears_above_ninety_percent_only_when_enabled() -> None:
    at = parse_settings(text(table(budget_per_5min="50"), symbols(246)), base_dir=BASE)
    above = parse_settings(text(table(budget_per_5min="50"), symbols(247)), base_dir=BASE)
    off = parse_settings(text(table(enabled="false", budget_per_5min="50"), symbols(247)), base_dir=BASE)
    assert at.funding_rate_capacity_warning() is None
    warning = above.funding_rate_capacity_warning()
    assert warning is not None and "247" in warning and "246" in warning
    assert off.funding_rate_capacity_warning() is None
    assert parse_settings(config_text(), base_dir=BASE).funding_rate_capacity_warning() is None


def test_the_delay_maximum_is_one_constant_shared_with_the_command() -> None:
    assert MAX_FUNDING_RATE_PUBLICATION_DELAY_MS == 595_000
    assert fetch_funding_rate_once.MAX_PUBLICATION_DELAY_MS == MAX_FUNDING_RATE_PUBLICATION_DELAY_MS
