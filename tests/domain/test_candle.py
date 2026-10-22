import dataclasses
import json
from pathlib import Path

import pytest

from quasar_market_data.domain.candle import Candle, CandleKey
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, MIN_TIMESTAMP_MS
from tests.typing_helpers import untyped

MARKETS_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "binance" / "usdm_perpetual_markets.json"
SURVEYED_SYMBOLS = sorted(market["symbol"] for market in json.loads(MARKETS_FIXTURE.read_text("utf-8"))["markets"])

OPEN_MS = 1_700_000_100_000
TF = Timeframe("5m")


class _IntSubclass(int):
    pass


def make_key(
    *,
    venue: str = "binance-usdm",
    symbol: str = "BTC/USDT:USDT",
    timeframe: Timeframe = TF,
    open_time_ms: int = OPEN_MS,
) -> CandleKey:
    return CandleKey(venue=venue, symbol=symbol, timeframe=timeframe, open_time_ms=open_time_ms)


def make_candle(
    *,
    key: CandleKey | None = None,
    open: str = "100.50",
    high: str = "101",
    low: str = "99",
    close: str = "100",
    volume: str = "12.5",
    turnover: str | None = "1250",
) -> Candle:
    return Candle(
        key=key if key is not None else make_key(),
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
        turnover=turnover,
    )


def test_candle_canonicalizes_numeric_fields() -> None:
    assert make_candle(open="100.50").open == "100.5"


def test_candles_from_equivalent_decimal_text_are_equal() -> None:
    assert make_candle(open="100.50") == make_candle(open="100.5")


def test_turnover_none_stays_none() -> None:
    assert make_candle(turnover=None).turnover is None


def test_turnover_is_canonicalized() -> None:
    assert make_candle(turnover="1250.00").turnover == "1250"


def test_end_exclusive_ms() -> None:
    assert make_candle().end_exclusive_ms == OPEN_MS + 300_000


def test_candle_is_frozen() -> None:
    candle = make_candle()
    with pytest.raises(dataclasses.FrozenInstanceError):
        candle.open = "1"  # type: ignore[misc]


def test_key_is_frozen() -> None:
    key = make_key()
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.symbol = "ETH/USDT:USDT"  # type: ignore[misc]


def test_misaligned_open_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="aligned"):
        make_key(open_time_ms=1_700_000_100_001)


@pytest.mark.parametrize("bad", [MIN_TIMESTAMP_MS - 1, MAX_TIMESTAMP_MS + 1])
def test_out_of_range_open_time_is_rejected(bad: int) -> None:
    with pytest.raises(ValueError):
        make_key(open_time_ms=bad)


def test_open_time_whose_end_overflows_is_rejected() -> None:
    with pytest.raises(ValueError):
        make_key(open_time_ms=MAX_TIMESTAMP_MS)


def test_last_open_time_whose_end_fits_is_accepted() -> None:
    candle = make_candle(key=make_key(open_time_ms=MAX_TIMESTAMP_MS - 300_000))
    assert candle.end_exclusive_ms == MAX_TIMESTAMP_MS


@pytest.mark.parametrize("bad", [True, 1.0, _IntSubclass(OPEN_MS)])
def test_non_int_open_time_is_rejected(bad: object) -> None:
    with pytest.raises(TypeError):
        make_key(open_time_ms=untyped(bad))


def test_non_timeframe_is_rejected() -> None:
    with pytest.raises(TypeError):
        make_key(timeframe=untyped("5m"))


@pytest.mark.parametrize(
    "venue", ["", " ", "Binance", "binance usdm", "binance-usdm\n", "-binance", "a" * 33, "bïnance"]
)
def test_bad_venue_is_rejected(venue: str) -> None:
    with pytest.raises(ValueError):
        make_key(venue=venue)


@pytest.mark.parametrize("venue", [None, 5, b"binance"])
def test_non_str_venue_is_rejected(venue: object) -> None:
    with pytest.raises(TypeError):
        make_key(venue=untyped(venue))


@pytest.mark.parametrize("venue", ["binance-usdm", "binance_spot", "a", "0x", "a" * 32])
def test_good_venue_is_accepted(venue: str) -> None:
    assert make_key(venue=venue).venue == venue


@pytest.mark.parametrize(
    "symbol",
    [
        "",
        "  ",
        " BTC/USDT:USDT",
        "BTC/USDT:USDT ",
        "BTC USDT",
        "BTC/USDT\n",
        "BTC\tUSDT",
        "BTC\x00USDT",
        "BTC USDT",
        "A" * 65,
    ],
)
def test_bad_symbol_is_rejected(symbol: str) -> None:
    with pytest.raises(ValueError):
        make_key(symbol=symbol)


@pytest.mark.parametrize("symbol", [None, 5])
def test_non_str_symbol_is_rejected(symbol: object) -> None:
    with pytest.raises(TypeError):
        make_key(symbol=untyped(symbol))


@pytest.mark.parametrize(
    "symbol",
    ["BTC/USDT:USDT", "1000PEPE/USDT:USDT", "BTC/USDT", "币安人生/USDT:USDT", "哈基米/USDT:USDT", "A" * 64],
)
def test_good_symbol_is_accepted(symbol: str) -> None:
    assert make_key(symbol=symbol).symbol == symbol


def test_survey_fixture_is_not_empty_and_has_non_ascii_symbols() -> None:
    assert len(SURVEYED_SYMBOLS) > 100
    assert any(not symbol.isascii() for symbol in SURVEYED_SYMBOLS)


@pytest.mark.parametrize("symbol", SURVEYED_SYMBOLS)
def test_every_surveyed_symbol_is_accepted(symbol: str) -> None:
    assert make_key(symbol=symbol).symbol == symbol


def test_keys_differing_only_in_venue_are_distinct() -> None:
    perp = make_key(venue="binance-usdm")
    spot = make_key(venue="binance-spot")
    assert perp != spot
    assert len({perp: 1, spot: 2}) == 2


def test_keys_differing_only_in_symbol_are_distinct() -> None:
    spot = make_key(symbol="BTC/USDT")
    perp = make_key(symbol="BTC/USDT:USDT")
    assert spot != perp
    assert len({spot: 1, perp: 2}) == 2


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume", "turnover"])
def test_float_numeric_field_is_rejected(field: str) -> None:
    with pytest.raises(TypeError):
        make_candle(**{field: untyped(1.5)})


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_malformed_numeric_text_is_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        make_candle(**untyped({field: "abc"}))


def test_non_candle_key_is_rejected() -> None:
    with pytest.raises(TypeError):
        Candle(key=untyped("key"), open="1", high="1", low="1", close="1", volume="1", turnover=None)


def test_construction_does_not_validate_content() -> None:
    candle = make_candle(high="90", low="99")
    assert candle.high == "90"
    assert candle.low == "99"
