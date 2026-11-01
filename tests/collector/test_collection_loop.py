import asyncio
import itertools
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from quasar_market_data.collector.candle_writer import CandleWriteOutcomeUnknown
from quasar_market_data.collector.collection_loop import (
    FatalLoopError,
    LoopConfig,
    LoopExit,
    RetryPolicy,
    RoundReport,
    SymbolOutcome,
    SymbolResult,
    run_collection_loop,
)
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.rate_limiter import RateLimitedSource, RateLimiter
from quasar_market_data.domain.schedule import next_wake_ms
from quasar_market_data.domain.server_time import ServerTimeObservation
from quasar_market_data.domain.timeframe import Timeframe
from tests.collector.fetch_fakes import ControlledSink
from tests.collector.loop_fakes import DELAY_MS, ScriptedSource
from tests.collector.manual_time import NS_PER_MS, ManualTime, assert_no_leftover_tasks
from tests.typing_helpers import untyped

FIVE = Timeframe("5m")
TF_MS = 300_000
GRACE = 10_000
WAITS = [1000, 2000, 4000, 8000, 8000]
FIRST_TARGET = 1_792_676_700_000  # the newest slot confirmable at ManualTime's start
EXPIRED = "shutdown grace expired"


def expired_result(symbol: str) -> SymbolResult:
    return SymbolResult(symbol, SymbolOutcome.STOPPED, None, None, None, None, EXPIRED)


def config(symbols: tuple[str, ...], **kwargs: Any) -> LoopConfig:
    return LoopConfig(symbols=symbols, timeframe=FIVE, limit=100, **kwargs)


class Harness:
    def __init__(self, symbols: tuple[str, ...] = ("A",), *, sink: ControlledSink | None = None, **cfg: Any) -> None:
        self.symbols = symbols
        self.cfg = cfg
        self.t = ManualTime()
        self.source = ScriptedSource(self.t)
        self.sink = sink or ControlledSink()
        self.stop = asyncio.Event()
        self.rounds: list[RoundReport] = []
        self.events: list[str] = []
        self.run_kwargs: dict[str, Any] = {}
        self.task: asyncio.Task[LoopExit]

    def start(self) -> None:
        self.task = asyncio.create_task(
            run_collection_loop(
                self.source,
                self.sink,
                config(self.symbols, **self.cfg),
                self.t,
                self.t.sleep,
                self.stop,
                self.on_round,
                **self.run_kwargs,
            )
        )

    def on_round(self, report: RoundReport) -> None:
        self.rounds.append(report)
        self.events.append("on_round")

    async def until_rounds(self, n: int) -> None:
        for _ in range(1_000):
            if len(self.rounds) >= n:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"expected {n} rounds, have {len(self.rounds)}")

    async def advance_to_next_boundary(self) -> int:
        await self.t.settle()  # let the loop register its boundary wait before time moves
        wake = next_wake_ms(self.t.wall_ms(), FIVE, DELAY_MS)
        await self.t.advance(wake - self.t.wall_ms())
        return wake


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(scenario())


def test_first_round_runs_immediately_then_waits_for_the_boundary() -> None:
    async def scenario() -> None:
        h = Harness()
        start_wall = h.t.wall_ms()
        h.start()
        await h.t.settle()
        assert len(h.rounds) == 1
        assert h.rounds[0].started_wall_ms == start_wall and h.rounds[0].target_open_ms == FIRST_TARGET
        wake = next_wake_ms(h.t.wall_ms(), FIVE, DELAY_MS)
        assert h.t.waiting == [h.t.monotonic_ns() + (wake - h.t.wall_ms()) * NS_PER_MS]
        h.stop.set()
        assert await h.task == LoopExit(())
        assert h.t.waiting == []
        await assert_no_leftover_tasks()

    run(scenario)


def test_rounds_follow_every_boundary() -> None:
    async def scenario() -> None:
        h = Harness()
        h.start()
        await h.until_rounds(1)
        for i in (1, 2):
            wake = await h.advance_to_next_boundary()
            await h.until_rounds(i + 1)
            assert h.rounds[i].started_wall_ms == wake
            previous = h.rounds[i - 1].target_open_ms
            assert previous is not None and h.rounds[i].target_open_ms == previous + TF_MS
            assert h.rounds[i].results[0].outcome is SymbolOutcome.COLLECTED
        h.stop.set()
        await h.task

    run(scenario)


