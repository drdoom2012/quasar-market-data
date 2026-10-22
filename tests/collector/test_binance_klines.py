import pytest

from quasar_market_data.collector.binance_klines import VENUE_BINANCE_USDM, KlineParseError, parse_klines
from quasar_market_data.domain.candle import Candle, CandleKey
from quasar_market_data.domain.timeframe import Timeframe

SYMBOL = "BTC/USDT:USDT"
M5 = Timeframe("5m")
OPEN_MS = 1792676700000
STEP_MS = 300_000


def row(open_time: object = OPEN_MS, **overrides: object) -> list[object]:
    """A 5m Binance kline row opening at ``open_time``; ``overrides`` replace slots by name."""
    slots: dict[str, object] = {
        "open_time": open_time,
        "open": "81700.80",
        "high": "81700.80",
        "low": "81454.70",
        "close": "81555.90",
        "volume": "2123.997",
        "close_time": open_time + STEP_MS - 1 if type(open_time) is int else 0,
        "turnover": "173139160.77130",
        "trades": 34093,
        "taker_base": "811.912",
        "taker_quote": "66183389.55020",
        "ignore": "0",
    }
    unknown = set(overrides) - set(slots)
    assert not unknown, unknown
    slots.update(overrides)
    return list(slots.values())


ROW = [
    1792676700000,
    "81700.80",
    "81700.80",
    "81454.70",
    "81555.90",
    "2123.997",
    1792676999999,
    "173139160.77130",
    34093,
    "811.912",
    "66183389.55020",
    "0",
]


def test_parses_a_row_into_a_candle() -> None:
    assert parse_klines([ROW], SYMBOL, M5) == (
        Candle(
            key=CandleKey(VENUE_BINANCE_USDM, SYMBOL, M5, OPEN_MS),
            open="81700.8",
            high="81700.8",
            low="81454.7",
            close="81555.9",
            volume="2123.997",
            turnover="173139160.7713",
        ),
    )
    assert VENUE_BINANCE_USDM == "binance-usdm"


def test_values_are_canonical_decimal_text() -> None:
    (candle,) = parse_klines([ROW], SYMBOL, M5)
    assert (candle.open, candle.low, candle.turnover) == ("81700.8", "81454.7", "173139160.7713")


def test_empty_batch_gives_no_candles() -> None:
    assert parse_klines([], SYMBOL, M5) == ()


def test_tuple_rows_are_accepted() -> None:
    assert parse_klines([tuple(ROW)], SYMBOL, M5) == parse_klines([ROW], SYMBOL, M5)


def test_valid_batch_keeps_input_order() -> None:
    rows = [row(OPEN_MS + i * STEP_MS) for i in range(3)]
    candles = parse_klines(rows, SYMBOL, M5)
    assert [c.key.open_time_ms for c in candles] == [OPEN_MS, OPEN_MS + STEP_MS, OPEN_MS + 2 * STEP_MS]


def test_gaps_between_rows_are_accepted() -> None:
    candles = parse_klines([row(OPEN_MS), row(OPEN_MS + 5 * STEP_MS)], SYMBOL, M5)
    assert [c.key.open_time_ms for c in candles] == [OPEN_MS, OPEN_MS + 5 * STEP_MS]


MALFORMED = [
    pytest.param(row()[:11], id="eleven_items"),
    pytest.param([*row(), "x"], id="thirteen_items"),
    pytest.param({"open_time": OPEN_MS}, id="row_is_dict"),
    pytest.param("not a row", id="row_is_str"),
    pytest.param(row(float(OPEN_MS)), id="open_time_float"),
    pytest.param(row(True), id="open_time_bool"),
    pytest.param(row(str(OPEN_MS)), id="open_time_str"),
    pytest.param(row(close_time=OPEN_MS + STEP_MS - 2), id="close_time_one_early"),
    pytest.param(row(close_time=OPEN_MS + STEP_MS), id="close_time_one_late"),
    pytest.param(row(close_time=float(OPEN_MS + STEP_MS - 1)), id="close_time_float"),
    pytest.param(row(close_time=str(OPEN_MS + STEP_MS - 1)), id="close_time_str"),
    pytest.param(row(open=81700.8), id="open_float"),
    pytest.param(row(high=81700), id="high_int"),
    pytest.param(row(low=None), id="low_none"),
    pytest.param(row(close=81555.9), id="close_float"),
    pytest.param(row(volume=2123.997), id="volume_float"),
    pytest.param(row(turnover=173139160.7713), id="turnover_float"),
    pytest.param(row(open="abc"), id="open_text_abc"),
    pytest.param(row(high="1e5x"), id="high_text_1e5x"),
    pytest.param(row(OPEN_MS + 1), id="open_time_misaligned"),
]


@pytest.mark.parametrize("bad_row", MALFORMED)
def test_malformed_batch_is_rejected_entirely(bad_row: object) -> None:
    good = row(OPEN_MS - STEP_MS)
    with pytest.raises(KlineParseError) as caught:
        parse_klines([good, bad_row, row(OPEN_MS + STEP_MS)], SYMBOL, M5)
    assert caught.value.row_index == 1


def test_error_in_the_first_row_names_row_zero() -> None:
    with pytest.raises(KlineParseError) as caught:
        parse_klines([row(open=81700.8)], SYMBOL, M5)
    assert caught.value.row_index == 0


@pytest.mark.parametrize(
    ("second_open", "case"),
    [(OPEN_MS, "duplicate"), (OPEN_MS - STEP_MS, "descending")],
)
def test_non_increasing_open_time_is_rejected(second_open: int, case: str) -> None:
    with pytest.raises(KlineParseError) as caught:
        parse_klines([row(OPEN_MS), row(second_open)], SYMBOL, M5)
    assert caught.value.row_index == 1, case


def test_construction_errors_are_chained() -> None:
    with pytest.raises(KlineParseError) as caught:
        parse_klines([row(open="abc")], SYMBOL, M5)
    assert isinstance(caught.value.__cause__, ValueError | TypeError)


def test_invalid_symbol_surfaces_as_parse_error_with_chained_cause() -> None:
    with pytest.raises(KlineParseError) as caught:
        parse_klines([ROW], "", M5)
    assert isinstance(caught.value.__cause__, ValueError)


def test_empty_batch_does_not_check_the_symbol() -> None:
    # Nothing is constructed for an empty batch, so a bad symbol goes unnoticed here by design.
    assert parse_klines([], "", M5) == ()


def test_parser_does_not_judge_content() -> None:
    # high < low parses; validate_candle reports it later.
    (candle,) = parse_klines([row(high="1", low="2")], SYMBOL, M5)
    assert (candle.high, candle.low) == ("1", "2")
