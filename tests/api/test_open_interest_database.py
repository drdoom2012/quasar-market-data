"""How ``read_open_interest`` maps storage failures to the contract's errors (6.5.5): the candle mapping, reused."""

from pathlib import Path

import pytest

from quasar_market_data.api.database import read_candles, read_open_interest
from quasar_market_data.api.errors import ApiError
from quasar_market_data.api.open_interest_page import OpenInterestPage
from quasar_market_data.api.params import CandlesRequest, OpenInterestRequest
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule
from quasar_market_data.domain.timeframe import Timeframe
from tests.api.seed import SYMBOL, VENUE, make_database

P5 = Timeframe("5m")
TF = 300_000
S = 1792672500000
RULE = OpenInterestRule(cadence=1, expected_after_ms=500_000, lag_threshold_ms=60_000, period=P5)
NOW = S + 100 * TF


def oi_request(symbol: str = SYMBOL) -> OpenInterestRequest:
    return OpenInterestRequest(VENUE, symbol, P5, S, S + 5 * TF, 1000)


def read(path: Path, request: OpenInterestRequest) -> OpenInterestPage:
    return read_open_interest(path, request, as_of_ms=NOW, rule=RULE)


def api_error(path: Path, request: OpenInterestRequest) -> ApiError:
    with pytest.raises(ApiError) as caught:
        read(path, request)
    return caught.value


def test_a_page_is_returned_for_a_known_series(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[S])

    page = read(path, oi_request())

    assert page.symbol == SYMBOL


def test_an_unknown_series_is_a_404_with_the_period_and_not_the_timeframe(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[S])

    error = api_error(path, oi_request("ETH/USDT:USDT"))

    assert (error.status, error.code) == (404, "unknown_series")
    assert error.details == {"venue": VENUE, "symbol": "ETH/USDT:USDT", "period": "5m"}
    assert error.message == "no open interest row of this series has been stored"


def test_the_candle_endpoint_still_names_the_timeframe(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[S])

    with pytest.raises(ApiError) as caught:
        read_candles(path, CandlesRequest(VENUE, "ETH/USDT:USDT", P5, S, S + TF, 1000))

    assert caught.value.details == {"venue": VENUE, "symbol": "ETH/USDT:USDT", "timeframe": "5m"}


def test_a_missing_file_is_database_missing_and_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "absent.db"

    error = api_error(path, oi_request())

    assert (error.status, error.code, error.details) == (503, "not_ready", {"reason": "database_missing"})
    assert not path.exists()
