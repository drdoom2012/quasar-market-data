"""Local limiter for funding rate requests: the 305 s window, class caps and the in-flight slots (ADR-0004 P-9).

``FundingRateLimiter`` allows a request of a class to **start** at ``t`` iff fewer than the cap of its group
started in ``(t - 305 s, t]`` (left-open, right-closed): a start at ``s`` counts until ``s + 305 s`` and not from
that instant on. The window is the documented 300 s plus a 5 s guard for network jitter. The two groups are never
lent to each other: ``R`` (rounds, seeds and the round's server-time call) has the cap ``m = floor(b / 2)``, ``C``
(catch-up) and ``H`` (history) share ``b - m``. The caps sum to ``b``, so the total never exceeds ``b``. A group
that is full does not delay the other one.

The same object owns the at most ``MAX_IN_FLIGHT`` request slots. ``grant`` is an async context manager that
is entered right before the CCXT call and left after it:

1. take the group's FIFO lock;
2. wait (sleep) until the group has room, without holding a slot;
3. take a slot, still holding the group lock, so the room cannot shrink;
4. record the start with a fresh clock reading and release the group lock.

A group that waits for room therefore never holds a slot against the other group, and a start is recorded only
when a slot is held, so the sends of a group stay at least 305 s apart in the sense of the rule above. A waiter
that is cancelled (for example by the caller's deadline) before step 4 consumes nothing and holds neither lock nor
slot. CCXT's own throttle may still delay the network send after the grant; that only spaces requests further
apart. The limiter says nothing about other processes on the same IP (README).

Memory of starts is pruned on the next grant, so ``starts_in_window`` is exact for instants not earlier than the
previous grant's clock reading.
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

from quasar_market_data.collector.clock import Clock, Sleep
from quasar_market_data.domain.funding_rate_capacity import (
    LIMITER_WINDOW_MS,
    RequestClass,
    history_catchup_share,
    round_share,
)

MAX_IN_FLIGHT: Final = 2
WINDOW_MS: Final = LIMITER_WINDOW_MS
_NS_PER_MS: Final = 1_000_000
_WINDOW_NS: Final = WINDOW_MS * _NS_PER_MS

__all__ = ["MAX_IN_FLIGHT", "WINDOW_MS", "FundingRateLimiter", "RequestClass"]


@dataclass
class _Group:
    cap: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    starts: deque[int] = field(default_factory=deque)


class FundingRateLimiter:
    def __init__(self, budget_per_5min: int, clock: Clock, sleep: Sleep, *, max_in_flight: int = MAX_IN_FLIGHT) -> None:
        if type(max_in_flight) is not int:
            raise TypeError(f"max_in_flight must be int, got {type(max_in_flight).__name__}")
        if max_in_flight < 1:
            raise ValueError(f"max_in_flight must be >= 1, got {max_in_flight}")
        self._rounds = _Group(round_share(budget_per_5min))  # validates 50 <= b <= 250 and the type
        self._other = _Group(history_catchup_share(budget_per_5min))
        self._clock = clock
        self._sleep = sleep
        self._slots = asyncio.Semaphore(max_in_flight)

    def _group(self, request_class: RequestClass) -> _Group:
        if type(request_class) is not RequestClass:
            raise TypeError(f"request_class must be RequestClass, got {type(request_class).__name__}")
        return self._rounds if request_class is RequestClass.R else self._other

    @asynccontextmanager
    async def _granted(self, group: _Group) -> AsyncIterator[None]:
        async with group.lock:
            while True:
                now = self._clock.monotonic_ns()
                while group.starts and group.starts[0] + _WINDOW_NS <= now:
                    group.starts.popleft()
                if len(group.starts) < group.cap:
                    break
                remaining_ns = group.starts[0] + _WINDOW_NS - now
                await self._sleep(-(-remaining_ns // _NS_PER_MS))
            await self._slots.acquire()  # cancelled here: nothing recorded, the lock is released by ``async with``
            group.starts.append(self._clock.monotonic_ns())
        try:
            yield
        finally:
            self._slots.release()

    def grant(self, request_class: RequestClass) -> AbstractAsyncContextManager[None]:
        """Async context manager: room in the class's group, a slot, then the start is recorded."""
        return self._granted(self._group(request_class))

    def starts_in_window(self, at_ns: int, request_class: RequestClass | None = None) -> int:
        """Counted starts whose window still covers ``at_ns`` (a start at ``s`` counts while ``at_ns < s + 305 s``).

        ``R`` counts the round group, ``C`` or ``H`` the shared catch-up and history group, ``None`` both. A
        read-only view; it neither removes entries nor reads the clock.
        """
        if type(at_ns) is not int:
            raise TypeError(f"at_ns must be int, got {type(at_ns).__name__}")
        groups = (self._rounds, self._other) if request_class is None else (self._group(request_class),)
        return sum(1 for group in groups for start in group.starts if start + _WINDOW_NS > at_ns)
