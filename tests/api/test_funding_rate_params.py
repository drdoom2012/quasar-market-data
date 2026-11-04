"""``GET /v1/funding-rates`` request parsing (contract api-v1 6.7.2): closed window, unaligned instants, limit from 2."""

import pytest

from quasar_market_data.api.errors import ApiError
from quasar_market_data.api.params import FundingRatesRequest, parse_funding_rates_request
from quasar_market_data.domain.funding_rate_coverage import MAX_FUNDING_WINDOW_MS

START = 1_792_656_000_017  # not aligned to anything: a few ms after a whole hour
BASE = f"venue=binance-usdm&symbol=BTC/USDT:USDT&start={START}"
MIN_MS, MAX_MS = 946_684_800_000, 4_102_444_800_000


def parse(query: str) -> FundingRatesRequest:
    return parse_funding_rates_request(query.encode("ascii"))


def error_of(query: str | bytes) -> ApiError:
    raw = query.encode("ascii") if isinstance(query, str) else query
    with pytest.raises(ApiError) as caught:
        parse_funding_rates_request(raw)
    assert caught.value.status == 400
    assert caught.value.code == "invalid_parameter"
    return caught.value


def replace(**changes: str | None) -> str:
    pairs = dict(pair.split("=", 1) for pair in BASE.split("&"))
    for name, value in changes.items():
        if value is None:
            del pairs[name]
        else:
            pairs[name] = value
    return "&".join(f"{name}={value}" for name, value in pairs.items())


def test_minimal_valid_request_has_no_end_and_the_default_limit() -> None:
    assert parse(BASE) == FundingRatesRequest("binance-usdm", "BTC/USDT:USDT", START, None, 1000)


def test_unaligned_instants_and_the_boundaries_are_accepted() -> None:
    assert parse(f"{BASE}&end={START + 1}").end == START + 1
    assert parse(f"{BASE}&end={START}").end == START  # a one-millisecond window is valid
    assert parse(replace(start=str(MIN_MS))).start == MIN_MS
    assert parse(replace(start=str(MAX_MS)) + f"&end={MAX_MS}").end == MAX_MS  # the last valid instant, not +1 slot
    assert parse(f"{BASE}&limit=2").limit == 2
    assert parse(f"{BASE}&limit=1000").limit == 1000


def test_the_window_maximum_is_365_days_inclusive_and_one_ms_more_is_refused() -> None:
    assert parse(f"{BASE}&end={START + MAX_FUNDING_WINDOW_MS}").end == START + MAX_FUNDING_WINDOW_MS
    error = error_of(f"{BASE}&end={START + MAX_FUNDING_WINDOW_MS + 1}")
    assert (error.details["parameter"], error.details["maximum"]) == ("end", MAX_FUNDING_WINDOW_MS)
    assert "365 days" in error.message


@pytest.mark.parametrize(
    ("query", "parameter", "received", "maximum"),
    [
        (replace(venue=None), "venue", None, None),
        (replace(symbol=None), "symbol", None, None),
        (replace(start=None), "start", None, None),
        (f"{BASE}&timeframe=5m", "timeframe", "5m", None),  # no timeframe here
        (f"{BASE}&period=5m", "period", "5m", None),  # and no period
        (f"{BASE}&limt=10", "limt", "10", None),
        (f"{BASE}&limit=1&limit=2", "limit", "2", None),
        (f"{BASE}&end=", "end", "", None),
        (replace(venue="binance-spot"), "venue", "binance-spot", None),
        (replace(symbol="BTC/USDT: USDT"), "symbol", "BTC/USDT:%20USDT".replace("%20", " "), None),
        (replace(start=str(MIN_MS - 1)), "start", str(MIN_MS - 1), None),
        (replace(start=str(MAX_MS + 1)), "start", str(MAX_MS + 1), None),
        (replace(start="1_000"), "start", "1_000", None),
        (replace(start="1.5"), "start", "1.5", None),
        (f"{BASE}&end={MAX_MS + 1}", "end", str(MAX_MS + 1), None),
        (f"{BASE}&end={START - 1}", "end", str(START - 1), None),
        (f"{BASE}&limit=0", "limit", "0", 1000),
        (f"{BASE}&limit=1", "limit", "1", 1000),
        (f"{BASE}&limit=1001", "limit", "1001", 1000),
        (f"{BASE}&limit=1_000", "limit", "1_000", None),
    ],
)
def test_invalid_requests_name_the_parameter(
    query: str, parameter: str, received: str | None, maximum: int | None
) -> None:
    error = error_of(query)
    assert error.details["parameter"] == parameter
    if received is not None:
        assert error.details["received"] == received
    assert error.details.get("maximum") == maximum


def test_the_limit_message_names_two_to_one_thousand() -> None:
    assert error_of(f"{BASE}&limit=1").message == "limit must be between 2 and 1000"


def test_end_before_start_is_refused_but_equal_is_not() -> None:
    assert error_of(f"{BASE}&end={START - 1}").message == "end must not be before start"
    assert parse(f"{BASE}&end={START}").end == START


def test_the_checks_run_in_the_order_of_the_candle_endpoint() -> None:
    # venue, symbol, start, end, limit, window: the first failing one is reported
    bad = "venue=other&symbol=A&start=x&end=y&limit=z"
    assert error_of(bad).details["parameter"] == "venue"
    assert error_of("venue=binance-usdm&symbol=A%20B&start=x&end=y&limit=z").details["parameter"] == "symbol"
    assert error_of("venue=binance-usdm&symbol=A&start=x&end=y&limit=z").details["parameter"] == "start"
    assert error_of(f"venue=binance-usdm&symbol=A&start={START}&end=y&limit=z").details["parameter"] == "end"
    assert error_of(f"venue=binance-usdm&symbol=A&start={START}&end={START}&limit=z").details["parameter"] == "limit"
    over = START + MAX_FUNDING_WINDOW_MS + 1
    assert error_of(f"venue=binance-usdm&symbol=A&start={START}&end={over}&limit=1").details["parameter"] == "limit"
    assert error_of(f"venue=binance-usdm&symbol=A&start={START}&end={over}").details["parameter"] == "end"


def test_malformed_query_strings_are_400s() -> None:
    assert error_of(b"venue=binance-usdm&symbol=%ZZ&start=1").details.get("parameter") is None
    assert error_of("venue=binance-usdm&symbol=A&start=1&start=2").details["parameter"] == "start"
