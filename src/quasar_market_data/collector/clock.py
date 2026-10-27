"""Time sources the collector injects: monotonic and wall clocks, and an awaitable sleep in milliseconds."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

type Sleep = Callable[[int], Awaitable[None]]


class Clock(Protocol):
    def monotonic_ns(self) -> int: ...

    def wall_ms(self) -> int: ...


class SystemClock:
    def monotonic_ns(self) -> int:
        return time.monotonic_ns()

    def wall_ms(self) -> int:
        return time.time_ns() // 1_000_000


async def system_sleep(ms: int) -> None:
    await asyncio.sleep(ms / 1000)
