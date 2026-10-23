import asyncio
import json
from collections.abc import Awaitable
from copy import deepcopy
from pathlib import Path
from typing import Any

import ccxt
import ccxt.async_support
import pytest

from quasar_market_data.collector.binance_klines import parse_klines
from quasar_market_data.collector.binance_usdm import SUPPORTED_TIMEFRAMES, BinanceUsdmSource
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.timeframe import Timeframe

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "binance"
GOOD_URL = "https://fapi.binance.com/fapi/v1"
SYMBOL = "BTC/USDT:USDT"
FIVE_MINUTES = Timeframe("5m")


def run[T](awaitable: Awaitable[T]) -> T:
    async def scenario() -> T:
        return await awaitable

    return asyncio.run(scenario())


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def usdm_markets() -> dict[str, dict[str, Any]]:
    markets = {}
    for entry in load("usdm_perpetual_markets.json")["markets"]:
        markets[entry["symbol"]] = {**entry, "swap": True, "linear": True}
    return markets


class FakeExchange:
    def __init__(self) -> None:
        self.markets: dict[str, dict[str, Any]] | None = usdm_markets()
        self.urls: dict[str, Any] = {"api": {"fapiPublic": GOOD_URL}}
        self.klines_response: object = load("btcusdt_5m_closed_raw.json")
        self.time_response: object = {"serverTime": 1792677035725}
        self.error: BaseException | None = None
        self.load_error: BaseException | None = None
        self.calls: list[tuple[str, Any]] = []
        self.closed = False

    async def load_markets(self) -> None:
        self.calls.append(("load_markets", None))
        if self.load_error is not None:
            raise self.load_error

    async def fapiPublicGetKlines(self, params: dict[str, Any]) -> object:
        self.calls.append(("klines", params))
        if self.error is not None:
            raise self.error
        return self.klines_response

    async def fapiPublicGetTime(self) -> object:
        self.calls.append(("time", None))
        if self.error is not None:
            raise self.error
        return self.time_response

    async def close(self) -> None:
        self.closed = True

    def called(self, name: str) -> int:
        return sum(1 for call_name, _ in self.calls if call_name == name)


def open_source(exchange: FakeExchange) -> BinanceUsdmSource:
    return run(BinanceUsdmSource.open(exchange))


def test_klines_request_carries_the_market_id_interval_and_limit() -> None:
    exchange = FakeExchange()
    source = open_source(exchange)
    run(source.fetch_klines(SYMBOL, FIVE_MINUTES, 100))
    assert exchange.calls[-1] == ("klines", {"symbol": "BTCUSDT", "interval": "5m", "limit": 100})


def test_raw_rows_are_returned_untouched() -> None:
    rows = load("smallprice_5m_closed_raw.json")  # contains padded decimals such as 0.2344000
    exchange = FakeExchange()
    exchange.klines_response = rows
    source = open_source(exchange)

    returned = run(source.fetch_klines("0G/USDT:USDT", FIVE_MINUTES, 100))

    assert returned == deepcopy(rows)
    assert returned is rows
    assert len(parse_klines(returned, "0G/USDT:USDT", FIVE_MINUTES)) == len(rows)


def unsafe_market(**changes: Any) -> dict[str, dict[str, Any]]:
    markets = usdm_markets()
    markets[SYMBOL] = {**markets[SYMBOL], **changes}
    return markets


UNSAFE = {
    "not_swap": unsafe_market(swap=False),
    "not_linear": unsafe_market(linear=False),
    "settle_usd": unsafe_market(settle="USD"),
    "quote_not_usdt": unsafe_market(quote="USD"),
    "has_expiry": unsafe_market(expiry=1893456000000),
}


@pytest.mark.parametrize("markets", UNSAFE.values(), ids=UNSAFE.keys())
def test_unsafe_markets_are_refused_before_any_request(markets: dict[str, dict[str, Any]]) -> None:
    exchange = FakeExchange()
    exchange.markets = markets
    source = open_source(exchange)
    with pytest.raises(SourceError, match="BTC/USDT:USDT"):
        run(source.fetch_klines(SYMBOL, FIVE_MINUTES, 100))
    assert exchange.called("klines") == 0


def test_unknown_symbol_is_refused_before_any_request() -> None:
    exchange = FakeExchange()
    source = open_source(exchange)
    with pytest.raises(SourceError, match="NOPE/USDT:USDT"):
        run(source.fetch_klines("NOPE/USDT:USDT", FIVE_MINUTES, 100))
    assert exchange.called("klines") == 0


def test_unverified_timeframe_is_refused() -> None:
    exchange = FakeExchange()
    source = open_source(exchange)
    assert SUPPORTED_TIMEFRAMES == frozenset({"5m"})
    with pytest.raises(SourceError, match="1h"):
        run(source.fetch_klines(SYMBOL, Timeframe("1h"), 100))
    assert exchange.called("klines") == 0