def test_concurrency_never_exceeds_the_bound() -> None:
    async def scenario() -> None:
        symbols = tuple(f"S{i}" for i in range(10))
        h = Harness(symbols, max_concurrency=3)
        h.source.gate = asyncio.Event()
        h.start()
        await h.t.settle()
        assert h.source.active == 3 and h.source.max_active == 3
        h.source.gate.set()
        await h.until_rounds(1)
        assert h.source.max_active == 3
        assert [r.outcome for r in h.rounds[0].results] == [SymbolOutcome.COLLECTED] * 10
        h.stop.set()
        await h.task

    run(scenario)


def test_results_are_in_symbol_order_whatever_finishes_first() -> None:
    async def scenario() -> None:
        h = Harness(("A", "B", "C"))
        h.source.gates = {s: asyncio.Event() for s in "ABC"}
        h.start()
        await h.t.settle()
        for symbol in "CBA":
            h.source.gates[symbol].set()
            await h.t.settle()
        await h.until_rounds(1)
        assert [r.symbol for r in h.rounds[0].results] == ["A", "B", "C"]
        h.stop.set()
        await h.task

    run(scenario)


def test_a_failing_symbol_does_not_stop_the_others() -> None:
    async def scenario() -> None:
        h = Harness(("A", "B", "C"))
        h.source.errors["B"] = SourceError("down")
        h.start()
        for ms in WAITS:
            await h.t.until_waiting(1)
            await h.t.advance(ms)
        await h.until_rounds(1)
        outcomes = {r.symbol: r.outcome for r in h.rounds[0].results}
        assert outcomes == {"A": SymbolOutcome.COLLECTED, "B": SymbolOutcome.GAVE_UP, "C": SymbolOutcome.COLLECTED}
        assert not h.stop.is_set()  # a retryable error never sets stop
        calls_before = h.source.klines_calls.count("A")
        await h.advance_to_next_boundary()
        await h.t.settle()
        assert h.source.klines_calls.count("A") == calls_before + 1  # the next round still ran
        h.stop.set()
        await h.task

    run(scenario)


def test_slow_round_skips_boundaries_instead_of_bursting() -> None:
    async def scenario() -> None:
        h = Harness()
        h.source.delays = [650_000]  # the first klines call takes more than two timeframes
        h.start()
        await h.t.until_waiting(1)
        await h.t.advance(650_000)
        await h.until_rounds(1)
        now = h.t.wall_ms()
        wake = next_wake_ms(now, FIVE, DELAY_MS)
        assert wake - now <= TF_MS and wake > now
        assert h.t.waiting == [h.t.monotonic_ns() + (wake - now) * NS_PER_MS]  # one wake, not a burst
        await h.t.advance(wake - now)
        await h.until_rounds(2)
        assert h.rounds[1].started_wall_ms == wake
        assert h.source.max_active == 1  # no two rounds overlapped
        assert len(h.source.klines_calls) == 2
        h.stop.set()
        await h.task

    run(scenario)


def test_stop_before_the_first_round_runs_no_round() -> None:
    async def scenario() -> None:
        h = Harness()
        h.stop.set()
        h.start()
        assert await h.task == LoopExit(())
        assert h.rounds == [] and h.source.klines_calls == []

    run(scenario)


def test_stop_while_waiting_for_a_boundary_returns_with_no_uncertainty() -> None:
    async def scenario() -> None:
        h = Harness()
        h.start()
        await h.until_rounds(1)
        await h.t.until_waiting(1)
        h.stop.set()
        assert await h.task == LoopExit(())
        assert h.t.waiting == []
        await assert_no_leftover_tasks()

    run(scenario)


def test_stop_during_a_retry_wait_ends_the_symbol_as_stopped() -> None:
    async def scenario() -> None:
        h = Harness()
        h.source.errors["A"] = SourceError("down")
        h.start()
        await h.t.until_waiting(1)
        h.stop.set()
        assert await h.task == LoopExit(())
        result = h.rounds[0].results[0]
        assert result.outcome is SymbolOutcome.STOPPED and result.attempts == 1
        await assert_no_leftover_tasks()

    run(scenario)


