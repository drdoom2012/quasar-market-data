import asyncio
import inspect
from collections.abc import Callable, Coroutine, Sequence

import pytest

from quasar_market_data.collector.deadline_source import DeadlineSource
from quasar_market_data.collector.kline_source import KlineSource, SourceError
from quasar_market_data.collector.rate_limiter import RateLimitedSource, RateLimiter
from quasar_market_data.domain.timeframe import Timeframe
from tests.collector.manual_time import NS_PER_MS, ManualTime, assert_no_leftover_tasks

TF = Timeframe("5m")
SYMBOL = "BTC/USDT:USDT"
TIMEOUT_MS = 5_000


def run(scenario: Callable[[], Coroutine[None, None, None]]) -> None:
    asyncio.run(scenario())


class Inner:
    """A source whose calls wait for ``release`` (or return at once) and can ignore cancellation."""

    def __init__(self, *, hang: bool = True, ignore_cancel_once: bool = False) -> None:
        self.hang = hang
        self.ignore_cancel_once = ignore_cancel_once
        self.release = asyncio.Event()
        self.started = 0
        self.ended = 0
        self.cancelled = 0

    async def _wait(self) -> None:
        self.started += 1
        try:
            if self.hang:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            if self.ignore_cancel_once:
                self.ignore_cancel_once = False
                await self.release.wait()
            else:
                raise
        finally:
            self.ended += 1

    async def fetch_server_time_ms(self) -> int:
        await self._wait()
        return 42

    async def fetch_klines(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> Sequence[object]:
        await self._wait()
        return [["row"]]

    async def close(self) -> None:
        self.ended += 100


def wrap(
    inner: KlineSource, t: ManualTime, *, deadline_ms: int = 60_000, stop: asyncio.Event | None = None
) -> DeadlineSource:
    return DeadlineSource(
        inner,
        timeout_ms=TIMEOUT_MS,
        deadline_mono_ns=t.monotonic_ns() + deadline_ms * NS_PER_MS,
        clock=t,
        sleep=t.sleep,
        stop=stop or asyncio.Event(),
    )


def test_a_slow_call_is_cancelled_at_the_timeout_with_a_source_error() -> None:
    async def scenario() -> None:
        t = ManualTime()
        inner = Inner()
        source = wrap(inner, t)
        task = asyncio.ensure_future(source.fetch_klines(SYMBOL, TF, 10))
        await t.until_waiting(1)
        await t.advance(TIMEOUT_MS - 1)
        assert not task.done()
        await t.advance(1)
        with pytest.raises(SourceError, match="timed out"):
            await task
        assert inner.cancelled == 1
        server = asyncio.ensure_future(source.fetch_server_time_ms())
        await t.until_waiting(1)
        await t.advance(TIMEOUT_MS)
        with pytest.raises(SourceError, match="timed out"):
            await server
        await assert_no_leftover_tasks()

    run(scenario)


def test_a_stuck_limiter_is_cancelled_at_the_timeout_and_consumes_no_slot() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = RateLimiter(10_000, t, t.sleep)
        grants: list[int] = []

        async def take() -> None:
            await limiter.acquire()
            grants.append(t.monotonic_ns())

        await take()  # grant 0
        holder = asyncio.ensure_future(take())  # sleeps 10 s holding the lock
        await t.until_waiting(1)
        source = wrap(RateLimitedSource(Inner(hang=False), limiter), t)
        task = asyncio.ensure_future(source.fetch_klines(SYMBOL, TF, 10))
        await t.until_waiting(2)
        await t.advance(TIMEOUT_MS)
        with pytest.raises(SourceError, match="timed out"):
            await task
        await t.advance(TIMEOUT_MS)
        await holder  # granted at 10 s
        later = asyncio.ensure_future(take())
        await t.until_waiting(1)
        await t.advance(10_000)
        await later
        step = 10_000 * NS_PER_MS
        assert grants == [grants[0], grants[0] + step, grants[0] + 2 * step]
        await assert_no_leftover_tasks()

    run(scenario)


def test_stop_cancels_an_in_flight_call_at_once() -> None:
    async def scenario() -> None:
        t = ManualTime()
        stop = asyncio.Event()
        inner = Inner()
        task = asyncio.ensure_future(wrap(inner, t, stop=stop).fetch_klines(SYMBOL, TF, 10))
        await t.until_waiting(1)
        stop.set()
        with pytest.raises(SourceError, match="stopped"):
            await task
        assert inner.cancelled == 1
        with pytest.raises(SourceError, match="stopped"):  # and a call after stop never starts
            await wrap(inner, t, stop=stop).fetch_klines(SYMBOL, TF, 10)
        assert inner.started == 1
        await assert_no_leftover_tasks()

    run(scenario)


def test_the_deadline_shortens_the_timeout() -> None:
    async def scenario() -> None:
        t = ManualTime()
        source = wrap(Inner(), t, deadline_ms=1_200)
        task = asyncio.ensure_future(source.fetch_klines(SYMBOL, TF, 10))
        await t.until_waiting(1)
        await t.advance(1_199)
        assert not task.done()
        await t.advance(1)
        with pytest.raises(SourceError, match="deadline reached"):
            await task
        with pytest.raises(SourceError, match="deadline reached"):  # nothing is left: no call is started
            await source.fetch_klines(SYMBOL, TF, 10)

    run(scenario)


@pytest.mark.parametrize("step_ms", [3_600_000, -3_600_000], ids=["wall_forward", "wall_backward"])
def test_a_wall_clock_step_does_not_move_the_deadline(step_ms: int) -> None:
    async def scenario() -> None:
        t = ManualTime()
        source = wrap(Inner(), t)
        task = asyncio.ensure_future(source.fetch_klines(SYMBOL, TF, 10))
        await t.until_waiting(1)
        t._wall_ms += step_ms  # the wall clock steps; the monotonic clock does not
        await t.advance(TIMEOUT_MS - 1)
        assert not task.done()
        await t.advance(1)
        with pytest.raises(SourceError, match="timed out"):
            await task

    run(scenario)


def test_a_call_that_finishes_in_time_is_not_disturbed() -> None:
    async def scenario() -> None:
        t = ManualTime()
        inner = Inner(hang=False)
        source = wrap(inner, t)
        assert await source.fetch_server_time_ms() == 42
        assert await source.fetch_klines(SYMBOL, TF, 10) == [["row"]]
        assert inner.cancelled == 0
        await source.close()
        assert inner.ended >= 100
        await assert_no_leftover_tasks()

    run(scenario)


def test_an_inner_error_passes_through_unchanged() -> None:
    class Failing(Inner):
        async def _wait(self) -> None:
            raise SourceError("boom")

    async def scenario() -> None:
        t = ManualTime()
        with pytest.raises(SourceError, match="boom"):
            await wrap(Failing(), t).fetch_klines(SYMBOL, TF, 10)
        await assert_no_leftover_tasks()

    run(scenario)


def test_no_task_is_left_behind() -> None:
    async def scenario() -> None:
        t = ManualTime()
        stop = asyncio.Event()
        source = wrap(Inner(), t, stop=stop)
        for how in ("timeout", "stop"):
            task = asyncio.ensure_future(source.fetch_klines(SYMBOL, TF, 10))
            await t.until_waiting(1)
            if how == "timeout":
                await t.advance(TIMEOUT_MS)
            else:
                stop.set()
            with pytest.raises(SourceError):
                await task
            await assert_no_leftover_tasks()
            assert t.waiting == []
            stop = asyncio.Event()
            source = wrap(Inner(), t, stop=stop)

    run(scenario)


def test_an_outer_cancellation_cancels_the_inner_call() -> None:
    async def scenario() -> None:
        t = ManualTime()
        inner = Inner()
        task = asyncio.ensure_future(wrap(inner, t).fetch_klines(SYMBOL, TF, 10))
        await t.until_waiting(1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert inner.cancelled == 1
        await assert_no_leftover_tasks()

    run(scenario)


def test_a_cancelled_call_that_ignores_cancellation_for_a_while_is_still_awaited_not_abandoned() -> None:
    async def scenario() -> None:
        t = ManualTime()
        inner = Inner(ignore_cancel_once=True)
        task = asyncio.ensure_future(wrap(inner, t).fetch_klines(SYMBOL, TF, 10))
        await t.until_waiting(1)
        await t.advance(TIMEOUT_MS)
        await t.settle()
        assert not task.done()  # the inner call is still unwinding, so we are still waiting for it
        inner.release.set()
        with pytest.raises(SourceError, match="timed out"):
            await task
        assert inner.ended == 1  # it finished before we returned: no orphan request
        await assert_no_leftover_tasks()

    run(scenario)


def test_the_deadline_source_has_no_sink() -> None:
    assert "sink" not in " ".join(inspect.signature(DeadlineSource.__init__).parameters)
