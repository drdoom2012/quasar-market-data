"""Binance USDT-M perpetual kline source over async CCXT raw endpoints (ADR-0001 D-A, P-1, P-9).

This is the only module that imports ``ccxt`` (import-linter contract ``ccxt is imported only by the Binance
adapter``). It uses the implicit raw endpoints ``fapiPublicGetKlines`` and ``fapiPublicGetTime``; the unified
``fetch_ohlcv`` is never used, and no value is converted, so decimal text reaches the parser untouched.

Fail closed before any klines request: the ``fapiPublic`` base URL must be exactly the USDT-M endpoint, the
market must be a USDT-margined linear perpetual, and the timeframe must be one ADR-0001 measured.
"""

from typing import Any, Final
from urllib.parse import urlsplit

import ccxt
import ccxt.async_support as ccxt_async

from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.timeframe import Timeframe

SUPPORTED_TIMEFRAMES: Final = frozenset({"5m"})  # the only timeframe ADR-0001 measured

_FAPI_SCHEME: Final = "https"
_FAPI_HOST: Final = "fapi.binance.com"
_FAPI_PATH: Final = "/fapi/v1"


def _require_fapi_base_url(url: object) -> None:
    if type(url) is not str:
        raise SourceError(f"fapiPublic base URL must be a string, got {type(url).__name__}")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise SourceError(f"fapiPublic base URL {url!r} is malformed") from error
    expected = (_FAPI_SCHEME, _FAPI_HOST, None, None, None, _FAPI_PATH, "", "")
    actual = (
        parts.scheme,
        parts.hostname,
        port,
        parts.username,
        parts.password,
        parts.path,
        parts.query,
        parts.fragment,
    )
    if actual != expected:
        raise SourceError(f"fapiPublic base URL {url!r} is not https://{_FAPI_HOST}{_FAPI_PATH}")


def _market_id(markets: object, symbol: str) -> str:
    market = markets.get(symbol) if isinstance(markets, dict) else None
    if not isinstance(market, dict):
        raise SourceError(f"unknown symbol {symbol!r}")
    checks = {
        "swap": market.get("swap") is True,
        "linear": market.get("linear") is True,
        "settle == USDT": market.get("settle") == "USDT",
        "quote == USDT": market.get("quote") == "USDT",
        "no expiry": market.get("expiry") is None,
    }
    for name, passed in checks.items():
        if not passed:
            raise SourceError(f"{symbol!r} is not a USDT-M perpetual: failed check {name!r}")
    market_id = market.get("id")
    if type(market_id) is not str or not market_id:
        raise SourceError(f"{symbol!r} has no market id")
    return market_id


class BinanceUsdmSource:
    def __init__(self, exchange: Any) -> None:
        self._exchange = exchange

    @classmethod
    async def open(cls, exchange: Any | None = None) -> "BinanceUsdmSource":
        client = ccxt_async.binanceusdm() if exchange is None else exchange
        try:
            try:
                base_url = client.urls["api"]["fapiPublic"]
            except (KeyError, TypeError) as error:
                raise SourceError("the CCXT client has no urls['api']['fapiPublic'] base URL") from error
            _require_fapi_base_url(base_url)
            await client.load_markets()
        except SourceError:
            await client.close()
            raise
        except ccxt.BaseError as error:
            await client.close()
            raise SourceError(f"loading the markets failed: {error}") from error
        except BaseException:
            await client.close()
            raise
        return cls(client)

    async def fetch_server_time_ms(self) -> int:
        try:
            response = await self._exchange.fapiPublicGetTime()
        except ccxt.BaseError as error:
            raise SourceError(f"server time request failed: {error}") from error
        server_time = response.get("serverTime") if isinstance(response, dict) else None
        if type(server_time) is not int:
            raise SourceError(f"serverTime must be an int, got {type(server_time).__name__}")
        return server_time

    async def fetch_klines(self, symbol: str, timeframe: Timeframe, limit: int) -> list[object]:
        market_id = _market_id(self._exchange.markets, symbol)
        if timeframe.label not in SUPPORTED_TIMEFRAMES:
            raise SourceError(
                f"timeframe {timeframe.label!r} is not verified (supported: {sorted(SUPPORTED_TIMEFRAMES)})"
            )
        try:
            response = await self._exchange.fapiPublicGetKlines(
                {"symbol": market_id, "interval": timeframe.label, "limit": limit}
            )
        except ccxt.BaseError as error:
            raise SourceError(f"klines request for {symbol!r} failed: {error}") from error
        if type(response) is not list:
            raise SourceError(f"klines response must be a list, got {type(response).__name__}")
        return response

    async def close(self) -> None:
        await self._exchange.close()