def test_stop_leaves_queued_semaphore_waiters_unstarted() -> None:
    async def scenario() -> None:
        h = Harness(("A", "B", "C", "D"), max_concurrency=1)
        h.source.gate = asyncio.Event()
        h.start()
        await h.t.settle()
        h.stop.set()
        await h.t.until_waiting(1)  # the grace timer
        h.source.gate.set()
        assert await h.task == LoopExit(())
        outcomes = [(r.outcome, r.attempts) for r in h.rounds[0].results]
        assert outcomes == [(SymbolOutcome.COLLECTED, 1)] + [(SymbolOutcome.STOPPED, 0)] * 3
        assert h.source.klines_calls == ["A"]
        await assert_no_leftover_tasks()

    run(scenario)


def test_stop_during_an_accepted_unfinished_write_starts_the_grace_at_once() -> None:
    async def scenario() -> None:
        h = Harness(sink=ControlledSink(block=True))
        h.start()
        await h.sink.write_started.wait()  # the write is accepted and unfinished
        stop_instant = h.t.monotonic_ns()
        h.stop.set()
        await h.t.until_waiting(1)
        assert h.t.waiting == [stop_instant + GRACE * NS_PER_MS]
        assert not h.task.done()
        h.sink.release.set()
        assert await h.task == LoopExit(())
        assert h.rounds[0].results[0].outcome is SymbolOutcome.COLLECTED
        assert h.t.waiting == []
        await assert_no_leftover_tasks()

    run(scenario)


def test_grace_timeout_during_a_write_gives_stopped_and_sets_the_uncertainty() -> None:
    async def scenario() -> None:
        h = Harness(sink=ControlledSink(block=True))
        h.start()
        await h.sink.write_started.wait()
        h.stop.set()
        await h.t.until_waiting(1)
        await h.t.advance(GRACE - 1)
        assert not h.task.done()
        await h.t.advance(1)
        assert await h.task == LoopExit(("A",))
        assert h.rounds[0].results == (expired_result("A"),)
        assert h.rounds[0].uncertain_symbols == ("A",)
        await assert_no_leftover_tasks()

    run(scenario)


def test_grace_timeout_during_a_fetch_gives_stopped_and_no_uncertainty() -> None:
    async def scenario() -> None:
        h = Harness()
        h.source.gate = asyncio.Event()  # nobody ever sets it
        h.start()
        await h.t.settle()
        stop_instant = h.t.monotonic_ns()
        h.stop.set()  # no exception anywhere
        await h.t.until_waiting(1)
        assert h.t.waiting == [stop_instant + GRACE * NS_PER_MS]
        await h.t.advance(GRACE - 1)
        assert not h.task.done()
        await h.t.advance(1)
        assert await h.task == LoopExit(())
        assert h.rounds[0].results == (expired_result("A"),)
        assert h.sink.calls == []  # it never reached write
        await assert_no_leftover_tasks()

    run(scenario)


