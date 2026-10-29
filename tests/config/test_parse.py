import re
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.api import rule
from quasar_market_data.collector import binance_klines, fetch_once
from quasar_market_data.config import settings
from quasar_market_data.config.settings import (
    ApiSettings,
    Backfill,
    CollectorSettings,
    ConfigError,
    Instrument,
    Rule,
    Venue,
    parse_settings,
)
from quasar_market_data.domain.reconcile import steady_state_limit_needed
from quasar_market_data.domain.timeframe import Timeframe
from tests.config_files import config_text

BASE = Path("/cfg")
VALID = config_text()

LEAF_LINES = [
    ("database.path", 'path = "market.db"\n'),
    ("rule.publication_delay_ms", "publication_delay_ms = 15000\n"),
    ("rule.lag_threshold_ms", "lag_threshold_ms = 60000\n"),
    ("venues[0].id", 'id = "binance-usdm"\n'),
    ("venues[0].timeframes", 'timeframes = ["5m"]\n'),
    ("venues[0].symbols", 'symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT"]\n'),
    ("collector.limit", "limit = 100\n"),
    ("collector.max_concurrency", "max_concurrency = 4\n"),
    ("collector.min_request_interval_ms", "min_request_interval_ms = 100\n"),
    ("collector.backfill.enabled", "enabled = true\n"),
    ("collector.backfill.days", "days = 30\n"),
    ("api.host", 'host = "127.0.0.1"\n'),
    ("api.port", "port = 8000\n"),
]


def fails(text: str) -> ConfigError:
    with pytest.raises(ConfigError) as e:
        parse_settings(text, base_dir=BASE)
    return e.value


def needed(delay: int) -> int:
    return steady_state_limit_needed(delay, Timeframe("5m").milliseconds)


def test_valid_text_parses_to_every_value() -> None:
    s = parse_settings(VALID, base_dir=BASE)
    assert s.database_path == Path("/cfg/market.db")
    assert s.rule == Rule(15_000, 60_000)
    assert s.venues == (Venue("binance-usdm", ("5m",), ("BTC/USDT:USDT", "ETH/USDT:USDT")),)
    assert s.collector == CollectorSettings(100, 4, 100, Backfill(True, 30))
    assert s.api == ApiSettings("127.0.0.1", 8000)


def test_config_error_message_is_key_and_problem() -> None:
    e = ConfigError("a.b", "broken")
    assert (e.key, e.problem, str(e)) == ("a.b", "broken", "a.b: broken")


def test_a_non_absolute_base_dir_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_settings(VALID, base_dir=Path("cfg"))


def test_instruments_are_sorted_by_triple() -> None:
    s = parse_settings(config_text(symbols=("ETH/USDT:USDT", "BTC/USDT:USDT")), base_dir=BASE)
    assert s.instruments() == (
        Instrument("binance-usdm", "BTC/USDT:USDT", "5m"),
        Instrument("binance-usdm", "ETH/USDT:USDT", "5m"),
    )


@pytest.mark.parametrize("key, line", LEAF_LINES)
def test_every_key_is_required(key: str, line: str) -> None:
    assert VALID.count(line) == 1
    e = fails(VALID.replace(line, "", 1))
    assert e.key == key and "missing" in e.problem


TABLES = {
    "database": "[database]\n" + 'path = "market.db"\n\n',
    "rule": "[rule]\npublication_delay_ms = 15000\nlag_threshold_ms = 60000\n\n",
    "collector.backfill": "[collector.backfill]\nenabled = true\ndays = 30\n\n",
}


@pytest.mark.parametrize("table", ["database", "rule", "collector", "collector.backfill", "api", "venues"])
def test_a_missing_table_names_it(table: str) -> None:
    if table == "collector":
        text = re.sub(r"\[collector\].*?\[api\]", "[api]", VALID, flags=re.DOTALL)
    elif table == "venues":
        text = re.sub(r"\[\[venues\]\].*?\n\n", "", VALID, count=1, flags=re.DOTALL)
    elif table == "api":
        text = VALID.split("[api]")[0]
    else:
        assert TABLES[table] in VALID
        text = VALID.replace(TABLES[table], "")
    assert text != VALID
    assert fails(text).key == table


