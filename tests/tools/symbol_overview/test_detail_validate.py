"""Validation of one page of candles, open interest rows or funding rows (design 5.2, contract 1, 5, 6.5, 6.7)."""

import json
from typing import Any

import pytest

from tests.tools.symbol_overview.detail_helpers import (
    api_error,
    body,
    candle_item,
    candles_doc,
    funding_item,
    funding_page_doc,
    oi_item,
    oi_page_doc,
)
from tests.tools.symbol_overview.helpers import T0, instrument
from tools.symbol_overview.detail_validate import (
    CandlePage,
    FundingPage,
    OiPage,
    candle_page_result,
    canonical_decimal,
    funding_page_result,
    open_interest_page_result,
)
from tools.symbol_overview.model import TF_MS, Invalid

S = T0 - 100 * TF_MS
E = T0
JSON = "application/json"


def candles(doc: Any, start: int = S, end: int = E) -> CandlePage | Invalid:
    return candle_page_result(200, JSON, body(doc), instrument=instrument(), start=start, end=end)


def oi(doc: Any, start: int = S, end: int = E) -> OiPage | Invalid:
    return open_interest_page_result(200, JSON, body(doc), instrument=instrument(), start=start, end=end)


def funding(doc: Any, start: int = S, end: int = E) -> FundingPage | Invalid:
    return funding_page_result(200, JSON, body(doc), instrument=instrument(), start=start, end=end)


def code(result: object) -> str:
    assert isinstance(result, Invalid), result
    return result.code


def three() -> list[dict[str, Any]]:
    return [candle_item(S + i * TF_MS, str(100 + i)) for i in range(3)]


# --- candles -------------------------------------------------------------------------------------------------------


def test_a_valid_page_of_candles_is_read() -> None:
    page = candles(candles_doc(three(), start=S, end=E))
    assert isinstance(page, CandlePage) and page.next is None
    assert [r.c for r in page.rows] == ["100", "101", "102"] and page.rows[0].t == S


def test_a_wrong_echo_is_refused() -> None:
    assert code(candles(candles_doc(three(), start=S, end=E, symbol="ETH/USDT:USDT"))) == "echo"
    assert code(candles(candles_doc(three(), start=S + TF_MS, end=E))) == "echo"
    assert code(candles(candles_doc(three(), start=S, end=E, timeframe="1m"))) == "echo"


def test_unsorted_duplicate_or_out_of_window_rows_are_refused() -> None:
    a, b, _c = three()
    assert code(candles(candles_doc([b, a], start=S, end=E, oldest=S, newest=S + TF_MS))) == "schema"
    assert code(candles(candles_doc([a, a], start=S, end=E))) == "schema"
    outside = candle_item(E)  # the window end is exclusive
    assert code(candles(candles_doc([a, outside], start=S, end=E, newest=E))) == "schema"
    assert code(candles(candles_doc([candle_item(S + 1)], start=S, end=E))) == "schema"  # unaligned


def test_a_float_instead_of_a_decimal_string_is_refused() -> None:
    assert code(candles(candles_doc([candle_item(S, open=82855.4)], start=S, end=E))) == "schema"


@pytest.mark.parametrize("bad", ["12\n", "١٢", "1e3", "+1", "01", "1.50", "-5", "", "1.", ".5", "0.0"])
def test_the_decimal_rule_of_the_contract_refuses(bad: str) -> None:
    assert not canonical_decimal(bad)
    assert code(candles(candles_doc([candle_item(S, bad)], start=S, end=E))) == "schema"


def test_the_decimal_rule_of_the_contract_accepts() -> None:
    assert all(canonical_decimal(ok) for ok in ("0", "0.5", "82709", "95690.12", "10"))
    assert canonical_decimal("-0.00005123", signed=True) and not canonical_decimal("-0", signed=True)
    assert not canonical_decimal("-1")  # only funding_rate is signed


def test_inconsistent_ohlc_is_refused() -> None:
    assert code(candles(candles_doc([candle_item(S, high="99", low="100")], start=S, end=E))) == "schema"
    assert code(candles(candles_doc([candle_item(S, high="99", close="100")], start=S, end=E))) == "schema"
    assert code(candles(candles_doc([candle_item(S, low="101", close="100")], start=S, end=E))) == "schema"