def test_after_stop_the_loop_waits_only_on_unfinished_tasks_and_does_not_spin(monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []
    original = asyncio.wait

    async def recording_wait(fs: Any, **kwargs: Any) -> Any:
        assert all(not task.done() for task in fs), "a completed task was passed to asyncio.wait"
        sizes.append(len(fs))
        return await original(fs, **kwargs)

    async def scenario() -> None:
        h = Harness(("A", "B"))
        h.source.gates = {"A": asyncio.Event(), "B": asyncio.Event()}
        monkeypatch.setattr(asyncio, "wait", recording_wait)
        h.start()
        await h.t.settle()
        h.stop.set()
        await h.t.until_waiting(1)  # stop observed, grace timer running
        calls = len(sizes)
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(sizes) == calls  # blocked on real events, not re-waiting on a completed task
        assert sizes[-1] == 3  # A, B, the grace timer (the stop-wait task is gone)
        h.source.gates["A"].set()
        await h.t.settle()
        assert len(sizes) == calls + 1 and sizes[-1] == 2  # B and the grace timer
        await h.t.advance(GRACE)
        await h.task

    run(scenario)


def test_normally_finished_task_keeps_its_real_result_when_another_expires() -> None:
    async def scenario() -> None:
        h = Harness(("A", "B"))
        h.source.gates = {"A": asyncio.Event(), "B": asyncio.Event()}
        h.start()
        await h.t.settle()
        h.stop.set()
        await h.t.until_waiting(1)
        h.source.gates["A"].set()
        await h.t.settle()
        await h.t.advance(GRACE)
        await h.task
        a, b = h.rounds[0].results
        assert (a.symbol, a.outcome, a.attempts, a.inserted) == ("A", SymbolOutcome.COLLECTED, 1, 3)
        assert b == expired_result("B")

    run(scenario)


def test_unknown_write_then_source_error_then_stop_stays_flagged() -> None:
    async def scenario() -> None:
        h = Harness(sink=ControlledSink(raises=[CandleWriteOutcomeUnknown("unknown")]))
        h.source.script["A"] = [None, SourceError("down")]
        h.start()
        await h.t.until_waiting(1)
        await h.t.advance(1000)  # attempt 2: the fetch fails, no write
        await h.t.until_waiting(1)
        h.stop.set()
        assert await h.task == LoopExit(("A",))
        result = h.rounds[0].results[0]
        assert result.outcome is SymbolOutcome.STOPPED and result.attempts == 2

    run(scenario)


def test_unknown_write_then_a_different_batch_succeeds_flag_persists_across_rounds() -> None:
    async def scenario() -> None:
        h = Harness(sink=ControlledSink(raises=[CandleWriteOutcomeUnknown("unknown")]))
        h.source.windows["A"] = [3, 2]  # the second attempt writes a different batch
        h.start()
        await h.t.until_waiting(1)
        await h.t.advance(1000)
        await h.until_rounds(1)
        assert h.rounds[0].results[0].outcome is SymbolOutcome.COLLECTED
        assert h.rounds[0].uncertain_symbols == ("A",)
        assert len(h.sink.calls[0]) == 3 and len(h.sink.calls[1]) == 2
        await h.advance_to_next_boundary()
        await h.until_rounds(2)
        assert h.rounds[1].results[0].outcome is SymbolOutcome.COLLECTED
        assert h.rounds[1].uncertain_symbols == ("A",)
        h.stop.set()
        assert await h.task == LoopExit(("A",))

    run(scenario)


def test_fatal_error_with_queued_symbols_never_calls_the_source_again() -> None:
    async def scenario() -> None:
        h = Harness(("A", "B", "C"), max_concurrency=1)
        boom = RuntimeError("boom")
        h.source.errors["A"] = boom
        h.start()
        with pytest.raises(FatalLoopError) as caught:
            await h.task
        assert h.source.klines_calls == ["A"]
        assert caught.value.uncertain_symbols == () and caught.value.__cause__ is boom
        assert h.rounds == []  # a round that ended in a fatal error is not reported
        await assert_no_leftover_tasks()

    run(scenario)


def test_cancelled_error_is_not_swallowed_by_the_symbol_wrapper() -> None:
    async def scenario() -> None:
        h = Harness()
        h.source.gate = asyncio.Event()
        h.start()
        await h.t.settle()
        (symbol_task,) = [t for t in asyncio.all_tasks() if t.get_name() == "collect-A"]
        symbol_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await h.task
        assert not h.stop.is_set()
        await assert_no_leftover_tasks()

    run(scenario)


def test_fatal_error_lets_a_running_write_finish_within_the_grace() -> None:
    async def scenario() -> None:
        h = Harness(("A", "B"), sink=ControlledSink(block=True))
        failure = asyncio.Event()
        h.source.fail_after["A"] = (failure, RuntimeError("boom"))
        h.start()
        await h.sink.write_started.wait()  # B's write is running, A has not failed yet
        failure.set()
        await h.t.until_waiting(1)  # the grace timer
        h.sink.release.set()
        with pytest.raises(FatalLoopError) as caught:
            await h.task
        assert caught.value.uncertain_symbols == () and len(h.sink.calls) == 1
        await assert_no_leftover_tasks()

    run(scenario)


def test_fatal_error_plus_unfinished_write_reports_both() -> None:
    async def scenario() -> None:
        h = Harness(("A", "B"), sink=ControlledSink(block=True))
        boom = RuntimeError("boom")
        failure = asyncio.Event()
        h.source.fail_after["A"] = (failure, boom)
        h.start()
        await h.sink.write_started.wait()
        failure.set()
        await h.t.until_waiting(1)
        await h.t.advance(GRACE)
        with pytest.raises(FatalLoopError) as caught:
            await h.task
        assert caught.value.uncertain_symbols == ("B",) and caught.value.__cause__ is boom
        await assert_no_leftover_tasks()

    run(scenario)


def test_stop_and_fatal_error_in_one_shutdown_do_not_extend_the_grace() -> None:
    async def scenario() -> None:
        h = Harness(("A", "B"), sink=ControlledSink(block=True))
        boom = RuntimeError("boom")
        failure = asyncio.Event()
        h.source.fail_after["A"] = (failure, boom)
        h.start()
        await h.sink.write_started.wait()  # B's write is parked, A waits to fail
        t0 = h.t.monotonic_ns()
        h.stop.set()
        await h.t.until_waiting(1)
        deadline = t0 + GRACE * NS_PER_MS
        assert h.t.waiting == [deadline]
        await h.t.advance(GRACE // 2)
        failure.set()  # a fatal error during the shutdown
        h.stop.set()  # and a repeated stop
        await h.t.settle()
        assert h.t.waiting == [deadline]  # one timer, not restarted, not extended
        await h.t.advance(GRACE // 2)
        with pytest.raises(FatalLoopError) as caught:
            await h.task
        assert caught.value.uncertain_symbols == ("B",) and caught.value.__cause__ is boom
        await assert_no_leftover_tasks()

    run(scenario)


def test_cancelling_the_loop_settles_every_task() -> None:
    async def during_a_round() -> None:
        h = Harness(("A", "B"))
        h.source.gate = asyncio.Event()
        h.start()
        await h.t.settle()
        h.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await h.task
        await assert_no_leftover_tasks()

    async def during_the_grace() -> None:
        h = Harness()
        h.source.gate = asyncio.Event()
        h.start()
        await h.t.settle()
        h.stop.set()
        await h.t.until_waiting(1)
        h.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await h.task
        assert h.t.waiting == []
        await assert_no_leftover_tasks()

    async def while_waiting_for_a_boundary() -> None:
        h = Harness()
        h.start()
        await h.until_rounds(1)
        await h.t.until_waiting(1)
        h.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await h.task
        assert h.t.waiting == []
        await assert_no_leftover_tasks()

    for scenario in (during_a_round, during_the_grace, while_waiting_for_a_boundary):
        run(scenario)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"symbols": ()}, ValueError),
        ({"symbols": ("A", "A")}, ValueError),
        ({"symbols": ("",)}, ValueError),
        ({"symbols": ["A"]}, TypeError),
        ({"symbols": (1,)}, TypeError),
        ({"max_concurrency": 0}, ValueError),
        ({"max_concurrency": True}, TypeError),
        ({"shutdown_grace_ms": -1}, ValueError),
        ({"shutdown_grace_ms": 1.5}, TypeError),
        ({"limit": 0}, ValueError),
        ({"retry": "x"}, TypeError),
    ],
)
def test_loop_config_validation(kwargs: dict[str, Any], error: type[Exception]) -> None:
    base: dict[str, Any] = {"symbols": ("A",), "timeframe": FIVE, "limit": 100}
    with pytest.raises(error):
        LoopConfig(**untyped({**base, **kwargs}))


