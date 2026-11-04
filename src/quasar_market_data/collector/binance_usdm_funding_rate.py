"""Binance USDT-M funding rate history source on its own CCXT client (ADR-0004 P-1, P-5, P-9).

This is the third module that imports ``ccxt`` (import-linter contract ``ccxt is imported only by the Binance
adapter``). It calls the implicit raw endpoint ``fapiPublicGetFundingRate`` (``GET /fapi/v1/fundingRate``) and
returns the response untouched, so decimal text reaches the FR-1 parser as Binance sent it; the unified
``fetch_funding_rate_history`` (a float rate, no ``markPrice``, no ``rateType``) is never used.

Isolation from candle collection (P-9). ``open`` creates a new ``binanceusdm`` client unless one is passed
(tests): own ``load_markets``, own CCXT throttle queue. The module never uses the candle ``RateLimiter`` or
``SharedServerTime`` or an open interest module (an import contract enforces it). Its own server-time
observations (``fapiPublicGetTime`` through the same client) are class ``R`` requests of the same limiter.

Every request, history or time, passes the same steps: ``pause.check()`` (a 429 pause or a 418 stop), the
limiter's grant (room in the class's group, then one of at most ``MAX_IN_FLIGHT`` slots, then the start is
recorded; waiting for room never holds a slot), ``pause.check()`` again (a call that waited while another got a
429/418 sends nothing), the CCXT call. A send may lag its recorded start by CCXT's own throttle (``enableRateLimit``; tens of
milliseconds for ``fundingRate``); the 5 s guard of the limiter's window covers it.

Share one source (one limiter, one pause) between everything that asks for funding rates in a process.

Request rules (P-5) are checked before the pause, the limiter or the network are touched: both bounds are
timestamps and ``start <= end`` (Binance answers ``200 []`` for the reverse, which would read as an empty
answer); the symbol must be a known, **active**, USDT-margined linear perpetual (so a typo or a delisted
contract never turns into ``200 []``; ``usdm_market_id`` shared with the candle path does not check ``active``,
this adapter does, as P-5 requires); every request sends ``symbol``, ``startTime``, ``endTime`` and
``limit=1000``. A type error of a bound is a caller bug and raises ``TypeError``; anything else breaking a rule
raises ``FundingRateRequestRefused``.

CCXT retries (P-9 [P], verified for CCXT 4.5.89): ``fetch2`` loops ``maxRetriesOnFailure + 1`` times and
retries only ``OperationFailed`` errors (these include ``RequestTimeout``, ``DDoSProtection`` and
``RateLimitExceeded``); the option is read from the request params, ``options['fundingRate']`` and
``options['defaultMaxRetriesOnFailure']`` and its default is 0, so a failed raw call is issued once. Tests pin
both the behaviour and the absence of the option.

429 and 418 (CCXT 4.5.89, verified): ``binance.handle_errors`` raises ``ccxt.DDoSProtection`` for both, with the
message ``"<id> <status> <reason> <body>"``; ``RateLimitExceeded`` is a different class (body code -1003). A
message starting ``"<id> 418 "`` is a ban. Any other ``DDoSProtection`` or ``RateLimitExceeded`` is a pause (fail
closed: stop asking); only a message starting ``"<id> 429 "`` reads ``Retry-After`` from
``exchange.last_response_headers`` (case-insensitive, a base-10 integer of seconds, otherwise 0). With two
requests in flight that header may belong to the other response; the pause never goes below 300 s. A 429 or 418
on the time request counts too: it is the same IP limit. ``-1130`` (an invalid parameter) is a ``BadRequest``
in real CCXT, never a returned object, and an ordinary ``SourceError``.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import ccxt
import ccxt.async_support as ccxt_async

from quasar_market_data.collector.binance_usdm import BinanceUsdmSource, usdm_market_id
from quasar_market_data.collector.clock import Clock
from quasar_market_data.collector.extension_pause import ExtensionPause, ExtensionPaused, ExtensionStopped
from quasar_market_data.collector.funding_rate_limiter import MAX_IN_FLIGHT, FundingRateLimiter, RequestClass
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.funding_rate import FUNDING_RATE_PAGE_LIMIT
from quasar_market_data.domain.server_time import ServerTimeObservation
from quasar_market_data.domain.timestamps import ensure_timestamp_ms

__all__ = [
    "MAX_IN_FLIGHT",
    "BinanceUsdmFundingRateSource",
    "ExtensionPaused",
    "ExtensionStopped",
    "FundingRateRequestRefused",
    "RequestClass",
]


class FundingRateRequestRefused(SourceError):
    """The request breaks a P-5 rule (a caller bug or an unusable symbol); nothing was sent."""


def _retry_after_ms(headers: object) -> int:
    if not isinstance(headers, Mapping):
        return 0
    for name, value in headers.items():
        if isinstance(name, str) and name.lower() == "retry-after":
            if type(value) is str and value.isascii() and value.isdigit():
                return int(value) * 1000
            return 0
    return 0


class BinanceUsdmFundingRateSource:
    def __init__(
        self, client: Any, inner: BinanceUsdmSource, limiter: FundingRateLimiter, pause: ExtensionPause
    ) -> None:
        self._client = client
        self._inner = inner
        self._limiter = limiter
        self._pause = pause

    @classmethod
    async def open(
        cls,
        limiter: FundingRateLimiter,
        pause: ExtensionPause,
        clock: Clock | None = None,
        exchange: Any | None = None,
    ) -> "BinanceUsdmFundingRateSource":
        client = ccxt_async.binanceusdm() if exchange is None else exchange  # a new instance: never shared
        inner = await BinanceUsdmSource.open(client, clock)  # checks fapiPublic, loads markets, closes on failure
        return cls(client, inner, limiter, pause)

    def _stop_for(self, error: BaseException) -> SourceError | None:
        """The pause or stop a 429/418 answer means (recorded in the pause), else ``None``."""
        if not isinstance(error, (ccxt.DDoSProtection, ccxt.RateLimitExceeded)):
            return None
        exchange_id = str(getattr(self._client, "id", ""))
        text = str(error)
        if text.startswith(f"{exchange_id} 418 "):
            self._pause.record_banned()
            return ExtensionStopped()
        retry_after = _retry_after_ms(getattr(self._client, "last_response_headers", None))
        if not text.startswith(f"{exchange_id} 429 "):
            retry_after = 0
        self._pause.record_rate_limited(retry_after)
        return self._pause.paused()

    async def _request[T](self, what: str, request_class: RequestClass, call: Callable[[], Awaitable[T]]) -> T:
        self._pause.check()
        async with self._limiter.grant(request_class):
            self._pause.check()  # the start is counted, but a call that waited sends nothing into a pause
            generation = self._pause.generation
            try:
                result = await call()
            except (ccxt.BaseError, SourceError) as error:
                original = error if isinstance(error, ccxt.BaseError) else error.__cause__
                stop = None if original is None else self._stop_for(original)
                if stop is not None:
                    raise stop from error
                if isinstance(error, SourceError):
                    if not isinstance(original, ccxt.BaseError):
                        self._pause.record_success(generation)  # the answer arrived; the adapter rejected its content
                    raise
                raise SourceError(f"{what} failed: {error}") from error
            self._pause.record_success(generation)
            return result

    def market_id(self, symbol: str) -> str:
        """The Binance market id of an **active** USDT-M perpetual (the ``symbol`` of every response row), else a refusal."""
        try:
            market_id = usdm_market_id(self._client.markets, symbol)
        except SourceError as error:
            raise FundingRateRequestRefused(f"funding rate request for {symbol!r} refused: {error}") from error
        if self._client.markets[symbol].get("active") is not True:
            raise FundingRateRequestRefused(f"funding rate request for {symbol!r} refused: the market is not active")
        return market_id

    async def fetch_server_time(self, request_class: RequestClass = RequestClass.R) -> ServerTimeObservation:
        return await self._request("funding rate server time request", request_class, self._inner.fetch_server_time)

    async def fetch_funding_rate_history(
        self, symbol: str, start_time_ms: int, end_time_ms: int, *, request_class: RequestClass
    ) -> list[object]:
        market_id = self.market_id(symbol)
        try:
            start = ensure_timestamp_ms(start_time_ms, "start_time_ms")
            end = ensure_timestamp_ms(end_time_ms, "end_time_ms")
        except ValueError as error:
            raise FundingRateRequestRefused(f"funding rate request for {symbol!r} refused: {error}") from error
        if start > end:
            raise FundingRateRequestRefused(
                f"funding rate request for {symbol!r} refused: start_time_ms {start} is after end_time_ms {end}"
            )
        if type(request_class) is not RequestClass:
            raise TypeError(f"request_class must be RequestClass, got {type(request_class).__name__}")
        params: dict[str, Any] = {
            "symbol": market_id,
            "startTime": start,
            "endTime": end,
            "limit": FUNDING_RATE_PAGE_LIMIT,
        }
        response = await self._request(
            f"funding rate request for {symbol!r}", request_class, lambda: self._client.fapiPublicGetFundingRate(params)
        )
        if type(response) is not list:
            raise SourceError(f"funding rate response must be a list, got {type(response).__name__}")
        return response

    async def close(self) -> None:
        await self._inner.close()
