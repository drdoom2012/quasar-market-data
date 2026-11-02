from typing import Any

import pytest

from quasar_market_data.collector.binance_open_interest import OpenInterestParseError, parse_open_interest_hist
from quasar_market_data.domain.open_interest import OpenInterest, OpenInterestKey
from quasar_market_data.domain.timeframe import Timeframe
from tests.typing_helpers import untyped

P5 = Timeframe("5m")
START = 1_793_620_200_000
T = 1_793_620_800_000
END = 1_793_621_100_000  # period ends START, START+5m, T, END: 4 slots


def raw(timestamp: object = T, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": "BTCUSDT",
        "sumOpenInterest": "95677.77900000",
        "sumOpenInterestValue": "7446123150.67500000",
        "CMCCirculatingSupply": "19951234.00000000",
        "timestamp": timestamp,
    }
    row.update(overrides)
    return row


def without(key: str) -> dict[str, object]:
    row = raw()
    del row[key]
    return row


def parse(response: object, start: int = START, end: int = END) -> tuple[OpenInterest, ...]:
    return parse_open_interest_hist(
        response, market_id="BTCUSDT", symbol="BTC/USDT:USDT", period=P5, start_time_ms=start, end_time_ms=end
    )


def test_parses_a_row() -> None:
    expected = OpenInterest(OpenInterestKey("binance-usdm", "BTC/USDT:USDT", P5, T), "95677.779", "7446123150.675")
    assert parse([raw()]) == (expected,)
    assert parse([without("CMCCirculatingSupply")]) == (expected,)
    assert parse([raw(unknownKey="x")]) == (expected,)


def test_empty_response_is_valid() -> None:
    assert parse([]) == ()


def test_inclusive_bounds_and_gaps() -> None:
    assert [r.key.time_ms for r in parse([raw(START)])] == [START]
    assert [r.key.time_ms for r in parse([raw(END)])] == [END]
    full = [START, START + 300_000, T, END]
    assert [r.key.time_ms for r in parse([raw(ts) for ts in full])] == full
    assert [r.key.time_ms for r in parse([raw(START), raw(END)])] == [START, END]


FIRST = raw(START)

MALFORMED: list[Any] = [
    # index 0: no previous row, so the ordering check cannot mask the guard
    pytest.param([raw(START - 300_000)], START, 0, "before", id="row_before_start"),
    pytest.param([raw(1_793_620_500_000)], 1_793_620_500_001, 0, "before", id="mid_period_start"),
    # index 1, after a valid first row at START
    pytest.param([FIRST, raw(END + 300_000)], START, 1, "after", id="row_after_end"),
    pytest.param([FIRST, raw(T + 1)], START, 1, "aligned", id="unaligned_timestamp"),
    pytest.param([FIRST, raw(START)], START, 1, "increasing", id="duplicated_timestamp"),
    pytest.param([raw(T), raw(T - 300_000)], START, 1, "increasing", id="descending_timestamp"),
    pytest.param([FIRST, raw(symbol="ETHUSDT")], START, 1, "symbol", id="wrong_symbol"),
    pytest.param([FIRST, without("symbol")], START, 1, "missing", id="symbol_missing"),
    pytest.param([FIRST, without("sumOpenInterestValue")], START, 1, "missing", id="value_missing"),
    pytest.param([FIRST, raw(str(T))], START, 1, "timestamp", id="timestamp_str"),
    pytest.param([FIRST, raw(float(T))], START, 1, "timestamp", id="timestamp_float"),
    pytest.param([FIRST, raw(True)], START, 1, "timestamp", id="timestamp_bool"),
    pytest.param([FIRST, raw(sumOpenInterest=95677.779)], START, 1, "sumOpenInterest", id="value_float"),
    pytest.param([FIRST, raw(sumOpenInterest=95677)], START, 1, "sumOpenInterest", id="value_int"),
    pytest.param([FIRST, raw(sumOpenInterestValue=None)], START, 1, "sumOpenInterestValue", id="value_none"),
    pytest.param([FIRST, raw(sumOpenInterest="-1")], START, 1, "sign", id="value_negative"),
    pytest.param([FIRST, raw(sumOpenInterest="+5")], START, 1, "sign", id="value_plus"),
    pytest.param([FIRST, raw(sumOpenInterest=" 1")], START, 1, "grammar", id="value_whitespace"),
    pytest.param([FIRST, raw(sumOpenInterestValue="abc")], START, 1, "grammar", id="value_text"),
    pytest.param([FIRST, [T, "1", "2"]], START, 1, "dict", id="row_list"),
    pytest.param([FIRST, "row"], START, 1, "dict", id="row_str"),
]


@pytest.mark.parametrize(("response", "start", "row_index", "word"), MALFORMED)
def test_malformed_response_is_rejected_entirely(response: object, start: int, row_index: int, word: str) -> None:
    with pytest.raises(OpenInterestParseError, match=word) as error:
        parse(response, start=start)
    assert error.value.row_index == row_index


@pytest.mark.parametrize("response", [{}, None, "[]"], ids=["dict", "none", "str"])
def test_non_list_response_is_rejected(response: object) -> None:
    with pytest.raises(OpenInterestParseError, match="list") as error:
        parse(response)
    assert error.value.row_index is None


def test_row_count_is_checked_before_rows() -> None:
    rows = [raw(str(T)), raw(T), raw(END)]
    with pytest.raises(OpenInterestParseError, match="period ends") as error:
        parse(rows, start=T, end=END)  # 2 period ends, 3 rows, row 0 malformed
    assert error.value.row_index is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_time_ms": END + 1},
        {"market_id": ""},
        {"market_id": untyped(5)},
        {"symbol": ""},
        {"start_time_ms": untyped(True)},
        {"period": untyped("5m")},
    ],
    ids=["start_after_end", "empty_market_id", "int_market_id", "empty_symbol", "bool_start", "str_period"],
)
def test_argument_errors_are_not_parse_errors(kwargs: dict[str, Any]) -> None:
    arguments: dict[str, Any] = {
        "market_id": "BTCUSDT",
        "symbol": "BTC/USDT:USDT",
        "period": P5,
        "start_time_ms": START,
        "end_time_ms": END,
    }
    arguments.update(kwargs)
    with pytest.raises((TypeError, ValueError)) as error:
        parse_open_interest_hist([raw()], **arguments)
    assert type(error.value) is not OpenInterestParseError
