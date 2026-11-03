"""``GET /v1/open-interest/coverage`` request parsing (contract api-v1 6.6.1): five required parameters, no paging."""

import pytest

from quasar_market_data.api.errors import ApiError
from quasar_market_data.api.params import OpenInterestCoverageRequest, parse_open_interest_coverage_request
from quasar_market_data.domain.timeframe import Timeframe

TF = 300_000
START = 1792672800000  # a period END (time_ms), aligned
END = START + 7 * TF
BASE = f"venue=binance-usdm&symbol=BTC/USDT:USDT&period=5m&start={START}&end={END}"


def parse(query: str) -> OpenInterestCoverageRequest:
    return parse_open_interest_coverage_request(query.encode("ascii"))


def error_of(query: str) -> ApiError:
    with pytest.raises(ApiError) as caught:
        parse_open_interest_coverage_request(query.encode("ascii"))
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


def test_the_five_parameters_parse() -> None:
    assert parse(BASE) == OpenInterestCoverageRequest("binance-usdm", "BTC/USDT:USDT", Timeframe("5m"), START, END)


def test_the_boundaries_are_accepted() -> None:
    assert parse(replace(end=str(START + 10_000 * TF))).end == START + 10_000 * TF
    assert parse(replace(end=str(START + TF))).end == START + TF
    maximum = 4102444800000
    assert parse(replace(start=str(maximum), end=str(maximum + TF))).end == maximum + TF  # one period past the maximum
    assert parse(replace(start="946684800000", end=str(946684800000 + TF))).start == 946684800000


@pytest.mark.parametrize(
    ("query", "parameter", "received", "maximum"),
    [
        (replace(venue=None), "venue", None, None),
        (replace(symbol=None), "symbol", None, None),
        (replace(period=None), "period", None, None),
        (replace(start=None), "start", None, None),
        (replace(end=None), "end", None, None),  # no default end: the window is explicit
        (f"{BASE}&limit=10", "limit", "10", None),  # no paging
        (f"{BASE}&as_of=1", "as_of", "1", None),  # the clock is the server's
        (f"{BASE}&timeframe=5m", "timeframe", "5m", None),  # the candle name is unknown here
        (f"{BASE}&strt=1", "strt", "1", None),
        (f"{BASE}&end={END}", "end", str(END), None),  # repeated
        (replace(period="1h"), "period", "1h", None),
        (replace(venue="binance-spot"), "venue", "binance-spot", None),
        (replace(start=str(START + 1)), "start", str(START + 1), None),
        (replace(end=str(END + 1)), "end", str(END + 1), None),
        (replace(end=str(START)), "end", str(START), None),
        (replace(end=str(START - TF)), "end", str(START - TF), None),
        (replace(end=str(START + 10_001 * TF)), "end", str(START + 10_001 * TF), 10_000),
        (replace(end="4102445100001"), "end", "4102445100001", None),
    ],
)
def test_a_bad_request_is_a_400_naming_the_parameter(
    query: str, parameter: str, received: str | None, maximum: int | None
) -> None:
    error = error_of(query)

    assert error.details.get("parameter") == parameter
    if received is not None:
        assert error.details.get("received") == received
    if maximum is not None:
        assert error.details.get("maximum") == maximum


def test_the_messages_name_period_not_timeframe() -> None:
    assert "period" in error_of(replace(period="1h")).message
    assert "period" in error_of(replace(start=str(START + 1))).message
    assert "period" in error_of(replace(end=str(END + 1))).message
