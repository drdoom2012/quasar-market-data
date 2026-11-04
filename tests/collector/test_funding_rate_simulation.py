"""Capacity simulations of the funding rate task (ADR-0004 P-10, evidence E-14, P-15 Capacity and Latency rows).

The REAL task, limiter and source run on ``ManualTime`` against a fake exchange; writes are instant (an in-memory
sink), as in the model of E-14. Failures are injected like E-14: a chosen request fails once and its retry succeeds,
so no series is suspended. Cases for ``b = 50`` are in ``make check``; the larger budgets and the 200-round run are
marked ``simulation`` (``make simulate``).
"""

import asyncio
import itertools
import tempfile
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path

import ccxt
import pytest

from quasar_market_data.collector.funding_rate_limiter import WINDOW_MS, RequestClass
from quasar_market_data.domain.funding_rate_capacity import full_groups_per_round, groups_k, sustained_capacity
from quasar_market_data.domain.funding_rate_span import Interval
from tests.collector.funding_rate_task_fakes import (
    HOUR,
    Harness,
    InMemorySink,
    make_rig,
    seed_state,
    start_task,
    symbols_of,
)
from tests.collector.manual_time import NS_PER_MS, ManualTime

B0 = 1_792_677_036_000 // HOUR * HOUR
MONO_START = 18_053_000_000_000
WAKE_OFFSET = 60_000
DEADLINE_MARGIN = 15_000
S = 1000

BUDGETS = [50, pytest.param(150, marks=pytest.mark.simulation), pytest.param(250, marks=pytest.mark.simulation)]


def run(scenario: Callable[[Path], Coroutine[None, None, None]], timeout: float) -> None:
    async def guarded() -> None:
        with tempfile.TemporaryDirectory() as directory:
            await asyncio.wait_for(scenario(Path(directory)), timeout)

    asyncio.run(guarded())


def wake_ns(round_index: int) -> int:
    """Monotonic instant of round ``round_index``'s wake (the task starts 3 s after the whole hour)."""
    return MONO_START + (round_index * HOUR + WAKE_OFFSET - 3_000) * NS_PER_MS


@dataclass
class Outcome:
    offsets: list[list[int]]  # per round: ms after the wake of every R start (index 0 is the observation)
    skipped: list[int]
    asked: list[int]
    failed: list[int]
    lines: list[str]


async def simulate(
    tmp_path: Path,
    *,
    series: int,
    b: int,
    rounds: int,
    request_ms: int = 0,
    failures: Callable[[int], bool] | None = None,
    slow: Callable[[int], int] | None = None,
    history: bool = False,
    offset_ms: int = WAKE_OFFSET,
) -> Outcome:
    symbols = symbols_of(series)
    t = ManualTime(wall_start_ms=B0 + 3_000)
    sink = InMemorySink()
    rig = await make_rig(tmp_path, symbols=symbols, budget_per_5min=b, sink=sink, t=t, request_ms=request_ms)
    through = B0 - 3_600_000
    for symbol in symbols:  # every series already seeded and caught up: rounds only (and history if asked for)
        width = 3 * 3_600_000 if history else 2 * 86_400_000
        seed_state(rig, symbol, Interval(through - width, through))
    if failures is not None or slow is not None:
        counter = {"n": 0}
        original = rig.exchange.fapiPublicGetFundingRate

        async def hooked(params: dict[str, object]) -> object:
            counter["n"] += 1
            n = counter["n"]
            if slow is not None and slow(n):
                rig.exchange.request_ms = slow(n)
            else:
                rig.exchange.request_ms = request_ms
            if failures is not None and failures(n):
                raise ccxt.NetworkError("injected")
            return await original(params)

        rig.exchange.fapiPublicGetFundingRate = hooked  # type: ignore[method-assign]
    h = await start_task(
        tmp_path,
        rig=rig,
        budget_per_5min=b,
        backfill_days=1 if not history else 3650,
        offset_ms=offset_ms,
        recheck_ms=86_400_000,
    )
    await h.run_exact(rounds * HOUR + 120 * S)
    return outcome_of(h, rounds)


