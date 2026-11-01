"""The backfill worker inside the collection loop (S16 Task 6): priority, stalls and uncertainty, on simulated time."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quasar_market_data.collector.candle_writer import CandleWriteOutcomeUnknown
from quasar_market_data.collector.collection_loop import LoopConfig, RoundReport, run_collection_loop
from quasar_market_data.collector.rate_limiter import RateLimitedSource, RateLimiter
from quasar_market_data.domain.server_time import ServerTimeObservation
from quasar_market_data.domain.timeframe import Timeframe
from tests.collector.backfill_fakes import FIVE, Env
from tests.collector.fetch_fakes import ControlledSink, kline_row
from tests.collector.loop_fakes import ScriptedSource
from tests.collector.manual_time import ManualTime

LATENESS_BOUND_MS = 0  # the fake sleep has no jitter


class HybridSource:
    """Rounds (no range) are served by the scripted source; backfill pages (with a range) take ``page_delay_ms``."""

    def __init__(self, t: ManualTime, *, page_delay_ms: int = 0, hang: bool = False) -> None:
        self.t = t
        self.rounds = ScriptedSource(t)
        self.page_delay_ms = page_delay_ms
        self.hang = hang
        self.page_requests = 0
        self.round_requests = 0
        self.on_round: Callable[[int], None] | None = None
        self.page_error: Exception | None = None

    async def fetch_server_time(self) -> ServerTimeObservation:
        return await self.rounds.fetch_server_time()

    async def fetch_klines(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> Any:
        if start_time_ms is None or end_time_ms is None:
            self.round_requests += 1
            if self.on_round is not None:
                self.on_round(self.round_requests)
            return await self.rounds.fetch_klines(symbol, timeframe, limit)
        self.page_requests += 1
        if self.page_error is not None:
            raise self.page_error
        if self.hang:
            await asyncio.Event().wait()
        if self.page_delay_ms:
            await self.t.sleep(self.page_delay_ms)
        tf = timeframe.milliseconds
        return [kline_row(o, tf) for o in range(start_time_ms, end_time_ms + 1, tf)][:limit]

    async def close(self) -> None:
        pass


async def drive(t: ManualTime, until: Callable[[], bool], steps: int = 200_000) -> None:
    """Advance simulated time to the next waiter until ``until()``."""
    for _ in range(steps):
        await t.settle()
        if until():
            return
        assert t.waiting, "nothing is waiting: the simulation is stuck"
        await t.advance_ns(max(0, t.waiting[0] - t.monotonic_ns()))
    raise AssertionError("simulation did not reach the goal")


async def drive_forever_step(t: ManualTime) -> None:
    """Advance simulated time whenever something waits, until cancelled (used where no test body can drive)."""
    while True:
        await t.settle()
        if t.waiting:
            await t.advance_ns(max(0, t.waiting[0] - t.monotonic_ns()))
        else:
            await asyncio.sleep(0)


def wire(env: Env, sink: ControlledSink, source: HybridSource, rounds: list[RoundReport]) -> "asyncio.Task[Any]":
    limited = RateLimitedSource(source, RateLimiter(100, env.t, env.t.sleep))
    config = LoopConfig(symbols=env.symbols, timeframe=FIVE, limit=100)
    bf = env.backfiller()
    return asyncio.ensure_future(
        run_collection_loop(limited, sink, config, env.t, env.t.sleep, env.stop, rounds.append, idle_work=bf.run_idle)
    )


def go(tmp_path: Path, symbols: tuple[str, ...], body: Callable[[Env], Any]) -> None:
    async def main() -> None:
        env = await Env(tmp_path, symbols).open()
        try:
            for symbol in symbols:
                env.store([env.slot(0), env.slot(12)], symbol=symbol)  # a hole of eleven slots in between
            await body(env)
        finally:
            await env.close()

    asyncio.run(main())


def assert_on_time(rounds: list[RoundReport]) -> None:
    for report in rounds:
        assert report.idle_overrun_ms == 0
        assert report.skipped_rounds == 0
        assert report.lateness_ms <= LATENESS_BOUND_MS


def test_a_slow_backfill_does_not_skip_a_cycle(tmp_path: Path) -> None:
    symbols = tuple(f"S{i}" for i in range(60))

    async def body(env: Env) -> None:
        source = HybridSource(env.t, page_delay_ms=4_800)
        rounds: list[RoundReport] = []
        task = wire(env, ControlledSink(), source, rounds)
        await drive(env.t, lambda: len(rounds) >= 8)
        env.stop.set()
        await drive(env.t, task.done)
        await task
        assert source.page_requests > 8 * 30  # backfill really used the windows
        assert_on_time(rounds)

    go(tmp_path, symbols, body)


def test_a_stalled_source_does_not_delay_a_round(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        source = HybridSource(env.t, hang=True)  # every backfill request hangs forever
        rounds: list[RoundReport] = []
        task = wire(env, ControlledSink(), source, rounds)
        await drive(env.t, lambda: len(rounds) >= 5)
        env.stop.set()
        await drive(env.t, task.done)
        await task
        assert source.page_requests >= 1
        statuses = [s.status.value for s in env.slices]
        assert "backoff" in statuses and set(statuses) <= {
            "backoff",
            "progress",
        }  # stalled pages timed out into back-offs
        assert_on_time(rounds)

    go(tmp_path, ("A", "B", "C"), body)


def test_unknown_write_outcome_during_backfill_marks_the_symbol_uncertain(tmp_path: Path) -> None:
    async def body(env: Env) -> None:
        source = HybridSource(env.t)
        rounds: list[RoundReport] = []
        sink = ControlledSink(raises=[None, CandleWriteOutcomeUnknown("unknown")])  # round write ok, page write unknown
        task = wire(env, sink, source, rounds)
        await drive(env.t, lambda: len(env.slices) >= 1)
        env.stop.set()
        await drive(env.t, task.done)
        exit_ = await task
        assert exit_.uncertain_symbols == ("A",)
        assert env.slices[0].status.value == "backoff" and env.slices[0].attribution.value == "write_unknown"

    go(tmp_path, ("A",), body)
