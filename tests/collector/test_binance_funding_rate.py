from typing import Any

import pytest

from quasar_market_data.collector.binance_funding_rate import FundingRateParseError, parse_funding_rate_history
from quasar_market_data.domain.funding_rate import (
    FundingRate,
    FundingRateKey,
    RateType,
    count_off_hour_rows,
    is_off_hour,
)
from tests.typing_helpers import untyped

START = 1_793_700_000_000
END = 1_793_750_000_000
T = 1_793_736_000_003  # inside the range, 3 ms after a whole hour
ROW: dict[str, Any] = {
    "symbol": "BTCUSDT",
    "fundingTime": T,
    "fundingRate": "0.00010000",
    "markPrice": "67123.45000000",
    "rateType": "Regular",
}


def make_row(**overrides: Any) -> dict[str, Any]:
    row = dict(ROW)
    row.update(overrides)
    return row


def without(key: str) -> dict[str, Any]:
    row = dict(ROW)
    del row[key]
    return row


def parse(response: object, **kwargs: Any) -> tuple[FundingRate, ...]:
    arguments: dict[str, Any] = {
        "market_id": "BTCUSDT",
        "symbol": "BTC/USDT:USDT",
        "start_time_ms": START,
        "end_time_ms": END,
    }
    arguments.update(kwargs)
    return parse_funding_rate_history(response, **arguments)


def test_parses_a_row() -> None:
    (row,) = parse([ROW])
    assert row.key == FundingRateKey("binance-usdm", "BTC/USDT:USDT", T, RateType.REGULAR)
    assert row.funding_rate == "0.0001"
    assert row.mark_price == "67123.45"
    assert parse([make_row(extra="ignored")]) == (row,)


def test_negative_zero_and_empty_mark_price() -> None:
    (negative,) = parse([make_row(fundingRate="-0.00010000")])
    assert negative.funding_rate == "-0.0001"
    (negative_zero,) = parse([make_row(fundingRate="-0.00000000")])
    assert negative_zero.funding_rate == "0"
    (zero,) = parse([make_row(fundingRate="0.00000000", markPrice="0.00000000")])
    assert (zero.funding_rate, zero.mark_price) == ("0", "0")
    (no_price,) = parse([make_row(markPrice="")])
    assert no_price.mark_price is None
    regular, special = parse([make_row(), make_row(fundingTime=T + 1_002, rateType="Special")])
    assert regular.key != special.key
    assert special.key.rate_type is RateType.SPECIAL


def test_a_leading_plus_and_an_exponent_are_canonicalized() -> None:
    assert parse([make_row(fundingRate="+0.0001")])[0].funding_rate == "0.0001"
    assert parse([make_row(fundingRate="1e-4")])[0].funding_rate == "0.0001"


def test_empty_response_is_valid() -> None:
    assert parse([]) == ()


def test_inclusive_bounds() -> None:
    rows = parse([make_row(fundingTime=START), make_row(fundingTime=END)])
    assert [row.key.funding_time_ms for row in rows] == [START, END]


def test_off_hour_row_is_accepted() -> None:
    half_past = 1_793_736_000_000 + 1_800_000
    rows = parse([make_row(fundingTime=half_past)])
    assert is_off_hour(rows[0].key.funding_time_ms) is True
    assert count_off_hour_rows(rows) == 1
    with pytest.raises(FundingRateParseError, match="before"):
        parse([make_row(fundingTime=1_793_736_000)])  # seconds instead of milliseconds


@pytest.mark.parametrize("order", [("Regular", "Special"), ("Special", "Regular")])
def test_coincident_regular_and_special_pair_is_accepted_in_either_order(order: tuple[str, str]) -> None:
    rows = parse([make_row(rateType=order[0]), make_row(rateType=order[1])])
    assert [row.key.rate_type.value for row in rows] == list(order)
    assert rows[0].key.funding_time_ms == rows[1].key.funding_time_ms


@pytest.mark.parametrize("order", [("Regular", "Special"), ("Special", "Regular")])
def test_the_pair_as_the_last_two_rows_of_a_full_response_is_accepted(order: tuple[str, str]) -> None:
    limit = 5
    response = [make_row(fundingTime=START + i * 1_000) for i in range(limit - 2)]
    response += [make_row(fundingTime=T, rateType=order[0]), make_row(fundingTime=T, rateType=order[1])]
    assert len(parse(response, limit=limit)) == limit


FIRST_VALID = make_row(fundingTime=START)  # valid row 0 for the index 1 cases


def _case(identifier: str, response: list[Any], row_index: int | None, word: str) -> Any:
    return pytest.param(response, row_index, word, id=identifier)