def test_loop_config_defaults_and_fetch_config() -> None:
    cfg = config(("A", "B"))
    assert cfg.max_concurrency == 4 and cfg.shutdown_grace_ms == 10_000 and cfg.retry == RetryPolicy()
    assert cfg.fetch_config("B").symbol == "B" and cfg.fetch_config("B").limit == 100


F0 = 1_792_677_000_000  # the forming slot at ManualTime's start


async def honouring(h: Harness, *, wall_offset_ms: int = 0) -> None:
    h.source.honour_limit = True
    if wall_offset_ms:
        await h.t.advance(wall_offset_ms)


def test_first_round_limit_is_computed_from_the_clock_at_each_attempt() -> None:
    async def scenario() -> None:
        h = Harness()
        await honouring(h, wall_offset_ms=TF_MS - 36_000 - 500)  # 500 ms before the next boundary
        h.source.script["A"] = [SourceError("down")]
        h.run_kwargs["first_round_wanted_from"] = {"A": F0 - 990 * TF_MS}
        h.start()
        await h.t.until_waiting(1)
        await h.t.advance(1000)  # the retry wait crosses one boundary
        await h.until_rounds(1)
        assert h.source.limits == [("A", 993), ("A", 994)]
        await h.advance_to_next_boundary()
        await h.until_rounds(2)
        assert h.source.limits[-1] == ("A", 100)
        h.stop.set()
        await h.task

    run(scenario)


def test_a_late_round_start_changes_the_limit_and_it_is_capped() -> None:
    async def scenario(oldest_slots_back: int, expected: int) -> None:
        h = Harness()
        await honouring(h, wall_offset_ms=3 * TF_MS)
        h.run_kwargs["first_round_wanted_from"] = {"A": F0 - oldest_slots_back * TF_MS}
        h.start()
        await h.until_rounds(1)
        assert h.source.limits == [("A", expected)]
        h.stop.set()
        await h.task

    run(lambda: scenario(990, 996))
    run(lambda: scenario(997, 1000))


