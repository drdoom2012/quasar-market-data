import asyncio
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path

import pytest

from quasar_market_data.collector.funding_rate_limiter import WINDOW_MS, RequestClass
from quasar_market_data.collector.funding_rate_reader import read_span
from quasar_market_data.collector.funding_rate_writer import SpanMerge
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.funding_rate_span import Interval
from tests.collector.funding_rate_task_fakes import (
    HOUR,
    VENUE,
    Harness,
    InMemorySink,
    make_rig,
    seed_state,
    start_task,
    symbols_of,
)
from tests.collector.manual_time import NS_PER_MS, ManualTime

S = 1000
BASE_WALL = 1_792_677_036_000  # ManualTime's default wall start
B0 = BASE_WALL // HOUR * HOUR  # the whole hour the start falls in
MONO_START = 18_053_000_000_000


def run(scenario: Callable[[Path], Coroutine[None, None, None]], timeout: float = 120) -> None:
    async def guarded() -> None:
        with tempfile.TemporaryDirectory() as directory:
            await asyncio.wait_for(scenario(Path(directory)), timeout)

    asyncio.run(guarded())


def t_at(wall_ms: int) -> ManualTime:
    return ManualTime(wall_start_ms=wall_ms)


def round_lines(h: Harness) -> list[dict[str, str]]:
    return [dict(part.split("=", 1) for part in line.split()[1:]) for line in h.lines_starting("funding-round")]


def test_the_first_round_seeds_every_series_in_symbol_order_s_before_r() -> None:
    async def scenario(tmp_path: Path) -> None:
        symbols = ("ETH/USDT:USDT", "BTC/USDT:USDT", "ADA/USDT:USDT")
        h = await start_task(tmp_path, symbols=symbols, backfill_days=1)
        await h.run_for(60 * S)
        forward = [s.symbol for s in h.rig.exchange.served if s.end_ms - s.start_ms >= 86_400_000 - 1]
        assert forward[:3] == sorted(symbols)  # seeds in symbol order
        for symbol in symbols:
            assert isinstance((await read_span(h.rig.db, VENUE, symbol))[0], Interval)
        (first,) = round_lines(h)
        assert first["asked"] == "3" and first["failed"] == "0" and first["skipped"] == "0"
        await h.finish()

    run(scenario)


def test_the_next_round_asks_every_series_once_in_ascending_verified_at_then_symbol_order() -> None:
    async def scenario(tmp_path: Path) -> None:
        symbols = symbols_of(6)
        h = await start_task(tmp_path, symbols=symbols, backfill_days=1, request_ms=1_000)
        await h.run_for(120 * S)  # round one: seeds finish 1 s apart, in symbol order
        before = len(h.rig.exchange.served)
        await h.run_for(B0 + HOUR + 60_000 - h.t.wall_ms() + 60 * S, step_ms=5 * S)
        second = [s.symbol for s in h.rig.exchange.served[before:]]
        assert second[: len(symbols)] == sorted(symbols)
        assert len(h.rig.exchange.time_calls) == 2  # one observation per round, nothing else asked the time
        assert [r["asked"] for r in round_lines(h)] == ["6", "6"]
        await h.finish()

    run(scenario)


def test_a_round_is_cut_at_its_deadline_nothing_is_marked_failed_and_skipped_series_are_first_next_time() -> None:
    async def scenario(tmp_path: Path) -> None:
        symbols = symbols_of(400)  # b = 50 serves 274 per round at most: about 125 are not started
        sink = InMemorySink()
        h = await start_task(
            tmp_path, symbols=symbols, budget_per_5min=50, backfill_days=1, sink=sink, t=t_at(B0 + 3_000)
        )
        await h.run_for(B0 + HOUR - h.t.wall_ms() + 30 * S, step_ms=30 * S)  # before the next wake
        first = round_lines(h)[0]
        skipped = int(first["skipped"])
        assert 100 <= skipped <= 140 and first["failed"] == "0" and first["suspended"] == "0"
        missing = [s for s in symbols if s not in sink.spans]
        assert len(missing) == skipped
        await h.run_for(B0 + 2 * HOUR + 200 * S - h.t.wall_ms(), step_ms=30 * S)
        second_round = [s.symbol for s in h.rig.exchange.served if s.wall_ms >= B0 + HOUR + 60_000]
        assert set(second_round[: len(missing)]) == set(missing)  # the unseeded come first (rule 1)
        assert all(r["failed"] == "0" for r in round_lines(h))
        await h.finish()

    run(scenario, timeout=900)