@pytest.mark.parametrize(
    "where, key",
    [
        ("[database]", "database.extra"),
        ("[rule]", "rule.extra"),
        ("[[venues]]", "venues[0].extra"),
        ("[collector]", "collector.extra"),
        ("[collector.backfill]", "collector.backfill.extra"),
        ("[api]", "api.extra"),
        ("", "extra"),
    ],
)
def test_an_unknown_key_is_an_error(where: str, key: str) -> None:
    text = "extra = 1\n" + VALID if where == "" else VALID.replace(where + "\n", where + "\nextra = 1\n", 1)
    assert fails(text).key == key


def test_an_unknown_key_is_reported_before_a_missing_one() -> None:
    text = VALID.replace("limit = 100\n", "extra = 1\n", 1)
    assert fails(text).key == "collector.extra"


@pytest.mark.parametrize(
    "values, key",
    [
        ({"limit": 0}, "collector.limit"),
        ({"limit": 1001}, "collector.limit"),
        ({"backfill_days": 0}, "collector.backfill.days"),
        ({"backfill_days": 366}, "collector.backfill.days"),
        ({"port": 0}, "api.port"),
        ({"port": 65536}, "api.port"),
        ({"publication_delay_ms": -1}, "rule.publication_delay_ms"),
        ({"publication_delay_ms": 300_001}, "rule.publication_delay_ms"),
        ({"lag_threshold_ms": -1}, "rule.lag_threshold_ms"),
        ({"lag_threshold_ms": 900_001}, "rule.lag_threshold_ms"),
        ({"max_concurrency": 0}, "collector.max_concurrency"),
        ({"min_request_interval_ms": -1}, "collector.min_request_interval_ms"),
        ({"host": ""}, "api.host"),
    ],
)
def test_out_of_bound_values(values: dict[str, Any], key: str) -> None:
    assert fails(config_text(**values)).key == key


def test_the_bounds_themselves_are_valid() -> None:
    parse_settings(config_text(limit=1000), base_dir=BASE)
    parse_settings(config_text(backfill_days=1), base_dir=BASE)
    parse_settings(config_text(backfill_days=365), base_dir=BASE)
    parse_settings(config_text(port=1), base_dir=BASE)
    parse_settings(config_text(port=65535), base_dir=BASE)
    parse_settings(config_text(lag_threshold_ms=0), base_dir=BASE)
    parse_settings(config_text(lag_threshold_ms=900_000), base_dir=BASE)
    parse_settings(config_text(publication_delay_ms=0, limit=needed(0)), base_dir=BASE)
    parse_settings(config_text(publication_delay_ms=300_000, limit=needed(300_000)), base_dir=BASE)
    parse_settings(config_text(max_concurrency=1, min_request_interval_ms=0), base_dir=BASE)


@pytest.mark.parametrize("delay", [0, 1, 15_000, 299_999, 300_000])
def test_the_smallest_limit_a_delay_needs_is_accepted_and_one_less_is_refused(delay: int) -> None:
    parse_settings(config_text(publication_delay_ms=delay, limit=needed(delay)), base_dir=BASE)
    e = fails(config_text(publication_delay_ms=delay, limit=needed(delay) - 1))
    assert e.key == "collector.limit" and f"needs limit >= {needed(delay)}" in e.problem


def test_the_largest_delay_with_the_largest_limit_is_accepted() -> None:
    parse_settings(config_text(publication_delay_ms=300_000, limit=1000), base_dir=BASE)


def test_needed_grows_with_the_delay() -> None:
    assert needed(0) < needed(1) and needed(1) <= needed(300_000) <= 1000