def test_a_turnover_is_null_or_a_decimal() -> None:
    assert isinstance(candles(candles_doc([candle_item(S, turnover="12.5")], start=S, end=E)), CandlePage)
    assert code(candles(candles_doc([candle_item(S, turnover="1e5")], start=S, end=E))) == "schema"


def test_next_must_move_forward_and_stay_inside_the_window() -> None:
    rows = three()
    assert isinstance(candles(candles_doc(rows, start=S, end=E, next_=S + 3 * TF_MS)), CandlePage)
    assert code(candles(candles_doc(rows, start=S, end=E, next_=S))) == "schema"  # not after the last row
    assert code(candles(candles_doc(rows, start=S, end=E, next_=E))) == "schema"  # exclusive end
    assert code(candles(candles_doc(rows, start=S, end=E, next_=S + 3 * TF_MS + 1))) == "schema"  # unaligned


def test_a_missing_next_key_is_refused() -> None:
    doc = candles_doc(three(), start=S, end=E)
    del doc["next"]
    assert code(candles(doc)) == "schema"


def test_zero_rows_with_a_next_is_refused() -> None:
    assert code(candles(candles_doc([], start=S, end=E, next_=S + TF_MS))) == "schema"
    assert isinstance(candles(candles_doc([], start=S, end=E)), CandlePage)


def test_more_rows_than_the_limit_are_refused() -> None:
    rows = [candle_item(S + i * TF_MS) for i in range(1001)]
    assert code(candles(candles_doc(rows, start=S, end=S + 1001 * TF_MS), end=S + 1001 * TF_MS)) == "schema"


def test_a_boolean_is_not_an_integer() -> None:
    assert code(candles(candles_doc(three(), start=S, end=E, next_=True))) == "schema"


def test_rows_outside_the_series_bounds_are_refused() -> None:
    assert code(candles(candles_doc(three(), start=S, end=E, oldest=S + TF_MS))) == "schema"


def test_a_non_200_with_an_api_error_keeps_its_code() -> None:
    result = candle_page_result(
        404, JSON, json.dumps({"error": {"code": "unknown_series"}}).encode(), instrument=instrument(), start=S, end=E
    )
    assert result == Invalid("api_error", "unknown_series")
    assert (
        code(candle_page_result(200, "text/html", b"{}", instrument=instrument(), start=S, end=E)) == "bad_content_type"
    )
    assert api_error(503, "not_ready").status == 503


# --- open interest rows --------------------------------------------------------------------------------------------


def oi_rows() -> list[dict[str, Any]]:
    return [oi_item(S + TF_MS + i * TF_MS, str(200 + i)) for i in range(3)]


def test_a_valid_page_of_open_interest_rows_is_read() -> None:
    page = oi(oi_page_doc(oi_rows(), start=S, end=E))
    assert isinstance(page, OiPage) and [r.value for r in page.rows] == ["200", "201", "202"]


def test_open_interest_echo_and_time_semantics() -> None:
    assert code(oi(oi_page_doc(oi_rows(), start=S, end=E, symbol="ETH/USDT:USDT"))) == "echo"
    assert code(oi(oi_page_doc(oi_rows(), start=S, end=E, time_semantics="period_start"))) == "echo"
    assert code(oi(oi_page_doc(oi_rows(), start=S, end=E, period="1h"))) == "echo"


def test_open_interest_rows_are_validated_like_candles() -> None:
    a, b, _c = oi_rows()
    assert code(oi(oi_page_doc([b, a], start=S, end=E))) == "schema"
    assert code(oi(oi_page_doc([oi_item(S + 1)], start=S, end=E))) == "schema"
    assert code(oi(oi_page_doc([dict(a, open_interest_value=1.5)], start=S, end=E))) == "schema"
    assert code(oi(oi_page_doc([a], start=S, end=E, next_=a["time_ms"]))) == "schema"
    assert code(oi(oi_page_doc([], start=S, end=E, next_=S + TF_MS))) == "schema"


# --- funding rows --------------------------------------------------------------------------------------------------