BAD_URLS = {
    "http_scheme": "http://fapi.binance.com/fapi/v1",
    "lookalike_host_suffix": "https://fapi.binance.com.evil.example/fapi/v1",
    "userinfo_trick": "https://fapi.binance.com@evil.example/fapi/v1",
    "host_inside_path": "https://evil.example/fapi.binance.com/fapi/v1",
    "coin_m_host": "https://dapi.binance.com/dapi/v1",
    "other_version": "https://fapi.binance.com/fapi/v2",
    "extra_path": "https://fapi.binance.com/fapi/v1/extra",
    "explicit_port": "https://fapi.binance.com:8443/fapi/v1",
    "with_query": "https://fapi.binance.com/fapi/v1?x=1",
    "with_fragment": "https://fapi.binance.com/fapi/v1#x",
    "malformed_port": "https://fapi.binance.com:abc/fapi/v1",
    "not_a_string": None,
}


@pytest.mark.parametrize("url", BAD_URLS.values(), ids=BAD_URLS.keys())
def test_the_base_url_must_be_exactly_the_usdm_fapi_endpoint(url: object) -> None:
    exchange = FakeExchange()
    exchange.urls = {"api": {"fapiPublic": url}}
    with pytest.raises(SourceError):
        open_source(exchange)
    assert exchange.called("load_markets") == 0  # not a single request goes to an unexpected host
    assert exchange.closed


MISSING_URLS: dict[str, object] = {
    "no_urls": None,
    "empty_urls": {},
    "no_fapi_public": {"api": {}},
    "api_not_a_mapping": {"api": None},
}


@pytest.mark.parametrize("urls", MISSING_URLS.values(), ids=MISSING_URLS.keys())
def test_a_missing_base_url_is_a_source_error_not_a_crash(urls: object) -> None:
    exchange = FakeExchange()
    exchange.urls = urls  # type: ignore[assignment]
    with pytest.raises(SourceError, match="fapiPublic"):
        open_source(exchange)
    assert exchange.called("load_markets") == 0
    assert exchange.closed


def test_the_exact_usdm_endpoint_is_accepted() -> None:
    exchange = FakeExchange()
    open_source(exchange)
    assert exchange.called("load_markets") == 1


def test_server_time_is_returned_when_it_is_an_exact_int() -> None:
    assert run(open_source(FakeExchange()).fetch_server_time_ms()) == 1792677035725


@pytest.mark.parametrize("response", [{}, {"serverTime": "123"}, {"serverTime": 123.0}, {"serverTime": True}, None, []])
def test_server_time_must_be_an_exact_int(response: object) -> None:
    exchange = FakeExchange()
    exchange.time_response = response
    with pytest.raises(SourceError):
        run(open_source(exchange).fetch_server_time_ms())


@pytest.mark.parametrize("response", [{"rows": []}, None, "[]"], ids=["dict", "none", "str"])
def test_klines_response_must_be_a_list(response: object) -> None:
    exchange = FakeExchange()
    exchange.klines_response = response
    with pytest.raises(SourceError):
        run(open_source(exchange).fetch_klines(SYMBOL, FIVE_MINUTES, 10))


@pytest.mark.parametrize("error", [ccxt.NetworkError("boom"), ccxt.ExchangeError("bad")], ids=["network", "exchange"])
def test_ccxt_errors_become_source_errors(error: ccxt.BaseError) -> None:
    exchange = FakeExchange()
    source = open_source(exchange)
    exchange.error = error
    with pytest.raises(SourceError) as klines:
        run(source.fetch_klines(SYMBOL, FIVE_MINUTES, 10))
    assert klines.value.__cause__ is error
    with pytest.raises(SourceError) as server_time:
        run(source.fetch_server_time_ms())
    assert server_time.value.__cause__ is error


def test_cancellation_passes_through_unchanged() -> None:
    exchange = FakeExchange()
    source = open_source(exchange)
    exchange.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run(source.fetch_klines(SYMBOL, FIVE_MINUTES, 10))


def test_open_failure_closes_the_exchange() -> None:
    exchange = FakeExchange()
    exchange.load_error = ccxt.NetworkError("markets unavailable")
    with pytest.raises(SourceError):
        open_source(exchange)
    assert exchange.closed


def test_close_closes_the_exchange() -> None:
    exchange = FakeExchange()
    source = open_source(exchange)
    assert not exchange.closed
    run(source.close())
    assert exchange.closed


def test_default_exchange_is_the_ccxt_async_usdm_client(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    real = ccxt.async_support.binanceusdm

    async def no_network() -> None:
        return None

    def factory() -> Any:
        exchange = real()
        monkeypatch.setattr(exchange, "load_markets", no_network)
        created.append(exchange)
        return exchange

    monkeypatch.setattr(ccxt.async_support, "binanceusdm", factory)

    async def scenario() -> None:
        source = await BinanceUsdmSource.open()
        await source.close()

    asyncio.run(scenario())
    assert len(created) == 1
    assert isinstance(created[0], real)
    assert created[0].enableRateLimit is True