def test_unmapped_symbols_and_later_rounds_use_the_configured_limit() -> None:
    async def scenario() -> None:
        h = Harness(("A", "B"), max_concurrency=1)
        await honouring(h)
        h.run_kwargs["first_round_wanted_from"] = {"A": F0 - 500 * TF_MS}
        h.start()
        await h.until_rounds(1)
        assert h.source.limits == [("A", 503), ("B", 100)]
        await h.advance_to_next_boundary()
        await h.until_rounds(2)
        assert h.source.limits[2:] == [("A", 100), ("B", 100)]
        h.stop.set()
        await h.task

    run(scenario)


@pytest.mark.parametrize(
    ("wanted", "error"),
    [
        ({"Z": F0 - TF_MS}, ValueError),
        ({"A": F0 - TF_MS + 1}, ValueError),
        ({"A": True}, TypeError),
        ({"A": "5"}, TypeError),
        ({"A": -TF_MS}, ValueError),
    ],
)
def test_first_round_wanted_from_is_validated(wanted: dict[str, Any], error: type[Exception]) -> None:
    async def scenario() -> None:
        h = Harness()
        h.run_kwargs["first_round_wanted_from"] = wanted
        h.start()
        with pytest.raises(error):
            await h.task
        assert h.source.klines_calls == []
        await assert_no_leftover_tasks()

    run(scenario)


def test_after_first_round_runs_once_after_on_round_with_the_first_report() -> None:
    async def scenario() -> None:
        h = Harness()
        seen: list[RoundReport] = []

        async def hook(report: RoundReport) -> None:
            h.events.append("hook")
            seen.append(report)

        h.run_kwargs["after_first_round"] = hook
        h.start()
        await h.until_rounds(1)
        await h.t.settle()
        await h.advance_to_next_boundary()
        await h.until_rounds(2)
        assert h.events == ["on_round", "hook", "on_round"]
        assert seen == [h.rounds[0]]
        h.stop.set()
        await h.task

    run(scenario)


def test_after_first_round_is_skipped_when_stop_is_set_in_round_one() -> None:
    async def scenario() -> None:
        h = Harness()
        called: list[int] = []

        async def hook(report: RoundReport) -> None:
            called.append(1)

        h.run_kwargs["after_first_round"] = hook
        original = h.on_round

        def stopping(report: RoundReport) -> None:
            original(report)
            h.stop.set()

        h.on_round = stopping  # type: ignore[method-assign]
        h.start()
        await h.task
        assert called == []

    run(scenario)


def test_after_first_round_is_skipped_after_a_fatal_error_in_round_one() -> None:
    async def scenario() -> None:
        h = Harness()
        called: list[int] = []

        async def hook(report: RoundReport) -> None:
            called.append(1)

        h.run_kwargs["after_first_round"] = hook
        h.source.errors["A"] = RuntimeError("boom")
        h.start()
        with pytest.raises(FatalLoopError):
            await h.task
        assert called == []

    run(scenario)


def test_after_first_round_exception_is_a_fatal_loop_error_and_keeps_the_uncertain_symbols() -> None:
    async def scenario() -> None:
        h = Harness(sink=ControlledSink(raises=[CandleWriteOutcomeUnknown("unknown")]))
        boom = RuntimeError("hook")

        async def hook(report: RoundReport) -> None:
            raise boom

        h.run_kwargs["after_first_round"] = hook
        h.start()
        await h.t.until_waiting(1)
        await h.t.advance(1000)  # attempt 2 collects; the first write's outcome stays unknown
        with pytest.raises(FatalLoopError) as caught:
            await h.task
        assert caught.value.uncertain_symbols == ("A",) and caught.value.__cause__ is boom
        assert h.stop.is_set()
        await assert_no_leftover_tasks()

    run(scenario)


def test_observe_forming_slot_gets_the_newest_returned_row_and_the_wall_time_in_every_round() -> None:
    async def scenario() -> None:
        h = Harness()
        await honouring(h)
        h.source.forming_ahead_slots = 3
        seen: list[tuple[str, int, int]] = []
        h.run_kwargs["observe_forming_slot"] = lambda s, o, w: seen.append((s, o, w))
        h.start()
        await h.until_rounds(1)
        assert seen == [("A", F0 + 3 * TF_MS, F0 + 36_000)]
        await h.advance_to_next_boundary()
        await h.until_rounds(2)
        assert len(seen) == 2 and seen[1][0] == "A"  # S17: observed in every round, not only round one
        h.stop.set()
        await h.task

    run(scenario)