@pytest.mark.parametrize(
    "old, new, key",
    [
        ("port = 8000", "port = true", "api.port"),
        ("port = 8000", 'port = "8000"', "api.port"),
        ("limit = 100", "limit = 100.0", "collector.limit"),
        ("limit = 100", "limit = true", "collector.limit"),
        ("enabled = true", 'enabled = "true"', "collector.backfill.enabled"),
        ("days = 30", "days = 30.5", "collector.backfill.days"),
        ("enabled = true", "enabled = 1", "collector.backfill.enabled"),
        ('host = "127.0.0.1"', "host = 1", "api.host"),
        ('path = "market.db"', "path = 1", "database.path"),
        ("publication_delay_ms = 15000", "publication_delay_ms = false", "rule.publication_delay_ms"),
        ("lag_threshold_ms = 60000", "lag_threshold_ms = 6e4", "rule.lag_threshold_ms"),
        ("max_concurrency = 4", "max_concurrency = true", "collector.max_concurrency"),
        ('id = "binance-usdm"', "id = 1", "venues[0].id"),
        ('timeframes = ["5m"]', 'timeframes = "5m"', "venues[0].timeframes"),
        ('timeframes = ["5m"]', "timeframes = [5]", "venues[0].timeframes[0]"),
    ],
)
def test_wrong_types(old: str, new: str, key: str) -> None:
    assert old in VALID
    assert fails(VALID.replace(old, new, 1)).key == key


def test_a_table_given_as_a_scalar_is_an_error() -> None:
    without_api = VALID.split("[api]")[0]
    assert fails("api = 1\n" + without_api).key == "api"
    assert fails("api = []\n" + without_api).key == "api"


def test_venues_must_be_an_array_of_tables() -> None:
    text = re.sub(r"\[\[venues\]\].*?\n\n", "", VALID, count=1, flags=re.DOTALL)
    assert fails("venues = 1\n" + text).key == "venues"
    assert fails("venues = [1]\n" + text).key == "venues[0]"


def test_symbols_must_be_a_non_empty_array_without_duplicates() -> None:
    symbols_line = 'symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT"]'
    assert fails(VALID.replace(symbols_line, "symbols = []")).key == "venues[0].symbols"
    assert fails(VALID.replace(symbols_line, 'symbols = "BTC/USDT:USDT"')).key == "venues[0].symbols"
    assert fails(config_text(symbols=("BTC/USDT:USDT", "BTC/USDT:USDT"))).key == "venues[0].symbols"
    e = fails(VALID.replace(symbols_line, "symbols = [1]"))
    assert e.key == "venues[0].symbols[0]"


@pytest.mark.parametrize("symbol", ["1000PEPE/USDT:USDT", "我踏马来了/USDT:USDT", "BTC/USDT:USDT"])
def test_canonical_usdt_symbols_load(symbol: str) -> None:
    s = parse_settings(config_text(symbols=(symbol,)), base_dir=BASE)
    assert s.venues[0].symbols == (symbol,)


@pytest.mark.parametrize(
    "symbol",
    [
        "BTC/USDT",
        "BTC/USDC:USDC",
        "BTC/USDT:USDC",
        "BTC USDT/USDT:USDT",
        "BTC/USDT:USDT ",
        "/USDT:USDT",
        "A" * 55 + "/USDT:USDT",
        "BTC/USDT:USDT\n",
        "",
        "BTC/USDT:USDT\x00",
        "BTC/USDT:USDT\t",
        "BTC:X/USDT:USDT",
        "BTC/ETH/USDT:USDT",
        "btc/usdt:usdt",
    ],
)
def test_non_canonical_symbols_are_refused(symbol: str) -> None:
    assert fails(config_text(symbols=(symbol,))).key == "venues[0].symbols[0]"


