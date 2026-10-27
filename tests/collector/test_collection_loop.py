import asyncio
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
from quasar_market_data.domain.schedule import next_wake_ms
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
                self.rounds.append,
            )
        )

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