def test_observe_forming_slot_is_silent_without_rows_or_after_a_failed_fetch() -> None:
    async def scenario() -> None:
        h = Harness()
        await honouring(h)
        seen: list[tuple[str, int, int]] = []
        h.run_kwargs["observe_forming_slot"] = lambda s, o, w: seen.append((s, o, w))
        h.source.script["A"] = [SourceError("down")]
        h.start()
        await h.t.until_waiting(1)
        assert seen == []
        await h.t.advance(1000)
        await h.until_rounds(1)
        assert len(seen) == 1  # only the successful attempt

        h2 = Harness()
        h2.source.honour_limit = True
        h2.source.limited_rows = lambda limit, tf_ms: []  # type: ignore[method-assign]
        seen2: list[tuple[str, int, int]] = []
        h2.run_kwargs["observe_forming_slot"] = lambda s, o, w: seen2.append((s, o, w))
        h2.start()
        await h2.t.until_waiting(1)
        assert seen2 == []
        for task in (h.task, h2.task):
            task.cancel()
        await asyncio.gather(h.task, h2.task, return_exceptions=True)

    run(scenario)


def test_observe_exceptions_are_not_swallowed() -> None:
    async def scenario() -> None:
        h = Harness()
        boom = RuntimeError("observer")

        def observer(symbol: str, open_ms: int, wall_ms: int) -> None:
            raise boom

        h.run_kwargs["observe_forming_slot"] = observer
        h.start()
        with pytest.raises(FatalLoopError) as caught:
            await h.task
        assert caught.value.__cause__ is boom and caught.value.uncertain_symbols == ()
        assert h.stop.is_set()
        await assert_no_leftover_tasks()

    run(scenario)


def test_cancelling_the_hook_cancels_the_loop_and_leaves_no_task() -> None:
    async def scenario() -> None:
        h = Harness()
        parked = asyncio.Event()

        async def hook(report: RoundReport) -> None:
            await parked.wait()

        h.run_kwargs["after_first_round"] = hook
        h.start()
        await h.until_rounds(1)
        await h.t.settle()
        h.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await h.task
        await assert_no_leftover_tasks()

    run(scenario)


def test_600_symbol_first_round_keeps_the_spacing_and_the_concurrency_bound() -> None:
    async def scenario() -> None:
        symbols = tuple(f"S{i}" for i in range(600))
        h = Harness(symbols, max_concurrency=4)
        h.source.honour_limit = True
        starts: list[int] = []

        class Recording:
            def __init__(self, inner: ScriptedSource) -> None:
                self.inner = inner

            async def fetch_server_time(self) -> ServerTimeObservation:
                starts.append(h.t.monotonic_ns())
                return await self.inner.fetch_server_time()

            async def fetch_klines(
                self,
                symbol: str,
                timeframe: Timeframe,
                limit: int,
                *,
                start_time_ms: int | None = None,
                end_time_ms: int | None = None,
            ) -> Any:
                starts.append(h.t.monotonic_ns())
                return await self.inner.fetch_klines(symbol, timeframe, limit)

            async def close(self) -> None:
                await self.inner.close()

        limited = RateLimitedSource(Recording(h.source), RateLimiter(100, h.t, h.t.sleep))
        in_flight = 0
        peak = 0

        class Counting:
            """Placed before the limiter: counts the calls of attempts that are in flight, waiting included."""

            async def fetch_server_time(self) -> ServerTimeObservation:
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                try:
                    return await limited.fetch_server_time()
                finally:
                    in_flight -= 1

            async def fetch_klines(
                self,
                symbol: str,
                timeframe: Timeframe,
                limit: int,
                *,
                start_time_ms: int | None = None,
                end_time_ms: int | None = None,
            ) -> Any:
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                try:
                    return await limited.fetch_klines(symbol, timeframe, limit)
                finally:
                    in_flight -= 1

            async def close(self) -> None:
                await limited.close()

        h.run_kwargs["first_round_wanted_from"] = {f"S{i}": F0 - 990 * TF_MS for i in range(5)}
        start_ns = h.t.monotonic_ns()
        h.task = asyncio.create_task(
            run_collection_loop(
                Counting(),
                h.sink,
                config(symbols, max_concurrency=4),
                h.t,
                h.t.sleep,
                h.stop,
                h.on_round,
                **h.run_kwargs,
            )
        )
        for _ in range(1_500):
            if h.rounds:
                break
            await h.t.advance(100)
        assert h.rounds, "round one did not finish"
        gaps = [b - a for a, b in itertools.pairwise(starts)]
        assert gaps and min(gaps) >= 100 * NS_PER_MS
        assert peak == 4  # the semaphore, not the limiter, bounds the attempts in flight
        assert sum(1 for _, limit in h.source.limits if limit > 100) == 5
        grants = 600 + h.source.time_calls  # one klines call per symbol plus the shared server-time refreshes
        assert 1 <= h.source.time_calls <= 16  # a refresh about every 5 s of a round of about 60 s
        assert h.t.monotonic_ns() - start_ns >= (grants - 1) * 100 * NS_PER_MS
        h.stop.set()
        await h.t.advance(0)
        h.task.cancel()
        await asyncio.gather(h.task, return_exceptions=True)

    run(scenario)


