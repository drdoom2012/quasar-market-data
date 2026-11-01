"""Spacing of calls to a ``KlineSource``.

``RateLimiter`` makes consecutive grants at least ``min_interval_ms`` apart on the monotonic clock. It does not
reserve future times: one waiter at a time (an ``asyncio.Lock``, FIFO) sleeps the remaining time, rounded up to
a whole millisecond, then re-reads the clock and checks again, so an early or a delayed wake-up is corrected
and cannot let calls bunch up. The start of a call is the grant, which ``RateLimitedSource`` follows at once
with the inner call (no ``await`` in between). A cancelled waiter, queued or sleeping, consumes nothing.

What this guarantees: the *adapter calls* begin at least ``min_interval_ms`` apart, all symbols together. It does
not guarantee spacing of network requests: the CCXT client has its own throttle that may delay a call after the
grant, and nothing here knows Binance's request-weight limits.
"""

import asyncio
from collections.abc import Sequence

from quasar_market_data.collector.clock import Clock, Sleep
from quasar_market_data.collector.kline_source import KlineSource
from quasar_market_data.domain.server_time import ServerTimeObservation
from quasar_market_data.domain.timeframe import Timeframe

_NS_PER_MS = 1_000_000


class RateLimiter:
    def __init__(self, min_interval_ms: int, clock: Clock, sleep: Sleep) -> None:
        if type(min_interval_ms) is not int:
            raise TypeError(f"min_interval_ms must be int, got {type(min_interval_ms).__name__}")
        if min_interval_ms < 0:
            raise ValueError(f"min_interval_ms must be >= 0, got {min_interval_ms}")
        self._interval_ns = min_interval_ms * _NS_PER_MS
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_start_ns: int | None = None

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock.monotonic_ns()
                if self._last_start_ns is None:
                    remaining_ns = 0
                else:
                    remaining_ns = self._last_start_ns + self._interval_ns - now
                if remaining_ns <= 0:
                    self._last_start_ns = now
                    return
                await self._sleep(-(-remaining_ns // _NS_PER_MS))


class RateLimitedSource:
    def __init__(self, inner: KlineSource, limiter: RateLimiter) -> None:
        self._inner = inner
        self._limiter = limiter

    async def fetch_server_time(self) -> ServerTimeObservation:
        await self._limiter.acquire()
        return await self._inner.fetch_server_time()

    async def fetch_klines(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> Sequence[object]:
        await self._limiter.acquire()
        if start_time_ms is None and end_time_ms is None:
            return await self._inner.fetch_klines(symbol, timeframe, limit)
        return await self._inner.fetch_klines(
            symbol, timeframe, limit, start_time_ms=start_time_ms, end_time_ms=end_time_ms
        )

    async def close(self) -> None:
        await self._inner.close()
