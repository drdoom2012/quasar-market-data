from quasar_market_data.api.errors import (
    RECEIVED_MAX_CHARS,
    ApiError,
    cut_received,
    internal_error,
    invalid_parameter,
    method_not_allowed,
    not_found,
    not_ready,
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