MALFORMED = [
    # Index 0: no previous row, so the ordering guard cannot mask the guard under test.
    _case("before-start", [make_row(fundingTime=START - 1)], 0, "before"),
    _case("fundingTime-str", [make_row(fundingTime=str(T))], 0, "JSON integer"),
    _case("fundingTime-float", [make_row(fundingTime=float(T))], 0, "JSON integer"),
    _case("fundingTime-bool", [make_row(fundingTime=True)], 0, "JSON integer"),
    _case("wrong-symbol", [make_row(symbol="ETHUSDT")], 0, "symbol"),
    _case("rateType-lowercase", [make_row(rateType="regular")], 0, "Regular or Special"),
    _case("rateType-unknown", [make_row(rateType="Other")], 0, "Regular or Special"),
    _case("rateType-none", [make_row(rateType=None)], 0, "Regular or Special"),
    _case("rateType-list", [make_row(rateType=["Regular"])], 0, "Regular or Special"),
    _case("rate-float", [make_row(fundingRate=0.0001)], 0, "decimal string"),
    _case("rate-int", [make_row(fundingRate=1)], 0, "decimal string"),
    _case("rate-none", [make_row(fundingRate=None)], 0, "decimal string"),
    _case("rate-empty", [make_row(fundingRate="")], 0, "grammar"),
    _case("rate-text", [make_row(fundingRate="abc")], 0, "grammar"),
    _case("rate-space", [make_row(fundingRate=" 0.1")], 0, "grammar"),
    _case("price-float", [make_row(markPrice=67123.45)], 0, "decimal string"),
    _case("price-int", [make_row(markPrice=1)], 0, "decimal string"),
    _case("price-none", [make_row(markPrice=None)], 0, "decimal string"),
    _case("price-negative", [make_row(markPrice="-1")], 0, "sign"),
    _case("price-plus", [make_row(markPrice="+1")], 0, "sign"),
    _case("price-text", [make_row(markPrice="abc")], 0, "grammar"),
    _case("missing-symbol", [without("symbol")], 0, "missing key"),
    _case("missing-fundingTime", [without("fundingTime")], 0, "missing key"),
    _case("missing-fundingRate", [without("fundingRate")], 0, "missing key"),
    _case("missing-markPrice", [without("markPrice")], 0, "missing key"),
    _case("missing-rateType", [without("rateType")], 0, "missing key"),
    _case("row-list", [[1, 2]], 0, "dict"),
    _case("row-str", ["row"], 0, "dict"),
    # Index 1 after a valid first row at START.
    _case("after-end", [FIRST_VALID, make_row(fundingTime=END + 1)], 1, "after"),
    _case("descending", [make_row(fundingTime=T), make_row(fundingTime=T - 1)], 1, "ascending"),
    _case("identical-row-repeated", [make_row(), make_row()], 1, "repeated"),
    _case("same-key-different-rate", [make_row(), make_row(fundingRate="0.0002")], 1, "repeated"),
    _case("same-time-rateType-none", [make_row(), make_row(rateType=None)], 1, "Regular or Special"),
    _case("same-time-rateType-unknown", [make_row(), make_row(rateType="Other")], 1, "Regular or Special"),
    # Index 2: the key seen two rows ago is still a repeat.
    _case(
        "regular-special-regular",
        [make_row(rateType="Regular"), make_row(rateType="Special"), make_row(rateType="Regular")],
        2,
        "repeated",
    ),
]


@pytest.mark.parametrize(("response", "row_index", "word"), MALFORMED)
def test_malformed_response_is_rejected_entirely(response: list[Any], row_index: int | None, word: str) -> None:
    with pytest.raises(FundingRateParseError) as error:
        parse(response)
    assert error.value.row_index == row_index
    assert word in str(error.value)


@pytest.mark.parametrize(
    "response",
    [
        {},
        None,
        "[]",
        {"status": "ERROR", "type": "GENERAL", "code": "99099990", "errorData": "illegal params."},
    ],
)
def test_non_list_response_is_rejected(response: object) -> None:
    with pytest.raises(FundingRateParseError) as error:
        parse(response)
    assert error.value.row_index is None
    assert "list" in str(error.value)


def test_an_error_object_inside_a_list_is_rejected_as_a_row() -> None:
    error_object = {"status": "ERROR", "type": "GENERAL", "code": "99099990", "errorData": "illegal params."}
    with pytest.raises(FundingRateParseError, match="missing key") as error:
        parse([error_object])
    assert error.value.row_index == 0


def test_row_count_is_checked_before_rows() -> None:
    response = [make_row(fundingTime=str(START)), make_row(fundingTime=START + 1), make_row(fundingTime=START + 2)]
    with pytest.raises(FundingRateParseError, match="more than") as error:
        parse(response, limit=2)
    assert error.value.row_index is None
    too_many = [make_row(fundingTime=START + i * 1_000) for i in range(1_001)]
    with pytest.raises(FundingRateParseError, match="more than") as default_limit:
        parse(too_many)
    assert default_limit.value.row_index is None


@pytest.mark.parametrize("limit", [3, 1_000])
def test_limit_rows_are_accepted(limit: int) -> None:
    response = [make_row(fundingTime=START + i * 1_000) for i in range(limit)]
    assert len(parse(response, limit=limit)) == limit


def test_argument_errors_are_not_parse_errors() -> None:
    cases: list[tuple[dict[str, Any], type[Exception]]] = [
        ({"start_time_ms": END, "end_time_ms": START}, ValueError),
        ({"market_id": ""}, ValueError),
        ({"symbol": ""}, ValueError),
        ({"market_id": untyped(1)}, TypeError),
        ({"start_time_ms": untyped(True)}, TypeError),
        ({"end_time_ms": untyped(True)}, TypeError),
        ({"limit": 0}, ValueError),
        ({"limit": 1_001}, ValueError),
        ({"limit": untyped(True)}, TypeError),
    ]
    for kwargs, expected in cases:
        with pytest.raises(expected) as error:
            parse([], **kwargs)
        assert type(error.value) is not FundingRateParseError
