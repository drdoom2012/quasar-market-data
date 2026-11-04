"""The optional ``[open_interest]`` table (ADR-0003 P-13): fail closed, every key named in its error."""

from pathlib import Path

import pytest

from quasar_market_data.config.settings import ConfigError, OpenInterestSettings, parse_settings
from tests.config_files import TEN_SYMBOLS, config_text

BASE = Path("/cfg")
REPO = Path(__file__).resolve().parents[2]

VALID_TABLE = {
    "enabled": "true",
    "period": '"5m"',
    "publication_delay_ms": "15000",
    "round_offset_ms": "200000",
    "budget_per_5min": "400",
    "backfill_days": "29",
}


def table(**overrides: str | None) -> str:
    values = {**VALID_TABLE}
    for key, value in overrides.items():
        if value is None:
            del values[key]
        else:
            values[key] = value
    return "[open_interest]\n" + "".join(f"{key} = {value}\n" for key, value in values.items())


def text(table_text: str, symbols: tuple[str, ...] = TEN_SYMBOLS) -> str:
    return config_text(symbols=symbols) + "\n" + table_text


def fails(text_: str) -> ConfigError:
    with pytest.raises(ConfigError) as error:
        parse_settings(text_, base_dir=BASE)
    return error.value


def test_an_absent_table_means_disabled() -> None:
    assert parse_settings(config_text(), base_dir=BASE).open_interest is None


def test_the_table_is_parsed() -> None:
    settings = parse_settings(text(table()), base_dir=BASE)
    assert settings.open_interest == OpenInterestSettings(True, "5m", 15_000, 200_000, 400, 29)


def test_a_disabled_table_is_parsed_and_still_validated() -> None:
    settings = parse_settings(text(table(enabled="false")), base_dir=BASE)
    assert settings.open_interest is not None and settings.open_interest.enabled is False
    assert fails(text(table(enabled="false", budget_per_5min="99"))).key == "open_interest.budget_per_5min"


@pytest.mark.parametrize("value", ["99", "401", "0", "-1", "250.0", '"400"', "true"])
def test_budget_per_5min_outside_the_contract_is_rejected_by_key(value: str) -> None:
    assert fails(text(table(budget_per_5min=value))).key == "open_interest.budget_per_5min"


@pytest.mark.parametrize("value", ["100", "400", "250"])
def test_budget_per_5min_inside_the_contract_is_accepted(value: str) -> None:
    settings = parse_settings(text(table(budget_per_5min=value)), base_dir=BASE)
    assert settings.open_interest is not None and settings.open_interest.budget_per_5min == int(value)


@pytest.mark.parametrize("value", ["0", "31", "365", "-1", "29.0", '"29"', "true"])
def test_a_backfill_depth_outside_1_to_30_days_is_a_configuration_error(value: str) -> None:
    error = fails(text(table(backfill_days=value)))  # ADR-0003 A-4: the process does not start
    assert error.key == "open_interest.backfill_days"


@pytest.mark.parametrize("value", ["1", "29", "30"])
def test_a_backfill_depth_of_1_to_30_days_is_accepted(value: str) -> None:
    settings = parse_settings(text(table(backfill_days=value)), base_dir=BASE)
    assert settings.open_interest is not None and settings.open_interest.backfill_days == int(value)


def test_a_disabled_table_is_refused_too_when_the_depth_is_above_30_days() -> None:
    assert fails(text(table(enabled="false", backfill_days="31"))).key == "open_interest.backfill_days"


@pytest.mark.parametrize("key", list(VALID_TABLE))
def test_every_key_is_required(key: str) -> None:
    error = fails(text(table(**{key: None})))
    assert (error.key, error.problem) == (f"open_interest.{key}", "missing")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("round_offset_ms", "59999"),
        ("round_offset_ms", "280001"),
        ("round_offset_ms", "1.5"),
        ("publication_delay_ms", "-1"),
        ("publication_delay_ms", "300001"),
        ("period", '"15m"'),
        ("period", "5"),
        ("enabled", '"yes"'),
        ("enabled", "1"),
    ],
)
def test_a_value_outside_its_contract_is_rejected_by_key(key: str, value: str) -> None:
    assert fails(text(table(**{key: value}))).key == f"open_interest.{key}"


@pytest.mark.parametrize(("key", "value"), [("round_offset_ms", "60000"), ("round_offset_ms", "280000")])
def test_the_offset_bounds_are_accepted(key: str, value: str) -> None:
    assert parse_settings(text(table(**{key: value})), base_dir=BASE).open_interest is not None


def test_an_unknown_key_is_rejected_by_key() -> None:
    error = fails(text(table() + "symbols = []\n"))
    assert (error.key, error.problem) == ("open_interest.symbols", "unknown key")


def test_the_table_must_be_a_table() -> None:
    assert fails("open_interest = 5\n" + config_text()).key == "open_interest"


def symbols(count: int) -> tuple[str, ...]:
    return tuple(f"S{index}/USDT:USDT" for index in range(count))


@pytest.mark.parametrize(
    ("count", "budget", "accepted"), [(432, 100, True), (433, 100, False), (1788, 400, True), (1789, 400, False)]
)
def test_the_cadence_limit_is_checked_and_names_the_computed_values(count: int, budget: int, accepted: bool) -> None:
    config = text(table(budget_per_5min=str(budget)), symbols(count))
    if accepted:
        assert parse_settings(config, base_dir=BASE).open_interest is not None
        return
    error = fails(config)
    assert error.key == "open_interest.budget_per_5min"
    assert str(count) in error.problem and "largest accepted symbol count" in error.problem


@pytest.mark.parametrize(
    ("count", "budget", "cadence"),
    [
        (10, 100, 1),
        (10, 200, 1),
        (10, 400, 1),
        (150, 400, 2),
        (150, 200, 3),
        (150, 100, 5),
        (600, 400, 5),
        (600, 200, 9),
    ],
)
def test_the_collector_derives_the_adr_cadence_from_the_parsed_file(count: int, budget: int, cadence: int) -> None:
    from quasar_market_data.domain.open_interest_cadence import open_interest_cadence

    settings = parse_settings(text(table(budget_per_5min=str(budget)), symbols(count)), base_dir=BASE)
    assert settings.open_interest is not None
    symbol_count = len(settings.venues[0].symbols)
    assert open_interest_cadence(symbol_count, settings.open_interest.budget_per_5min) == cadence


@pytest.mark.parametrize("name", ["quasar.toml", "quasar.compose.toml"])
def test_the_shipped_files_switch_open_interest_on(name: str) -> None:
    path = REPO / "config" / name
    parsed = parse_settings(path.read_text(encoding="utf-8"), base_dir=path.parent)
    assert parsed.open_interest == OpenInterestSettings(True, "5m", 15_000, 200_000, 400, 29)
