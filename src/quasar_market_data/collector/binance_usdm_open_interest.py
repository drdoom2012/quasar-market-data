"""Binance USDT-M open interest history source on its own CCXT client (ADR-0003 P-1, P-5, P-9, P-10).

This is the second module that imports ``ccxt`` (import-linter contract ``ccxt is imported only by the Binance
adapter``). It calls the implicit raw endpoint ``fapiDataGetOpenInterestHist`` (``GET /futures/data/
openInterestHist``) with ``period=5m`` and returns the response untouched, so decimal text reaches the OI-1
parser as Binance sent it; the unified ``fetch_open_interest_history`` (floats) is never used.

Isolation from candle collection (P-9). ``open`` creates a new ``binanceusdm`` client unless one is passed
(tests): own ``load_markets``, own CCXT throttle queue. The module never uses the candle ``RateLimiter`` or
``SharedServerTime`` (an import contract enforces it). Its own server-time observations (``fetch_server_time``,
``fapiPublicGetTime`` through the same client) are counted in the same budget.

Every request, history or time, passes the same steps: ``pause.check()`` (a 429 pause or a 418 stop), one of at
most ``MAX_IN_FLIGHT`` slots, ``budget.acquire()`` (sliding window of starts), ``pause.check()`` again (a call that
waited while another got a 429/418 sends nothing), the CCXT call. The slot is taken before the budget so a call
waiting for a slot does not use up budget it cannot yet spend. CCXT's own throttle (``enableRateLimit``,
``rateLimit`` 50 ms) may still delay the network send after the grant; that only spaces requests further apart.

Share one source (one pause, one budget, one in-flight limit of ``MAX_IN_FLIGHT``) between everything that asks
for open interest in a process: the limit belongs to the instance, so two sources would allow four requests in
flight. The budget is a plain FIFO; "rounds pre-empt backfill" (P-15, OI-4) is OI-4 work on top of it. ``ExtensionPaused``
carries ``remaining_ms`` and ``until_monotonic_ns`` (the pause's monotonic clock) so a caller can schedule around it.

Request rules (P-5) are checked by ``validate_open_interest_request`` before any budget use or request: both
bounds aligned, at most 500 period ends including both bounds, no start older than the 29-day horizon before
``as_of_ms``. A violation raises ``OpenInterestRequestRefused``. An over-long range is refused, never split (the
caller plans pages); ``limit`` is exactly the number of period ends, so Binance cannot truncate (it returns the
*newest* ``limit`` rows of a longer range).

429 and 418 (CCXT 4.5.89, verified): ``binance.handle_errors`` raises ``ccxt.DDoSProtection`` for both, with the
message ``"<id> <status> <reason> <body>"``; ``RateLimitExceeded`` is a different class (body code -1003). A
message starting ``"<id> 418 "`` is a ban. Any other ``DDoSProtection`` or ``RateLimitExceeded`` is a pause (fail
closed: stop asking); only a message starting ``"<id> 429 "`` reads ``Retry-After`` from
``exchange.last_response_headers`` (case-insensitive, a base-10 integer of seconds, otherwise 0). With two
requests in flight that header may belong to the other response; the pause never goes below 300 s. A 429 or 418
on the time request counts too: it is the same IP limit. ``BinanceUsdmSource.fetch_server_time`` wraps CCXT errors
in ``SourceError``, so the original is read from ``__cause__``. ``-1130`` is a ``BadRequest`` and an ordinary
``SourceError``.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final
from urllib.parse import urlsplit

import ccxt
import ccxt.async_support as ccxt_async

from quasar_market_data.collector.binance_usdm import BinanceUsdmSource, usdm_market_id
from quasar_market_data.collector.clock import Clock
from quasar_market_data.collector.extension_pause import ExtensionPause, ExtensionPaused, ExtensionStopped
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.open_interest_budget import OpenInterestBudget
from quasar_market_data.domain.open_interest_request import OpenInterestRequestError, validate_open_interest_request
from quasar_market_data.domain.server_time import ServerTimeObservation
from quasar_market_data.domain.timeframe import Timeframe

MAX_IN_FLIGHT: Final = 2
DEFAULT_BUDGET_PER_5MIN: Final = 400  # ADR-0003 P-10 default; OI-4 reads it from the configuration

_PERIOD: Final = Timeframe("5m")
_DATA_EXPECTED: Final = ("https", "fapi.binance.com", None, None, None, "/futures/data", "", "")

__all__ = [
    "DEFAULT_BUDGET_PER_5MIN",
    "MAX_IN_FLIGHT",
    "BinanceUsdmOpenInterestSource",
    "ExtensionPaused",
    "ExtensionStopped",
    "OpenInterestRequestRefused",
]


class OpenInterestRequestRefused(SourceError):
    """The request breaks a P-5 rule (a caller bug or a range that is no longer retrievable); nothing was sent."""


def _require_fapi_data_url(client: Any) -> None:
    try:
        url = client.urls["api"]["fapiData"]
    except (KeyError, TypeError, AttributeError) as error:
        raise SourceError("the CCXT client has no urls['api']['fapiData'] base URL") from error
    if type(url) is not str:
        raise SourceError(f"fapiData base URL must be a string, got {type(url).__name__}")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise SourceError(f"fapiData base URL {url!r} is malformed") from error
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
    if actual != _DATA_EXPECTED:
        raise SourceError(f"fapiData base URL {url!r} is not https://fapi.binance.com/futures/data")


def _retry_after_ms(headers: object) -> int:
    if not isinstance(headers, Mapping):
        return 0
    for name, value in headers.items():
        if isinstance(name, str) and name.lower() == "retry-after":
            if type(value) is str and value.isascii() and value.isdigit():
                return int(value) * 1000
            return 0
    return 0


class BinanceUsdmOpenInterestSource:
    def __init__(
        self, client: Any, inner: BinanceUsdmSource, budget: OpenInterestBudget, pause: ExtensionPause
    ) -> None:
        self._client = client
        self._inner = inner
        self._budget = budget
        self._pause = pause
        self._in_flight = asyncio.Semaphore(MAX_IN_FLIGHT)

    @classmethod
    async def open(
        cls,
        budget: OpenInterestBudget,
        pause: ExtensionPause,
        clock: Clock | None = None,
        exchange: Any | None = None,
    ) -> "BinanceUsdmOpenInterestSource":
        client = ccxt_async.binanceusdm() if exchange is None else exchange  # a new instance: never shared
        try:
            _require_fapi_data_url(client)
        except BaseException:
            await client.close()
            raise
        inner = await BinanceUsdmSource.open(client, clock)  # checks fapiPublic, loads markets, closes on failure
        return cls(client, inner, budget, pause)

    def _stop_for(self, error: BaseException, what: str) -> SourceError | None:
        """The pause or ban a 429/418 answer means (recorded in the pause), else ``None``."""
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

    async def _request[T](self, what: str, call: Callable[[], Awaitable[T]]) -> T:
        self._pause.check()
        async with self._in_flight:
            await self._budget.acquire()
            self._pause.check()  # the start is counted, but a call that waited sends nothing into a pause
            generation = self._pause.generation
            try:
                result = await call()
            except (ccxt.BaseError, SourceError) as error:
                original = error if isinstance(error, ccxt.BaseError) else error.__cause__
                stop = None if original is None else self._stop_for(original, what)
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
        """The Binance market id of a USDT-M perpetual (the ``symbol`` of every response row), else ``SourceError``."""
        return usdm_market_id(self._client.markets, symbol)

    async def fetch_server_time(self) -> ServerTimeObservation:
        return await self._request("open interest server time request", self._inner.fetch_server_time)

    async def fetch_open_interest_hist(
        self, symbol: str, start_time_ms: int, end_time_ms: int, *, as_of_ms: int
    ) -> list[object]:
        market_id = self.market_id(symbol)
        try:
            limit = validate_open_interest_request(start_time_ms, end_time_ms, _PERIOD, as_of_ms)
        except OpenInterestRequestError as error:
            raise OpenInterestRequestRefused(f"open interest request for {symbol!r} refused: {error}") from error
        params: dict[str, Any] = {
            "symbol": market_id,
            "period": _PERIOD.label,
            "startTime": start_time_ms,
            "endTime": end_time_ms,
            "limit": limit,
        }
        response = await self._request(
            f"open interest request for {symbol!r}", lambda: self._client.fapiDataGetOpenInterestHist(params)
        )
        if type(response) is not list:
            raise SourceError(f"open interest response must be a list, got {type(response).__name__}")
        return response

    async def close(self) -> None:
        await self._inner.close()
