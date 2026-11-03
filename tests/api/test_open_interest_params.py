"""``GET /v1/open-interest`` request parsing (contract api-v1 6.5.2): the strictness of ``/v1/candles`` with ``period``."""

import pytest

from quasar_market_data.api.errors import ApiError
from quasar_market_data.api.params import OpenInterestRequest, parse_open_interest_request
from quasar_market_data.domain.timeframe import Timeframe

TF = 300_000
START = 1792672800000  # a period END (time_ms), aligned
BASE = f"venue=binance-usdm&symbol=BTC/USDT:USDT&period=5m&start={START}"


def parse(query: str) -> OpenInterestRequest:
    return parse_open_interest_request(query.encode("ascii"))


def error_of(query: str | bytes) -> ApiError:
    raw = query.encode("ascii") if isinstance(query, str) else query
    with pytest.raises(ApiError) as caught:
        parse_open_interest_request(raw)
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


def test_minimal_valid_request() -> None:
    assert parse(BASE) == OpenInterestRequest("binance-usdm", "BTC/USDT:USDT", Timeframe("5m"), START, None, 1000)


def test_all_parameters_and_boundaries_are_accepted() -> None:
    request = parse(f"{BASE}&end={START + 10_000 * TF}&limit=1")

    assert (request.end, request.limit) == (START + 10_000 * TF, 1)
    assert parse(f"{BASE}&limit=1000").limit == 1000
    assert parse(replace(start="946684800000")).start == 946684800000
    maximum = 4102444800000
    assert parse(replace(start=str(maximum)) + f"&end={maximum + TF}").end == maximum + TF


@pytest.mark.parametrize(
    ("query", "parameter", "received", "maximum"),
    [
        (replace(venue=None), "venue", None, None),
        (replace(symbol=None), "symbol", None, None),
        (replace(period=None), "period", None, None),
        (replace(start=None), "start", None, None),
        (f"{BASE}&timeframe=5m", "timeframe", "5m", None),  # the candle name is unknown here
        (f"{BASE}&limt=10", "limt", "10", None),
        (f"{BASE}&limit=1&limit=2", "limit", "2", None),
        (f"{BASE}&limit=", "limit", "", None),
        (f"{BASE}&end=", "end", "", None),
        (f"{BASE}&limit=0", "limit", "0", 1000),
        (f"{BASE}&limit=1001", "limit", "1001", 1000),
        (f"{BASE}&limit=1_000", "limit", "1_000", None),
        (replace(start=str(START + 1)), "start", str(START + 1), None),
        (replace(start="946684799999"), "start", "946684799999", None),
        (replace(start="4102444800001"), "start", "4102444800001", None),
        (f"{BASE}&end={START + TF + 1}", "end", str(START + TF + 1), None),
        (f"{BASE}&end={START}", "end", str(START), None),
        (f"{BASE}&end={START - TF}", "end", str(START - TF), None),
        (f"{BASE}&end={START + 10_001 * TF}", "end", str(START + 10_001 * TF), 10_000),
        (replace(venue="binance-spot"), "venue", "binance-spot", None),
        (replace(period="1h"), "period", "1h", None),
        (replace(period="5M"), "period", "5M", None),
        (replace(symbol="A" * 65), "symbol", "A" * 64, None),
        (replace(symbol="BTC%20USDT"), "symbol", "BTC USDT", None),
        (replace(symbol="BTC%00USDT"), "symbol", "BTC\x00USDT", None),
    ],
)
def test_invalid_requests_are_400_with_details(
    query: str, parameter: str, received: str | None, maximum: int | None
) -> None:
    error = error_of(query)

    assert error.details.get("parameter") == parameter
    assert error.details.get("received") == received
    assert error.details.get("maximum") == maximum
    assert ("received" in error.details) == (received is not None)


def test_the_messages_name_the_period_and_not_the_timeframe() -> None:
    assert error_of(replace(period="1h")).message == "period must be 5m"
    assert error_of(replace(start=str(START + 1))).message == "start must be aligned to the period"
    assert error_of(f"{BASE}&end={START + TF + 1}").message == "end must be aligned to the period"


def test_an_undecodable_query_is_400_without_parameter() -> None:
    error = error_of(f"{BASE}&a=%zz")

    assert "parameter" not in error.details


def test_a_raw_non_ascii_byte_is_400_without_parameter() -> None:
    assert "parameter" not in error_of(BASE.encode("ascii") + b"&limit=\xc3").details


def test_symbols_decode_exactly() -> None:
    assert parse(replace(symbol="BTC%2FUSDT%3AUSDT")).symbol == "BTC/USDT:USDT"
    assert parse(replace(symbol="e%CC%81")).symbol != parse(replace(symbol="%C3%A9")).symbol