def test_a_valid_page_of_funding_rows_is_read() -> None:
    t = S + 12345
    rows = [funding_item(t), funding_item(t, "0.0003", "Special"), funding_item(t + 8 * 3_600_000, "-0.00005123")]
    page = funding(funding_page_doc(rows, start=S, end=E))
    assert isinstance(page, FundingPage)
    assert [(r.t, r.rate_type) for r in page.rows][:2] == [(t, "Regular"), (t, "Special")]
    assert page.rows[2].rate == "-0.00005123" and page.oldest_ms == t


def test_funding_order_and_uniqueness() -> None:
    t = S + 5
    assert code(funding(funding_page_doc([funding_item(t), funding_item(t)], start=S, end=E))) == "schema"
    assert (
        code(funding(funding_page_doc([funding_item(t, kind="Special"), funding_item(t)], start=S, end=E))) == "schema"
    )
    assert code(funding(funding_page_doc([funding_item(t + 1), funding_item(t)], start=S, end=E))) == "schema"
    assert code(funding(funding_page_doc([funding_item(t, kind="Weird")], start=S, end=E))) == "schema"


def test_funding_rate_may_be_negative_but_not_minus_zero() -> None:
    assert isinstance(funding(funding_page_doc([funding_item(S + 5, "-0.5")], start=S, end=E)), FundingPage)
    assert code(funding(funding_page_doc([funding_item(S + 5, "-0")], start=S, end=E))) == "schema"
    assert code(funding(funding_page_doc([funding_item(S + 5, mark="-1")], start=S, end=E))) == "schema"
    assert isinstance(funding(funding_page_doc([funding_item(S + 5, mark=None)], start=S, end=E)), FundingPage)


def test_funding_window_is_closed_and_unaligned_times_are_legal() -> None:
    assert isinstance(funding(funding_page_doc([funding_item(E)], start=S, end=E)), FundingPage)  # closed end
    assert code(funding(funding_page_doc([funding_item(E + 1)], start=S, end=E))) == "schema"
    assert code(funding(funding_page_doc([funding_item(S - 1)], start=S, end=E))) == "schema"  # before the cursor


def test_next_bounds_differ_for_funding() -> None:
    rows = [funding_item(S + 5)]
    assert isinstance(funding(funding_page_doc(rows, start=S, end=E, next_=E)), FundingPage)  # next == end is legal
    assert code(funding(funding_page_doc(rows, start=S, end=E, next_=S + 5))) == "schema"
    assert code(funding(funding_page_doc(rows, start=S, end=E, next_=E + 1))) == "schema"
    assert code(funding(funding_page_doc([], start=S, end=E, next_=S + 5))) == "schema"


def test_funding_series_is_both_null_or_both_set() -> None:
    assert isinstance(funding(funding_page_doc([], start=S, end=E)), FundingPage)
    doc = funding_page_doc([], start=S, end=E)
    doc["series"] = {"oldest_funding_time_ms": None, "newest_funding_time_ms": S}
    assert code(funding(doc)) == "schema"
    rows_without_series = funding_page_doc([funding_item(S + 5)], start=S, end=E)
    rows_without_series["series"] = {"oldest_funding_time_ms": None, "newest_funding_time_ms": None}
    assert code(funding(rows_without_series)) == "schema"


def test_funding_coverage_block_is_checked() -> None:
    assert code(funding(funding_page_doc([], start=S, end=E, coverage={"window_state": "x"}))) == "schema"
    doc = funding_page_doc([], start=S, end=E)
    doc["coverage"]["complete"] = "yes"
    assert code(funding(doc)) == "schema"
    doc = funding_page_doc([], start=S, end=E)
    doc["coverage"]["conflicted_events"] = -1
    assert code(funding(doc)) == "schema"


def test_funding_echo() -> None:
    assert code(funding(funding_page_doc([], start=S, end=E, time_semantics="period_end"))) == "echo"
    assert code(funding(funding_page_doc([], start=S + 1, end=E))) == "echo"
    assert code(funding(funding_page_doc([], start=S, end=E, symbol="ETH/USDT:USDT"))) == "echo"