def outcome_of(h: Harness, rounds: int) -> Outcome:
    r_grants = sorted(ns for cls, ns in h.rig.limiter.grants if cls is RequestClass.R)
    offsets: list[list[int]] = []
    for index in range(rounds):
        wake = wake_ns(index)
        end = wake + (HOUR - DEADLINE_MARGIN - WAKE_OFFSET) * NS_PER_MS
        offsets.append([(ns - wake) // NS_PER_MS for ns in r_grants if wake <= ns < end])
    lines = h.lines_starting("funding-round")
    fields = [dict(part.split("=", 1) for part in line.split()[1:]) for line in lines]
    return Outcome(
        offsets,
        [int(f["skipped"]) for f in fields],
        [int(f["asked"]) for f in fields],
        [int(f["failed"]) for f in fields],
        h.lines,
    )


@pytest.mark.parametrize("b", BUDGETS)
def test_48_rounds_at_s_sustained_skip_nothing_do_not_drift_and_end_30_s_before_the_deadline(b: int) -> None:
    async def scenario(tmp_path: Path) -> None:
        series = sustained_capacity(b, WAKE_OFFSET)
        result = await simulate(tmp_path, series=series, b=b, rounds=48)
        assert result.skipped == [0] * 48 and result.asked == [series] * 48
        assert all(len(o) == series + 1 for o in result.offsets)  # the observation plus every series, each round
        assert all(o == result.offsets[0] for o in result.offsets), "the start offsets drift between rounds"
        deadline_offset = HOUR - DEADLINE_MARGIN - WAKE_OFFSET
        assert all(o[-1] <= deadline_offset - 30_000 for o in result.offsets)
        m = b // 2
        assert (
            result.offsets[0][-1] // WINDOW_MS == (series) // m
        )  # request j starts floor(j / m) windows after the wake

    run(scenario, timeout=3_000)


@pytest.mark.parametrize("b", BUDGETS)
def test_the_isolated_round_capacity_is_not_sustainable(b: int) -> None:
    async def scenario(tmp_path: Path) -> None:
        m = b // 2
        series = (groups_k(WAKE_OFFSET) + 1) * m - 1  # 299, 899, 1 499: the isolated-round capacity of P-10
        assert series > sustained_capacity(b, WAKE_OFFSET) and full_groups_per_round(WAKE_OFFSET) == 11
        result = await simulate(tmp_path, series=series, b=b, rounds=48)
        firsts = [o[0] for o in result.offsets]
        assert firsts[:4] == [0, 60_000, 120_000, 180_000]  # the first start slips by a minute per round
        assert result.skipped[3] == m and result.skipped[:3] == [0, 0, 0]  # round 3 loses a whole group
        assert sum(1 for count in result.skipped if count) == 12 and set(result.skipped) == {0, m}  # 12 of 48 rounds

    run(scenario, timeout=3_000)


@pytest.mark.parametrize(
    ("fraction", "expect_skips"),
    [(1.0, False), (0.9, False)],
    ids=["s_sustained_5_percent", "ninety_percent_10_percent"],
)
def test_a_failure_that_is_retried_once_costs_no_skip_inside_the_reserve(fraction: float, expect_skips: bool) -> None:
    async def scenario(tmp_path: Path) -> None:
        b = 50
        series = int(sustained_capacity(b, WAKE_OFFSET) * fraction)
        every = 20 if fraction == 1.0 else 10  # 5 % at S_sustained, 10 % at 0.9 x S_sustained
        result = await simulate(tmp_path, series=series, b=b, rounds=6, failures=lambda n: n % every == 0)
        assert (sum(result.skipped) > 0) is expect_skips and all(f >= 0 for f in result.failed)
        assert not [line for line in result.lines if line.startswith("funding-suspended")]

    run(scenario, timeout=3_000)


def test_more_failures_than_the_reserve_cause_skips_but_no_series_is_skipped_for_ever() -> None:
    async def scenario(tmp_path: Path) -> None:
        b = 50
        series = sustained_capacity(b, WAKE_OFFSET)
        result = await simulate(
            tmp_path, series=series, b=b, rounds=12, failures=lambda n: n % 10 == 0
        )  # 10 % at S_sustained
        assert sum(result.skipped) > 0
        asked = result.asked
        assert min(asked) >= series - max(result.skipped)  # the skip is the safety valve, not a collapse
        assert not [line for line in result.lines if line.startswith("funding-suspended")]

    run(scenario, timeout=3_000)


def test_the_series_asked_last_are_asked_first_next_time_so_none_is_left_unserved() -> None:
    async def scenario(tmp_path: Path) -> None:
        b = 50
        series = sustained_capacity(b, WAKE_OFFSET) + 60  # overload: some series are skipped every round
        symbols = symbols_of(series)
        t = ManualTime(wall_start_ms=B0 + 3_000)
        sink = InMemorySink()
        rig = await make_rig(tmp_path, symbols=symbols, budget_per_5min=b, sink=sink, t=t)
        for symbol in symbols:
            seed_state(rig, symbol, Interval(B0 - 3 * HOUR - 86_400_000, B0 - 3_600_000))
        h = await start_task(tmp_path, rig=rig, budget_per_5min=b, backfill_days=1)
        await h.run_exact(8 * HOUR + 120 * S)
        asked: dict[str, int] = {}
        for served in rig.exchange.served:
            asked[served.symbol] = asked.get(served.symbol, 0) + 1
        assert (
            len(asked) == series and min(asked.values()) >= 1
        )  # rotation: every series is served although every round is cut

    run(scenario, timeout=3_000)


@pytest.mark.parametrize(
    ("series", "latest_ms"),
    [(10, 0), (100, 305_000), (300, 1_220_000), (659, 2_440_000)],
    ids=["10", "100", "300", "659"],
)
def test_the_last_series_starts_floor_j_over_m_windows_after_the_wake(series: int, latest_ms: int) -> None:
    async def scenario(tmp_path: Path) -> None:
        result = await simulate(tmp_path, series=series, b=150, rounds=3)
        assert all(o[-1] // 305_000 * 305_000 == latest_ms for o in result.offsets)

    run(scenario, timeout=3_000)


def test_with_400_ms_requests_each_start_is_at_most_15_s_later_than_planned() -> None:
    async def scenario(tmp_path: Path) -> None:
        series = 300
        result = await simulate(tmp_path, series=series, b=150, rounds=3, request_ms=400)
        for offsets in result.offsets[1:]:
            for index, offset in enumerate(offsets):
                assert 0 <= offset - (index // 75) * 305_000 <= 15_000, (index, offset)

    run(scenario, timeout=3_000)


@pytest.mark.simulation
def test_200_rounds_at_b_150_with_a_history_backlog_saturating_the_share_keep_the_round_starts() -> None:
    async def scenario(tmp_path: Path) -> None:
        series = 500
        result = await simulate(tmp_path, series=series, b=150, rounds=200, request_ms=400, history=True)
        assert result.skipped == [0] * 200
        for offsets in result.offsets[1:]:
            for index, offset in enumerate(offsets):
                assert 0 <= offset - (index // 75) * 305_000 <= 15_000, (index, offset)

    run(scenario, timeout=20_000)


@pytest.mark.simulation
@pytest.mark.parametrize(("seconds", "passes"), [(4, True), (6, False)])
def test_the_in_flight_limit_bounds_the_throughput_at_b_250(seconds: int, passes: bool) -> None:
    async def scenario(tmp_path: Path) -> None:
        series = sustained_capacity(250, WAKE_OFFSET)
        result = await simulate(tmp_path, series=series, b=250, rounds=6, request_ms=seconds * S)
        assert (sum(result.skipped) == 0) is passes

    run(scenario, timeout=10_000)


def test_one_request_in_ten_taking_8_seconds_is_absorbed_at_b_50() -> None:
    async def scenario(tmp_path: Path) -> None:
        series = sustained_capacity(50, WAKE_OFFSET)
        result = await simulate(tmp_path, series=series, b=50, rounds=6, slow=lambda n: 8 * S if n % 10 == 0 else 0)
        assert sum(result.skipped) == 0

    run(scenario, timeout=3_000)


def test_funding_and_open_interest_limiters_are_independent_and_a_funding_429_does_not_stop_open_interest() -> None:
    from quasar_market_data.collector.open_interest_budget import OpenInterestBudget

    async def run_once(tmp_path: Path, with_open_interest: bool, pause_funding: bool) -> tuple[list[int], list[int]]:
        series = 100
        t = ManualTime(wall_start_ms=B0 + 3_000)
        sink = InMemorySink()
        rig = await make_rig(tmp_path, symbols=symbols_of(series), budget_per_5min=50, sink=sink, t=t)
        for symbol in symbols_of(series):
            seed_state(rig, symbol, Interval(B0 - 3 * HOUR - 86_400_000, B0 - 3_600_000))
        oi_starts: list[int] = []
        hammer_task: asyncio.Task[None] | None = None
        if with_open_interest:
            budget = OpenInterestBudget(100, t, t.sleep)

            async def hammer() -> None:
                while True:
                    await budget.acquire()
                    oi_starts.append(t.monotonic_ns())

            hammer_task = asyncio.ensure_future(hammer())
        if pause_funding:
            rig.exchange.last_response_headers = {"Retry-After": "1800"}
            rig.exchange.errors = [ccxt.DDoSProtection("binanceusdm 429 Too Many Requests {}")]
        h = await start_task(tmp_path, rig=rig, budget_per_5min=50, backfill_days=1)
        await h.run_exact(3 * HOUR)
        if hammer_task is not None:
            hammer_task.cancel()
            await asyncio.gather(hammer_task, return_exceptions=True)
        return sorted(ns for _, ns in rig.limiter.grants), oi_starts

    def worst(starts: list[int], window_ms: int) -> int:
        ordered, low, best = sorted(starts), 0, 0
        for high, instant in enumerate(ordered):
            while ordered[low] <= instant - window_ms * NS_PER_MS:
                low += 1
            best = max(best, high - low + 1)
        return best

    async def scenario(tmp_path: Path) -> None:
        for name in ("alone", "together", "paused"):
            (tmp_path / name).mkdir()
        alone, _ = await run_once(tmp_path / "alone", False, False)
        together, oi = await run_once(tmp_path / "together", True, False)
        assert together == alone  # saturating open interest does not move a single funding start
        assert worst(together, WINDOW_MS) <= 50 and worst(oi, 300_000) <= 100 and len(oi) > 100
        paused, oi_during_pause = await run_once(tmp_path / "paused", True, True)
        first_round_end = wake_ns(0) + 3_500_000 * NS_PER_MS

        def widest_gap(starts: list[int]) -> int:
            inside = [ns for ns in starts if ns < first_round_end]
            return max(later - earlier for earlier, later in itertools.pairwise(inside)) // NS_PER_MS

        assert widest_gap(paused) >= 1_800_000 > widest_gap(alone)
        silent = [ns for ns in oi_during_pause if paused[0] < ns < paused[0] + 1_800_000 * NS_PER_MS]
        assert len(silent) > 100  # funding was silent for the whole 429 pause; open interest kept starting requests

    run(scenario, timeout=3_000)