def test_a_symbol_of_exactly_the_maximum_length_loads() -> None:
    symbol = "A" * (settings.MAX_SYMBOL_CHARS - len("/USDT:USDT")) + "/USDT:USDT"
    assert len(symbol) == settings.MAX_SYMBOL_CHARS
    parse_settings(config_text(symbols=(symbol,)), base_dir=BASE)


def test_the_second_bad_symbol_is_indexed() -> None:
    assert fails(config_text(symbols=("BTC/USDT:USDT", "ETH/USDT"))).key == "venues[0].symbols[1]"


def test_a_second_venue_is_not_supported_yet() -> None:
    second = '[[venues]]\nid = "bybit"\ntimeframes = ["5m"]\nsymbols = ["BTC/USDT:USDT"]\n'
    e = fails(VALID + "\n" + second)
    assert e.key == "venues" and "not supported yet (S22)" in e.problem


def test_zero_venues_is_an_error() -> None:
    text = re.sub(r"\[\[venues\]\].*?\n\n", "", VALID, count=1, flags=re.DOTALL)
    e = fails("venues = []\n" + text)
    assert e.key == "venues"


@pytest.mark.parametrize("venue", ["bybit", "Binance-USDM", ""])
def test_another_venue_id_is_not_supported_yet(venue: str) -> None:
    e = fails(VALID.replace('id = "binance-usdm"', f'id = "{venue}"'))
    assert e.key == "venues[0].id" and "S22" in e.problem


@pytest.mark.parametrize("timeframes", ['["1m"]', '["5m", "1m"]', '["5m", "5m"]', "[]"])
def test_timeframes_other_than_5m_are_not_supported_yet(timeframes: str) -> None:
    e = fails(VALID.replace('timeframes = ["5m"]', f"timeframes = {timeframes}"))
    assert e.key == "venues[0].timeframes"


def test_a_duplicate_key_and_a_syntax_error_are_config_errors() -> None:
    assert fails(VALID.replace("[api]\n", "[api]\nport = 1\n", 1)).key == "file"
    assert fails("this is = = not toml").key == "file"
    assert fails("[database\n").key == "file"


def test_empty_text_reports_the_first_missing_table() -> None:
    e = fails("")
    assert e.key == "database" and "missing" in e.problem


@pytest.mark.parametrize(
    "path, expected",
    [
        ("../data/q.db", "/data/q.db"),
        ("a/./b/../q.db", "/cfg/a/q.db"),
        ("/abs/q.db", "/abs/q.db"),
        ("~/q.db", "/cfg/~/q.db"),
        ("../../../../q.db", "/q.db"),
        ("sp ace/q é.db", "/cfg/sp ace/q é.db"),
        ("//double/q.db", "//double/q.db"),
        ("a//b.db", "/cfg/a/b.db"),
    ],
)
def test_database_path_is_resolved_against_base_dir(path: str, expected: str) -> None:
    assert parse_settings(config_text(database_path=path), base_dir=BASE).database_path == Path(expected)


def test_the_resolved_path_is_independent_of_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert parse_settings(config_text(database_path="x.db"), base_dir=BASE).database_path == Path("/cfg/x.db")


@pytest.mark.parametrize("path", ["", "a\x00b.db"])
def test_an_empty_or_nul_database_path_is_an_error(path: str) -> None:
    assert fails(config_text(database_path=path)).key == "database.path"


def test_bounds_equal_the_switch_bounds_they_replace() -> None:
    assert settings.MAX_LIMIT == fetch_once.MAX_LIMIT
    assert settings.MAX_PUBLICATION_DELAY_MS == rule.MAX_PUBLICATION_DELAY_MS
    assert settings.MAX_LAG_THRESHOLD_MS == rule.MAX_LAG_THRESHOLD_MS
    assert settings.SUPPORTED_VENUE == binance_klines.VENUE_BINANCE_USDM
    assert settings.SUPPORTED_TIMEFRAMES == ("5m",)
    assert settings.MAX_BACKFILL_DAYS == 365
