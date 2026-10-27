import asyncio
import itertools
from collections.abc import Callable, Coroutine, Sequence

import pytest

from quasar_market_data.collector.clock import Sleep
from quasar_market_data.collector.rate_limiter import RateLimitedSource, RateLimiter
from quasar_market_data.domain.timeframe import Timeframe
from tests.collector.manual_time import NS_PER_MS, ManualTime, assert_no_leftover_tasks
from tests.typing_helpers import untyped

INTERVAL = 100


def run(scenario: Callable[[], Coroutine[None, None, None]]) -> None:
    asyncio.run(scenario())


def limiter(t: ManualTime, interval: int = INTERVAL, sleep: Sleep | None = None) -> RateLimiter:
    return RateLimiter(interval, t, sleep or t.sleep)


async def granted(lim: RateLimiter, t: ManualTime, into: list[int]) -> None:
    await lim.acquire()
    into.append(t.monotonic_ns())


def test_manual_time_wakes_due_waiters_in_deadline_order() -> None:
    async def scenario() -> None:
        t = ManualTime()
        order: list[int] = []

        async def waiter(ms: int) -> None:
            await t.sleep(ms)
            order.append(ms)

        tasks = [asyncio.create_task(waiter(ms)) for ms in (30, 10, 20)]
        await t.until_waiting(3)
        await t.advance(25)
        assert order == [10, 20]
        await t.advance(5)
        await asyncio.gather(*tasks)
        assert order == [10, 20, 30]

    run(scenario)


def test_manual_time_removes_a_cancelled_waiter() -> None:
    async def scenario() -> None:
        t = ManualTime()
        task = asyncio.create_task(t.sleep(50))
        await t.until_waiting(1)
        task.cancel()
        await t.settle()
        assert t.waiting == []
        await assert_no_leftover_tasks()

    run(scenario)


def test_first_acquire_does_not_wait() -> None:
    async def scenario() -> None:
        t = ManualTime()
        await limiter(t).acquire()
        assert t.waiting == []

    run(scenario)


def test_second_acquire_waits_the_remaining_interval() -> None:
    async def scenario() -> None:
        t = ManualTime()
        lim = limiter(t)
        starts: list[int] = []
        await granted(lim, t, starts)
        await t.advance(30)
        task = asyncio.create_task(granted(lim, t, starts))
        await t.until_waiting(1)
        assert t.waiting == [starts[0] + INTERVAL * NS_PER_MS]
        await t.advance(70)
        await task
        assert starts[1] == starts[0] + INTERVAL * NS_PER_MS

    run(scenario)


def test_late_wake_of_many_waiters_does_not_burst() -> None:
    async def scenario() -> None:
        t = ManualTime()
        lim = limiter(t)
        starts: list[int] = []
        tasks = [asyncio.create_task(granted(lim, t, starts)) for _ in range(5)]
        await t.until_waiting(1)  # the first was granted at once, its successor sleeps
        await t.advance(1_000, late=True)
        for _ in range(3):
            await t.advance(INTERVAL)
        await asyncio.gather(*tasks)
        assert len(starts) == 5
        gaps = [b - a for a, b in itertools.pairwise(starts)]
        assert all(gap >= INTERVAL * NS_PER_MS for gap in gaps), gaps
        assert len(set(starts)) == 5

    run(scenario)


def test_early_wake_is_rechecked() -> None:
    async def scenario() -> None:
        t = ManualTime()
        early = [True]

        async def sleep(ms: int) -> None:
            if early[0]:
                early[0] = False
                await t.sleep(ms - 1)  # wakes 1 ms before the limiter's deadline
            else:
                await t.sleep(ms)

        lim = limiter(t, sleep=sleep)
        starts: list[int] = []
        await granted(lim, t, starts)
        task = asyncio.create_task(granted(lim, t, starts))
        await t.until_waiting(1)
        await t.advance(INTERVAL - 1)
        await t.until_waiting(1)  # woke early, re-read the clock, sleeps the remaining 1 ms
        assert not task.done()
        await t.advance(1)
        await task
        assert starts[1] == starts[0] + INTERVAL * NS_PER_MS

    run(scenario)


