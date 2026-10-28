"""The idle-window hook, lateness, overrun and skipped-round accounting of the collection loop (S16, D5)."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from quasar_market_data.collector.candle_writer import CandleWriteOutcomeUnknown
from quasar_market_data.collector.collection_loop import (
    IDLE_RESERVE_MS,
    FatalLoopError,
    IdleWindow,
    LoopExit,
    RoundReport,
)
from quasar_market_data.domain.schedule import next_wake_ms
from tests.collector.fetch_fakes import ControlledSink
from tests.collector.loop_fakes import DELAY_MS
from tests.collector.manual_time import NS_PER_MS, assert_no_leftover_tasks
from tests.collector.test_collection_loop import FIVE, TF_MS, Harness

PLANNED_AFTER_START = 279_000  # ManualTime starts 36 s into a cycle; the next boundary is 279 s away


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(scenario())


class Idle:
    """An ``idle_work`` that records its windows and runs a scripted action inside each one."""

    def __init__(self, h: Harness, action: Callable[[IdleWindow], Coroutine[Any, Any, None]] | None = None) -> None:
        self.h = h
        self.action = action
        self.windows: list[IdleWindow] = []
        self.active = False

    async def __call__(self, window: IdleWindow) -> None:
        self.windows.append(window)
        self.h.events.append("idle_start")
        self.active = True
        try:
            if self.action is not None:
                await self.action(window)
        finally:
            self.active = False
            self.h.events.append("idle_end")


def harness(
    action: Callable[[IdleWindow], Coroutine[Any, Any, None]] | None = None, **kwargs: Any
) -> tuple[Harness, Idle]:
    h = Harness(**kwargs)
    idle = Idle(h, action)
    h.run_kwargs["idle_work"] = idle
    return h, idle


def overrun_by(h: Harness, extra_ms: int) -> Callable[[IdleWindow], Coroutine[Any, Any, None]]:
    """A hook that ends ``extra_ms`` after its window's due time (monotonic and wall move together)."""

    async def action(window: IdleWindow) -> None:
        await h.t.advance((window.due_mono_ns - h.t.monotonic_ns()) // NS_PER_MS + extra_ms)

    return action


def test_idle_work_is_not_called_when_there_is_no_room() -> None:
    async def scenario() -> None:
        h, idle = harness()
        await h.t.advance(269_000)  # 10 s before the boundary: less than IDLE_RESERVE_MS
        h.start()
        await h.t.settle()
        assert len(h.rounds) == 1 and idle.windows == []
        assert 10_000 < IDLE_RESERVE_MS
        h.stop.set()
        await h.task

    run(scenario)


def test_the_window_carries_monotonic_due_and_deadline_and_the_loops_source_and_sinks() -> None:
    async def scenario() -> None:
        h, idle = harness()
        now = h.t.monotonic_ns()
        h.start()
        await h.t.settle()
        (window,) = idle.windows
        assert window.due_mono_ns == now + PLANNED_AFTER_START * NS_PER_MS
        assert window.deadline_mono_ns == window.due_mono_ns - IDLE_RESERVE_MS * NS_PER_MS
        assert window.source is h.source and window.stop is h.stop
        h.stop.set()
        await h.task

    run(scenario)


def test_idle_work_never_overlaps_a_round_and_runs_after_on_round() -> None:
    async def scenario() -> None:
        h, idle = harness()
        in_hook_at_fetch: list[bool] = []
        real = h.source.fetch_klines

        async def spy(*args: Any, **kwargs: Any) -> Any:
            in_hook_at_fetch.append(idle.active)
            return await real(*args, **kwargs)

        h.source.fetch_klines = spy  # type: ignore[method-assign]
        h.start()
        await h.until_rounds(1)
        await h.advance_to_next_boundary()
        await h.until_rounds(2)
        await h.t.settle()
        assert h.events == ["on_round", "idle_start", "idle_end", "on_round", "idle_start", "idle_end"]
        assert in_hook_at_fetch and not any(in_hook_at_fetch)
        h.stop.set()
        await h.task

    run(scenario)


def test_backfill_starts_only_after_the_first_round_and_its_hook() -> None:
    async def scenario() -> None:
        h, _ = harness()

        async def after_first(report: RoundReport) -> None:
            h.events.append("after_first_round")

        h.run_kwargs["after_first_round"] = after_first
        h.start()
        await h.t.settle()
        assert h.events == ["on_round", "after_first_round", "idle_start", "idle_end"]
        h.stop.set()
        await h.task

    run(scenario)


def test_normal_cycle_reports_zero_lateness_overrun_and_skips() -> None:
    async def scenario() -> None:
        h, _ = harness()
        h.start()
        await h.until_rounds(1)
        for i in (1, 2):
            await h.advance_to_next_boundary()
            await h.until_rounds(i + 1)
        for report in h.rounds:
            assert (report.lateness_ms, report.idle_overrun_ms, report.skipped_rounds, report.clock_step_ms) == (
                0,
                0,
                0,
                0,
            )
        h.stop.set()
        await h.task

    run(scenario)


def test_an_idle_overrun_starts_the_round_at_once_and_reports_it() -> None:
    async def scenario() -> None:
        h, _ = harness()
        h.run_kwargs["idle_work"].action = overrun_by(h, 40_000)
        planned = next_wake_ms(h.t.wall_ms(), FIVE, DELAY_MS)
        h.start()
        await h.until_rounds(2)
        second = h.rounds[1]
        assert second.started_wall_ms == planned + 40_000  # no sleep, no jump to the following boundary
        assert (second.lateness_ms, second.idle_overrun_ms, second.skipped_rounds) == (40_000, 40_000, 0)
        h.stop.set()
        await h.task

    run(scenario)


@pytest.mark.parametrize(("extra", "skipped"), [(TF_MS + 1, 1), (3 * TF_MS + 1, 3)])
def test_an_idle_overrun_across_boundaries_reports_the_skipped_rounds(extra: int, skipped: int) -> None:
    async def scenario() -> None:
        h, _ = harness()
        h.run_kwargs["idle_work"].action = overrun_by(h, extra)
        h.start()
        await h.until_rounds(2)
        second = h.rounds[1]
        assert (second.skipped_rounds, second.idle_overrun_ms, second.lateness_ms) == (skipped, extra, extra)
        h.stop.set()
        await h.task

    run(scenario)


def test_repeated_overruns_do_not_accumulate() -> None:
    async def scenario() -> None:
        h, _ = harness()
        h.run_kwargs["idle_work"].action = overrun_by(h, 10_000)
        h.start()
        await h.until_rounds(4)
        for report in h.rounds[1:4]:
            assert (report.lateness_ms, report.idle_overrun_ms, report.skipped_rounds) == (10_000, 10_000, 0)
        h.stop.set()
        await h.task

    run(scenario)


def test_a_forward_wall_step_during_the_hook_is_a_clock_event_not_an_overrun() -> None:
    async def scenario() -> None:
        h, _ = harness()

        async def action(window: IdleWindow) -> None:
            h.t._wall_ms += 3_600_000  # the wall clock jumps; the monotonic clock only moves 1 s
            await h.t.advance(1_000)

        h.run_kwargs["idle_work"].action = action
        h.start()
        await h.until_rounds(1)
        await h.t.settle()
        await h.t.settle()  # the hook's own settle and the loop's sleep registration need these turns
        assert h.t.waiting == [h.t.monotonic_ns() + (PLANNED_AFTER_START - 1_000) * NS_PER_MS]  # sleep not lengthened
        await h.t.advance(PLANNED_AFTER_START - 1_000)
        await h.until_rounds(2)
        second = h.rounds[1]
        assert (second.idle_overrun_ms, second.lateness_ms) == (0, 0)
        assert second.skipped_rounds == 12 and second.clock_step_ms == 3_600_000
        h.stop.set()
        await h.task

    run(scenario)


def test_a_backward_wall_step_during_the_hook_does_not_stall_or_go_negative() -> None:
    async def scenario() -> None:
        h, _ = harness()

        async def action(window: IdleWindow) -> None:
            h.t._wall_ms -= 3_600_000
            await h.t.advance(1_000)

        h.run_kwargs["idle_work"].action = action
        h.start()
        await h.until_rounds(1)
        await h.t.settle()
        await h.t.settle()  # the hook's own settle and the loop's sleep registration need these turns
        assert h.t.waiting == [h.t.monotonic_ns() + (PLANNED_AFTER_START - 1_000) * NS_PER_MS]
        await h.t.advance(PLANNED_AFTER_START - 1_000)
        await h.until_rounds(2)
        second = h.rounds[1]
        assert (second.skipped_rounds, second.idle_overrun_ms, second.lateness_ms) == (0, 0, 0)
        assert second.clock_step_ms == -3_600_000
        assert min(second.skipped_rounds, second.idle_overrun_ms, second.lateness_ms) >= 0
        h.stop.set()
        await h.task

    run(scenario)


def test_steps_below_the_divergence_gate_are_not_reported() -> None:
    async def scenario() -> None:
        h, _ = harness()

        async def action(window: IdleWindow) -> None:
            h.t._wall_ms += 50
            await h.t.advance(1_000)

        h.run_kwargs["idle_work"].action = action
        h.start()
        await h.until_rounds(1)
        await h.t.settle()
        await h.t.settle()
        await h.t.advance(PLANNED_AFTER_START - 1_000)
        await h.until_rounds(2)
        assert h.rounds[1].clock_step_ms == 0
        h.stop.set()
        await h.task

    run(scenario)


def test_a_slow_round_that_skips_a_boundary_reports_skipped_without_idle_overrun() -> None:
    async def scenario() -> None:
        h, _ = harness()
        h.start()
        await h.until_rounds(1)
        await h.advance_to_next_boundary()
        await h.until_rounds(2)
        await h.t.settle()
        h.source.delays = [TF_MS + 10_000]  # the next round takes longer than a whole cycle
        await h.advance_to_next_boundary()
        await h.t.until_waiting(1)
        await h.t.advance(TF_MS + 10_000)
        await h.until_rounds(3)
        await h.t.settle()
        await h.advance_to_next_boundary()
        await h.until_rounds(4)
        fourth = h.rounds[3]
        assert (fourth.lateness_ms, fourth.idle_overrun_ms, fourth.skipped_rounds) == (0, 0, 1)
        h.stop.set()
        await h.task

    run(scenario)


def test_stop_during_the_hook_returns_promptly() -> None:
    async def scenario() -> None:
        h, _ = harness()

        async def action(window: IdleWindow) -> None:
            await window.stop.wait()

        h.run_kwargs["idle_work"].action = action
        h.start()
        await h.t.settle()
        assert not h.task.done()
        h.stop.set()
        assert await h.task == LoopExit(())
        await assert_no_leftover_tasks()

    run(scenario)


def test_idle_work_failure_is_fatal_and_keeps_the_uncertain_symbols() -> None:
    async def scenario() -> None:
        sink = ControlledSink(raises=[None, CandleWriteOutcomeUnknown("unknown")])
        h, _ = harness(sink=sink)

        async def action(window: IdleWindow) -> None:
            with pytest.raises(CandleWriteOutcomeUnknown):
                await window.sink_for("A").write([], ingested_at_ms=0)
            raise RuntimeError("backfill bug")

        h.run_kwargs["idle_work"].action = action
        h.start()
        with pytest.raises(FatalLoopError) as caught:
            await h.task
        assert caught.value.uncertain_symbols == ("A",)
        assert isinstance(caught.value.__cause__, RuntimeError)
        assert h.stop.is_set()

    run(scenario)


def test_an_unknown_write_outcome_during_the_hook_marks_the_symbol_uncertain() -> None:
    async def scenario() -> None:
        sink = ControlledSink(raises=[None, CandleWriteOutcomeUnknown("unknown")])
        h, _ = harness(sink=sink)

        async def action(window: IdleWindow) -> None:
            with pytest.raises(CandleWriteOutcomeUnknown):
                await window.sink_for("A").write([], ingested_at_ms=0)

        h.run_kwargs["idle_work"].action = action
        h.start()
        await h.t.settle()
        h.stop.set()
        assert await h.task == LoopExit(("A",))

    run(scenario)


def test_without_a_hook_the_loop_sleeps_to_the_next_boundary_as_in_s14() -> None:
    async def scenario() -> None:
        h = Harness()
        h.start()
        await h.t.settle()
        assert h.t.waiting == [h.t.monotonic_ns() + PLANNED_AFTER_START * NS_PER_MS]
        h.stop.set()
        await h.task

    run(scenario)