def test_untrusted_time_at_the_wake_skips_the_round_and_the_next_round_runs() -> None:
    async def scenario(tmp_path: Path) -> None:
        h = await start_task(tmp_path, t=t_at(B0 + 3_000), backfill_days=1)
        h.rig.exchange.time_error = SourceError("time request failed")
        await h.run_for(200 * S, step_ms=20 * S)  # the wake of the first round passes with a failing time request
        assert h.lines_starting("funding-warning round-skipped") and not round_lines(h)
        h.rig.exchange.time_error = None
        await h.run_for(B0 + HOUR + 200 * S - h.t.wall_ms(), step_ms=20 * S)
        assert [r["boundary"] for r in round_lines(h)] == [str(B0 + HOUR)] and round_lines(h)[0]["asked"] == "2"
        await h.finish()

    run(scenario)


def test_a_start_inside_the_last_fifteen_seconds_waits_for_the_next_round() -> None:
    async def scenario(tmp_path: Path) -> None:
        h = await start_task(tmp_path, t=t_at(B0 + HOUR - 10 * S), backfill_days=1)
        await h.run_for(30 * S, step_ms=5 * S)
        assert not h.rig.exchange.served and not round_lines(h)
        await h.run_for(B0 + HOUR + 80 * S - h.t.wall_ms(), step_ms=5 * S)
        assert [r["boundary"] for r in round_lines(h)] == [str(B0 + HOUR)]
        await h.finish()

    run(scenario)


def test_a_start_inside_a_rounds_window_runs_that_round_late() -> None:
    async def scenario(tmp_path: Path) -> None:
        h = await start_task(tmp_path, backfill_days=1)  # about 50 minutes into the hour
        await h.run_for(30 * S)
        (line,) = round_lines(h)
        assert line["boundary"] == str(B0) and int(line["late_ms"]) > 40 * 60_000
        await h.finish()

    run(scenario)


@pytest.mark.parametrize(
    ("offset", "behind", "expected"),
    [(500_000, True, 1), (500_000, False, 0), (60_000, True, 0)],
    ids=["behind_and_far_wake", "not_behind", "wake_within_one_window"],
)
def test_the_start_up_observation_is_taken_only_with_a_behind_series_and_a_far_wake(
    offset: int, behind: bool, expected: int
) -> None:
    async def scenario(tmp_path: Path) -> None:
        t = t_at(B0 + 3_000)  # 3 s after a whole hour
        rig = await make_rig(tmp_path, t=t)
        if behind:  # a stored span that ends three hours ago
            end = B0 - 3 * HOUR
            merge = SpanMerge(VENUE, "BTC/USDT:USDT", Interval(end - HOUR, end), t.wall_ms())
            await rig.writer.write([], ingested_at_ms=t.wall_ms(), merge=merge)
        h = await start_task(tmp_path, rig=rig, backfill_days=1, offset_ms=offset)
        await h.run_for(5 * S)
        assert len(rig.exchange.time_calls) == expected
        await h.finish()

    run(scenario)


