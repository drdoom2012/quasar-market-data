from typing import get_args

from quasar_market_data.api.errors import (
    RECEIVED_MAX_CHARS,
    ApiError,
    ErrorCode,
    cut_received,
    funding_rate_disabled,
    internal_error,
    invalid_parameter,
    method_not_allowed,
    not_found,
    not_ready,
    open_interest_disabled,
    unknown_funding_rate_series,
    unknown_open_interest_series,
    unknown_series,
)


def test_body_has_exactly_code_message_details() -> None:
    error = ApiError(400, "invalid_parameter", "m", {"parameter": "limit"})

    assert error.body() == {"error": {"code": "invalid_parameter", "message": "m", "details": {"parameter": "limit"}}}
    body = error.body()["error"]
    assert isinstance(body, dict)
    assert set(body) == {"code", "message", "details"}


def test_received_is_cut_to_64_characters() -> None:
    assert RECEIVED_MAX_CHARS == 64
    assert cut_received("x" * 100) == "x" * 64
    assert cut_received("short") == "short"

    error = invalid_parameter("m", parameter="symbol", received="y" * 100)

    assert error.details["received"] == "y" * 64


def test_absent_details_are_omitted() -> None:
    assert invalid_parameter("m").details == {}
    assert invalid_parameter("m", parameter="limit").details == {"parameter": "limit"}
    assert invalid_parameter("m", parameter="limit", maximum=1000).details == {"parameter": "limit", "maximum": 1000}


def test_not_ready_retry_after() -> None:
    assert not_ready("database_missing").headers == {"Retry-After": "5"}
    assert not_ready("database_unreadable").headers == {"Retry-After": "5"}
    assert not_ready("schema_mismatch").headers == {}
    assert not_ready("schema_mismatch").details == {"reason": "schema_mismatch"}
    assert not_ready("database_missing").status == 503
    assert not_ready("database_missing").code == "not_ready"


def test_method_not_allowed_carries_allow_get() -> None:
    error = method_not_allowed("GET")

    assert error.status == 405
    assert error.code == "method_not_allowed"
    assert error.details == {"allow": "GET"}
    assert error.headers == {"Allow": "GET"}


def test_the_other_constructors() -> None:
    assert unknown_series("binance-usdm", "X/Y", "5m").details == {
        "venue": "binance-usdm",
        "symbol": "X/Y",
        "timeframe": "5m",
    }
    assert unknown_series("binance-usdm", "X/Y", "5m").status == 404
    assert (not_found().status, not_found().code, not_found().details) == (404, "not_found", {})
    assert (internal_error().status, internal_error().code, internal_error().details) == (500, "internal_error", {})


def test_open_interest_disabled_is_a_404_with_no_details() -> None:
    error = open_interest_disabled()

    assert (error.status, error.code, error.details, error.headers) == (404, "open_interest_disabled", {}, {})


def test_unknown_open_interest_series_names_the_period_and_not_the_timeframe() -> None:
    error = unknown_open_interest_series("binance-usdm", "BTC/USDT:USDT", "5m")

    assert (error.status, error.code) == (404, "unknown_series")
    assert error.message == "no open interest row of this series has been stored"
    assert error.details == {"venue": "binance-usdm", "symbol": "BTC/USDT:USDT", "period": "5m"}
    assert unknown_series("v", "s", "5m").message == "no candle of this series has been stored"


def test_the_closed_list_of_error_codes() -> None:
    assert set(get_args(ErrorCode.__value__)) == {
        "invalid_parameter",
        "unknown_series",
        "not_found",
        "method_not_allowed",
        "internal_error",
        "not_ready",
        "open_interest_disabled",
        "funding_rate_disabled",
    }


def test_funding_rate_disabled_is_a_404_with_no_details() -> None:
    error = funding_rate_disabled()

    assert (error.status, error.code, error.details, error.headers) == (404, "funding_rate_disabled", {}, {})


def test_unknown_funding_rate_series_names_neither_a_timeframe_nor_a_period() -> None:
    error = unknown_funding_rate_series("binance-usdm", "BTC/USDT:USDT")

    assert (error.status, error.code) == (404, "unknown_series")
    assert error.message == "no funding rate span or row of this series has been stored"
    assert error.details == {"venue": "binance-usdm", "symbol": "BTC/USDT:USDT"}
