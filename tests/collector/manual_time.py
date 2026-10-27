"""Controlled test time: nothing advances unless the test says so.

``ManualTime`` is both the ``Clock`` and the ``Sleep`` of a test. ``sleep(ms)`` registers a waiter and awaits
it; ``advance`` moves time and wakes the waiters whose deadline has passed. Tests that need concurrent waiters
use this; ``fetch_fakes.advancing_sleep`` is only for tests with a single waiter.
"""

import asyncio
from dataclasses import dataclass, field

NS_PER_MS = 1_000_000
YIELDS = 100  # event-loop hops granted after every wake-up so awakened tasks run until they block again
MAX_WAIT_YIELDS = 1_000


@dataclass(order=True)
class _Waiter:
    deadline_ns: int
    seq: int
    future: "asyncio.Future[None]" = field(compare=False)


class ManualTime:
    def __init__(self, wall_start_ms: int = 1_792_677_036_000, mono_start_ns: int = 18_053_000_000_000) -> None:
        self._mono_ns = mono_start_ns
        self._wall_ms = wall_start_ms
        self._waiters: list[_Waiter] = []
        self._seq = 0

    def monotonic_ns(self) -> int:
        return self._mono_ns

    def wall_ms(self) -> int:
        return self._wall_ms

    @property
    def waiting(self) -> list[int]:
        """Sorted deadlines (monotonic ns) of the registered waiters."""
        return sorted(w.deadline_ns for w in self._waiters)

    async def sleep(self, ms: int) -> None:
        waiter = _Waiter(self._mono_ns + ms * NS_PER_MS, self._seq, asyncio.get_running_loop().create_future())
        self._seq += 1
        self._waiters.append(waiter)
        try:
            await waiter.future
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    async def settle(self) -> None:
        for _ in range(YIELDS):
            await asyncio.sleep(0)

    async def until_waiting(self, count: int) -> None:
        for _ in range(MAX_WAIT_YIELDS):
            if len(self._waiters) == count:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"expected {count} waiters, have {len(self._waiters)}")

    def _move_to(self, target_ns: int) -> None:
        self._wall_ms += (target_ns - self._mono_ns) // NS_PER_MS
        self._mono_ns = target_ns

    async def advance_ns(self, ns: int, *, late: bool = False) -> None:
        """Move time forward by ``ns``. By default every due waiter wakes at its own deadline, in order; with
        ``late=True`` time jumps first and all due waiters wake at the final instant (a delayed wake-up)."""
        target = self._mono_ns + ns
        if late:
            self._move_to(target)
        while True:
            due = sorted(w for w in self._waiters if w.deadline_ns <= target and not w.future.done())
            if not due:
                break
            waiter = due[0]
            if not late:
                self._move_to(max(self._mono_ns, waiter.deadline_ns))
            waiter.future.set_result(None)
            await self.settle()
        self._move_to(target)
        await self.settle()

    async def advance(self, ms: int, *, late: bool = False) -> None:
        await self.advance_ns(ms * NS_PER_MS, late=late)


async def assert_no_leftover_tasks() -> None:
    """Fail when a task other than the caller's is still alive after letting the loop settle."""
    for _ in range(YIELDS):
        await asyncio.sleep(0)
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not leftover, f"leftover tasks: {leftover}"
