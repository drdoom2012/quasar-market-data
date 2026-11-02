"""Sliding-window budget for open interest requests (ADR-0003 P-10).

``OpenInterestBudget`` allows at most ``budget_per_5min`` request **starts** in any 300 s: a start granted at
``s`` counts until ``s + 300 s`` and no longer from that instant on. It is separate from the candle
``RateLimiter`` (P-9) and is shared by every open interest request of one process (history, server time,
retries, backfill).

One waiter at a time (an ``asyncio.Lock``, FIFO) sleeps until the oldest counted start leaves the window,
rounded up to a whole millisecond, then re-reads the clock and checks again, so an early or delayed wake-up
cannot over-grant. A cancelled waiter, queued or sleeping, consumes nothing.

What this guarantees: adapter call *starts* are counted against the ceiling. The CCXT client has its own
throttle that may delay a call after the grant; that only spaces requests further apart, never closer. Binance
counts per IP: another consumer of ``/futures/data/*`` on the same IP is invisible here (README).
"""

import asyncio
from collections import deque
from typing import Final

from quasar_market_data.collector.clock import Clock, Sleep

WINDOW_MS: Final = 300_000
_NS_PER_MS = 1_000_000
_WINDOW_NS = WINDOW_MS * _NS_PER_MS


class OpenInterestBudget:
    def __init__(self, budget_per_5min: int, clock: Clock, sleep: Sleep) -> None:
        if type(budget_per_5min) is not int:
            raise TypeError(f"budget_per_5min must be int, got {type(budget_per_5min).__name__}")
        if budget_per_5min < 1:
            raise ValueError(f"budget_per_5min must be >= 1, got {budget_per_5min}")
        self._budget = budget_per_5min
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._starts: deque[int] = deque()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock.monotonic_ns()
                while self._starts and self._starts[0] + _WINDOW_NS <= now:
                    self._starts.popleft()
                if len(self._starts) < self._budget:
                    self._starts.append(now)
                    return
                remaining_ns = self._starts[0] + _WINDOW_NS - now
                await self._sleep(-(-remaining_ns // _NS_PER_MS))
