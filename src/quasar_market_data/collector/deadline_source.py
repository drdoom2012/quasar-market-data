"""A ``KlineSource`` wrapper that cancels the network, never a write (S16 D5).

Each call is raced against a timeout, against the idle window's monotonic deadline and against ``stop``. The
loser is cancelled *and awaited*, so no request is left running behind a returned call. It wraps source calls
only and holds no sink: a database write that has started is never cancelled from here. It reads no wall clock,
so a wall-clock step cannot extend or cut a window.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from quasar_market_data.collector.clock import Clock, Sleep
from quasar_market_data.collector.kline_source import KlineSource, SourceError
from quasar_market_data.domain.timeframe import Timeframe

_NS_PER_MS = 1_000_000


class DeadlineSource:
    def __init__(
        self,
        inner: KlineSource,
        *,
        timeout_ms: int,
        deadline_mono_ns: int,
        clock: Clock,
        sleep: Sleep,
        stop: asyncio.Event,
    ) -> None:
        if type(timeout_ms) is not int or timeout_ms < 1:
            raise ValueError(f"timeout_ms must be an int >= 1, got {timeout_ms!r}")
        self._inner = inner
        self._timeout_ms = timeout_ms
        self._deadline_ns = deadline_mono_ns
        self._clock = clock
        self._sleep = sleep
        self._stop = stop

    async def _race[T](self, call: Callable[[], Awaitable[T]]) -> T:
        if self._stop.is_set():
            raise SourceError("stopped")
        left_ms = (self._deadline_ns - self._clock.monotonic_ns()) // _NS_PER_MS
        if left_ms <= 0:
            raise SourceError("deadline reached")
        budget_ms = min(self._timeout_ms, left_ms)
        inner = asyncio.ensure_future(call())
        sleeper = asyncio.ensure_future(self._sleep(budget_ms))
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({inner, sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            finished = inner.done()
            for task in (inner, sleeper, stopper):
                task.cancel()
            await asyncio.gather(inner, sleeper, stopper, return_exceptions=True)
        if finished:
            return inner.result()
        if self._stop.is_set():
            raise SourceError("stopped")
        raise SourceError("deadline reached" if left_ms < self._timeout_ms else "timed out")

    async def fetch_server_time_ms(self) -> int:
        return await self._race(self._inner.fetch_server_time_ms)

    async def fetch_klines(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> Sequence[object]:
        return await self._race(
            lambda: self._inner.fetch_klines(
                symbol, timeframe, limit, start_time_ms=start_time_ms, end_time_ms=end_time_ms
            )
        )

    async def close(self) -> None:
        await self._inner.close()