# --- one server-time observation per refresh age, not per symbol (plan 2026-11-01-shared-server-time) ---------


def test_a_round_of_many_symbols_observes_server_time_once() -> None:
    async def scenario() -> None:
        h = Harness(tuple(f"S{i}" for i in range(20)), max_concurrency=4)
        h.start()
        await h.t.settle()
        assert len(h.rounds) == 1
        assert all(r.outcome is SymbolOutcome.COLLECTED for r in h.rounds[0].results)
        assert h.source.time_calls == 1
        h.stop.set()
        await h.task

    run(scenario)


def test_a_later_round_observes_again() -> None:
    async def scenario() -> None:
        h = Harness(("A", "B"))
        h.start()
        await h.until_rounds(1)
        await h.advance_to_next_boundary()
        await h.until_rounds(2)
        assert h.source.time_calls == 2  # 300 s later the observation is past the refresh age
        h.stop.set()
        await h.task

    run(scenario)


def test_a_rejected_observation_is_not_reused_by_the_retry() -> None:
    async def scenario() -> None:
        h = Harness(("A",))
        h.source.time_rtts = [2_500]  # above max_rtt_ms (2000): the first observation is rejected by the gate
        h.start()
        await h.t.settle()
        await h.t.advance(2_500)
        await h.t.settle()
        await h.t.advance(1_000)  # the retry wait
        await h.until_rounds(1)
        result = h.rounds[0].results[0]
        assert result.outcome is SymbolOutcome.COLLECTED and result.attempts == 2
        assert h.source.time_calls == 2  # the retry refreshed instead of reusing the rejected observation
        h.stop.set()
        await h.task

    run(scenario)


def test_a_failing_time_endpoint_fails_closed_once_the_observation_is_too_old() -> None:
    async def scenario() -> None:
        h = Harness(("A",), retry=RetryPolicy(max_attempts=1))
        h.start()
        await h.until_rounds(1)
        assert h.rounds[0].results[0].outcome is SymbolOutcome.COLLECTED
        written = len(h.sink.calls)
        h.source.time_error = SourceError("down")
        await h.advance_to_next_boundary()
        await h.until_rounds(2)
        result = h.rounds[1].results[0]
        assert result.outcome is SymbolOutcome.GAVE_UP
        assert result.last_problem == "time untrusted: AGE_TOO_LARGE"  # the old observation, judged by its age
        assert len(h.sink.calls) == written  # nothing was stored on the strength of it
        h.stop.set()
        await h.task

    run(scenario)


def test_a_symbol_served_late_in_a_long_round_gets_a_fresh_observation_not_an_old_one() -> None:
    async def scenario() -> None:
        h = Harness(("A", "B", "C"), max_concurrency=1)
        h.source.delays = [4_000, 4_000, 4_000]  # each klines call takes 4 s: B starts 4 s after A, C 8 s after A
        h.start()
        await h.t.settle()
        for _ in range(3):
            await h.t.advance(4_000)
            await h.t.settle()
        await h.until_rounds(1)
        assert all(r.outcome is SymbolOutcome.COLLECTED for r in h.rounds[0].results)
        assert h.source.time_calls == 2  # A and B share one (4 s old); C starts at 8 s, past the 5 s refresh age
        h.stop.set()
        await h.task

    run(scenario)