def test_fractional_remainder_rounds_up() -> None:
    async def scenario() -> None:
        t = ManualTime()
        lim = limiter(t, interval=1)
        await lim.acquire()
        await t.advance_ns(600_000)  # 0.6 ms later, 0.4 ms remain
        task = asyncio.create_task(lim.acquire())
        await t.until_waiting(1)
        assert t.waiting == [t.monotonic_ns() + NS_PER_MS]  # a whole millisecond, never 0
        await t.advance(1)
        await task

    run(scenario)


def test_zero_interval_never_sleeps() -> None:
    async def scenario() -> None:
        t = ManualTime()
        lim = limiter(t, interval=0)
        for _ in range(3):
            await lim.acquire()
        assert t.waiting == []

    run(scenario)


def test_interval_already_passed_does_not_wait() -> None:
    async def scenario() -> None:
        t = ManualTime()
        lim = limiter(t)
        await lim.acquire()
        await t.advance(INTERVAL + 5)
        await lim.acquire()
        assert t.waiting == []

    run(scenario)


def test_cancelled_sleeping_holder_consumes_nothing() -> None:
    async def scenario() -> None:
        t = ManualTime()
        lim = limiter(t)
        starts: list[int] = []
        await granted(lim, t, starts)
        second = asyncio.create_task(lim.acquire())
        await t.until_waiting(1)
        second.cancel()
        await t.settle()
        assert t.waiting == []
        third = asyncio.create_task(granted(lim, t, starts))
        await t.until_waiting(1)
        await t.advance(INTERVAL)
        await third
        assert starts[1] == starts[0] + INTERVAL * NS_PER_MS  # not 200
        await assert_no_leftover_tasks()

    run(scenario)


def test_cancelled_waiter_queued_on_the_lock_consumes_nothing() -> None:
    async def scenario() -> None:
        t = ManualTime()
        lim = limiter(t)
        starts: list[int] = []
        await granted(lim, t, starts)
        holder = asyncio.create_task(granted(lim, t, starts))
        await t.until_waiting(1)
        queued = asyncio.create_task(lim.acquire())
        last = asyncio.create_task(granted(lim, t, starts))
        await t.settle()
        queued.cancel()
        await t.settle()
        await t.advance(INTERVAL)
        await holder
        await t.until_waiting(1)
        await t.advance(INTERVAL)
        await last
        assert starts == [starts[0], starts[0] + INTERVAL * NS_PER_MS, starts[0] + 2 * INTERVAL * NS_PER_MS]
        await assert_no_leftover_tasks()

    run(scenario)


class RecordingSource:
    def __init__(self, t: ManualTime) -> None:
        self.t = t
        self.starts: list[int] = []
        self.closed = False

    async def fetch_server_time_ms(self) -> int:
        self.starts.append(self.t.monotonic_ns())
        return 0

    async def fetch_klines(self, symbol: str, timeframe: Timeframe, limit: int) -> Sequence[object]:
        self.starts.append(self.t.monotonic_ns())
        return []

    async def close(self) -> None:
        self.closed = True


def test_rate_limited_source_acquires_before_each_call_and_not_for_close() -> None:
    async def scenario() -> None:
        t = ManualTime()
        inner = RecordingSource(t)
        source = RateLimitedSource(inner, limiter(t))
        await source.fetch_server_time_ms()
        task = asyncio.create_task(source.fetch_klines("BTC/USDT:USDT", Timeframe("5m"), 10))
        await t.until_waiting(1)
        assert len(inner.starts) == 1  # the second call has not begun while the limiter holds it back
        await source.close()
        assert inner.closed and t.waiting != []  # close did not acquire, the klines call still waits
        await t.advance(INTERVAL)
        await task
        assert inner.starts[1] - inner.starts[0] == INTERVAL * NS_PER_MS

    run(scenario)


@pytest.mark.parametrize("value", [True, 1.5])
def test_invalid_interval_type(value: object) -> None:
    with pytest.raises(TypeError):
        RateLimiter(untyped(value), ManualTime(), ManualTime().sleep)


def test_negative_interval() -> None:
    with pytest.raises(ValueError):
        RateLimiter(-1, ManualTime(), ManualTime().sleep)
