"""The optional hooks the candle path gives open interest (ADR-0003 P-9 rules 2 and 5)."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from quasar_market_data.collector.candle_write_activity import CandleWriteActivity
from quasar_market_data.collector.candle_writer import CandleWriteOutcomeUnknown
from quasar_market_data.collector.collection_loop import CandleWriteHooks
from tests.collector.fetch_fakes import ControlledSink
from tests.collector.manual_time import ManualTime
from tests.collector.test_collection_loop import FIVE, Harness

DURATIONS: list[int] = []


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(scenario())


def hooked(h: Harness, record: Callable[[int], None] | None = None) -> CandleWriteActivity:
    activity = CandleWriteActivity()
    h.run_kwargs["candle_write_hooks"] = CandleWriteHooks(activity, record or (lambda ns: None))
    return activity


def test_the_activity_is_true_while_a_candle_write_runs_and_false_after() -> None:
    async def scenario() -> None:
        sink = ControlledSink(block=True)
        h = Harness(sink=sink)
        activity = hooked(h)
        assert not activity.active
        h.start()
        await sink.write_started.wait()
        assert activity.active
        sink.release.set()
        await h.until_rounds(1)
        await h.t.settle()
        assert not activity.active
        h.stop.set()
        await h.task

    run(scenario)


def test_the_activity_is_left_when_a_write_raises() -> None:
    async def scenario() -> None:
        sink = ControlledSink(raises=[CandleWriteOutcomeUnknown("unknown")])
        h = Harness(sink=sink)
        activity = hooked(h)
        h.start()
        await h.t.settle()
        await h.t.advance(1_000)  # the unknown outcome is retried after the first backoff step
        await h.until_rounds(1)
        await h.t.settle()
        assert not activity.active
        h.stop.set()
        await h.task

    run(scenario)


def test_two_concurrent_symbol_writes_keep_the_activity_until_both_finish() -> None:
    async def scenario() -> None:
        sink = ControlledSink(block=True)
        h = Harness(("A", "B"), sink=sink)
        activity = hooked(h)
        h.start()
        await h.t.settle()
        assert len(sink.calls) == 2 and activity.active
        sink.release.set()
        await h.until_rounds(1)
        await h.t.settle()
        assert not activity.active
        h.stop.set()
        await h.task

    run(scenario)


def test_a_cancelled_write_leaves_the_activity() -> None:
    async def scenario() -> None:
        sink = ControlledSink(block=True)
        h = Harness(sink=sink, shutdown_grace_ms=1_000)
        activity = hooked(h)
        h.start()
        await sink.write_started.wait()
        h.stop.set()
        await h.t.settle()
        await h.t.advance(1_000)  # the shutdown grace expires and the blocked symbol task is cancelled
        await h.task
        assert not activity.active

    run(scenario)


def test_the_duration_of_every_write_is_reported_including_a_failed_one() -> None:
    async def scenario() -> None:
        durations: list[int] = []
        h = Harness(sink=ControlledSink(raises=[CandleWriteOutcomeUnknown("unknown")]))
        t = h.t

        original = h.sink.write

        async def slow(candles: Any, *, ingested_at_ms: int) -> Any:
            t_start = t.monotonic_ns()
            await t.sleep(40)
            assert t.monotonic_ns() - t_start == 40_000_000
            return await original(candles, ingested_at_ms=ingested_at_ms)

        h.sink.write = slow  # type: ignore[method-assign]
        hooked(h, durations.append)
        h.start()
        await t.settle()
        await t.advance(40)  # the first write ends with an unknown outcome
        await t.settle()
        await t.advance(1_000)  # backoff, then the retry
        await t.settle()
        await t.advance(40)
        await h.until_rounds(1)
        assert durations == [40_000_000, 40_000_000]  # the failed write is measured too
        h.stop.set()
        await h.task

    run(scenario)


def test_a_raising_callback_does_not_change_the_candle_result() -> None:
    async def scenario() -> None:
        baseline = Harness()
        baseline.start()
        await baseline.until_rounds(1)
        baseline.stop.set()
        await baseline.task

        def boom(ns: int) -> None:
            raise RuntimeError("callback bug")

        h = Harness()
        hooked(h, boom)
        h.start()
        await h.until_rounds(1)
        h.stop.set()
        await h.task
        assert h.rounds[0].results == baseline.rounds[0].results

    run(scenario)


def test_without_hooks_the_write_path_reads_no_extra_clock() -> None:
    async def scenario() -> None:
        class SpyTime(ManualTime):
            reads = 0

            def monotonic_ns(self) -> int:
                SpyTime.reads += 1
                return super().monotonic_ns()

        plain, hooked_run = Harness(), Harness()
        plain.t = SpyTime()
        plain.source.t = plain.t
        plain.start()
        await plain.until_rounds(1)
        plain.stop.set()
        await plain.task
        without = SpyTime.reads

        SpyTime.reads = 0
        hooked_run.t = SpyTime()
        hooked_run.source.t = hooked_run.t
        hooked(hooked_run)
        hooked_run.start()
        await hooked_run.until_rounds(1)
        hooked_run.stop.set()
        await hooked_run.task
        assert without < SpyTime.reads  # the hook reads the clock around the write; the plain path does not
        assert plain.rounds[0].results == hooked_run.rounds[0].results

    run(scenario)


def test_backfill_writes_in_the_idle_window_enter_the_activity() -> None:
    async def scenario() -> None:
        sink = ControlledSink(block=True)
        h = Harness(sink=sink)
        activity = hooked(h)
        seen: list[bool] = []

        async def idle(window: Any) -> None:
            sink.release.clear()
            sink_for = window.sink_for("A")
            task = asyncio.create_task(sink_for.write([], ingested_at_ms=h.t.wall_ms()))
            await h.t.settle()
            seen.append(activity.active)
            sink.release.set()
            await task
            seen.append(activity.active)

        h.run_kwargs["idle_work"] = idle
        sink.release.set()  # round one's writes pass at once
        h.start()
        await h.until_rounds(1)
        await h.t.settle()
        h.stop.set()
        await h.task
        assert seen == [True, False]
        assert FIVE.milliseconds == 300_000

    run(scenario)