@pytest.mark.parametrize("extra", [0, 1])
def test_determinism_the_same_stored_data_clock_and_grants_give_the_same_request_order(extra: int) -> None:
    async def one(tmp_path: Path) -> list[tuple[str, int, int, int]]:
        h = await start_task(
            tmp_path,
            symbols=symbols_of(12 + extra),
            budget_per_5min=50,
            backfill_days=1,
            request_ms=300,
            sink=InMemorySink(),  # keeps the writer thread's real timing out of a comparison of two runs
        )
        await h.run_for(2 * HOUR + 300 * S, step_ms=10 * S)
        served = [
            (s.symbol, s.start_ms - B0, s.end_ms - B0, (s.at_ns - MONO_START) // NS_PER_MS)
            for s in h.rig.exchange.served
        ]
        await h.finish()
        return served

    async def scenario(tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        first = await one(tmp_path / "a")
        second = await one(tmp_path / "b")
        assert first and first == second

    run(scenario, timeout=300)


def test_with_history_saturating_the_share_every_r_request_keeps_its_planned_start_and_the_caps_hold() -> None:
    async def scenario(tmp_path: Path) -> None:
        symbols = symbols_of(120)  # b = 50: m = 25; 120 series need five groups
        h = await start_task(
            tmp_path,
            symbols=symbols,
            budget_per_5min=50,
            backfill_days=1,
            recheck_ms=3_600_000,  # the seed is one hour wide: 23 hours of history are pending
            period_ms=30_000,  # 30 s settlements: that history is 2 760 rows, three pages per series
            sink=InMemorySink(),
        )
        await h.run_for(B0 + HOUR - h.t.wall_ms() + 20 * S, step_ms=20 * S)  # round one
        await h.run_for(
            B0 + 2 * HOUR + 60_000 - h.t.wall_ms() + 1_800 * S, step_ms=20 * S
        )  # round two, history saturating
        grants = h.rig.limiter.grants
        r = [ns for cls, ns in grants if cls is RequestClass.R]
        ch = [ns for cls, ns in grants if cls is not RequestClass.R]
        window = WINDOW_MS * NS_PER_MS

        def worst(starts: list[int]) -> int:
            ordered, low, best = sorted(starts), 0, 0
            for high, instant in enumerate(ordered):
                while ordered[low] <= instant - window:
                    low += 1
                best = max(best, high - low + 1)
            return best

        assert worst(r) <= 25 and worst(ch) <= 25 and worst(r + ch) <= 50
        assert len(ch) > 25 * 10  # history really saturated its share
        wake = MONO_START + (B0 + 2 * HOUR + 60_000 - BASE_WALL) * NS_PER_MS  # round two's wake on the monotonic clock
        offsets = [(ns - wake) // NS_PER_MS for ns in sorted(ns for ns in r if ns >= wake)]
        assert len(offsets) == 121  # the observation (index 0) and the 120 series
        for index, offset in enumerate(offsets):
            assert offset // WINDOW_MS == index // 25, (index, offset)
        assert h.rig.exchange.max_active_by_symbol == 1  # one series never has two requests in flight
        await h.finish()

    run(scenario, timeout=900)


def test_a_429_pause_at_the_wake_delays_the_round_until_the_pause_ends_instead_of_skipping_it() -> None:
    async def scenario(tmp_path: Path) -> None:
        h = await start_task(tmp_path, t=t_at(B0 + 3_000), backfill_days=1)
        h.rig.pause.record_rate_limited(0)  # a 429 shortly before the wake: paused for 300 s, the wake is at 60 s
        await h.run_for(500 * S, step_ms=10 * S)
        assert not h.lines_starting("funding-warning round-skipped")
        (line,) = round_lines(h)
        assert line["boundary"] == str(B0) and line["asked"] == "2"
        await h.finish()

    run(scenario)


def test_a_pause_that_outlasts_the_deadline_skips_that_round_only() -> None:
    async def scenario(tmp_path: Path) -> None:
        h = await start_task(tmp_path, t=t_at(B0 + 3_000), backfill_days=1)
        h.rig.pause.record_rate_limited(3_600_000)  # longer than the round
        await h.run_for(200 * S, step_ms=10 * S)
        assert h.lines_starting("funding-warning round-skipped") and not round_lines(h)
        await h.finish()

    run(scenario)


def test_a_hanging_round_observation_does_not_stop_history_requests() -> None:
    async def scenario(tmp_path: Path) -> None:
        symbols = symbols_of(40)
        rig = await make_rig(tmp_path, symbols=symbols, t=t_at(B0 + 3_000), sink=InMemorySink(), period_ms=30_000)
        for symbol in symbols:
            seed_state(rig, symbol, Interval(B0 - 3 * HOUR, B0 - HOUR))  # history (one day deep) is pending: 3 pages
        rig.exchange.hang_time = True
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(60 * S, step_ms=5 * S)  # up to the wake: the observation now hangs
        before = len(rig.exchange.served)
        await h.run_for(700 * S, step_ms=10 * S)
        assert len(rig.exchange.served) > before + 10  # history went on while the time request was pending
        await h.finish()

    run(scenario)
