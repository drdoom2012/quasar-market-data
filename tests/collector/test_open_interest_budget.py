import asyncio
import random
from collections.abc import Callable, Coroutine

import pytest

from quasar_market_data.collector.open_interest_budget import WINDOW_MS, OpenInterestBudget
from tests.collector.manual_time import NS_PER_MS, ManualTime, assert_no_leftover_tasks
from tests.typing_helpers import untyped

WINDOW_NS = WINDOW_MS * NS_PER_MS


def run(scenario: Callable[[], Coroutine[None, None, None]]) -> None:
    asyncio.run(scenario())


def max_starts_in_any_window(starts: list[int]) -> int:
    """The largest number of starts g_j with g_i - 300 s < g_j <= g_i over all i (a start counts for 300 s)."""
    ordered = sorted(starts)
    worst = 0
    low = 0
    for high, instant in enumerate(ordered):
        while ordered[low] <= instant - WINDOW_NS:
            low += 1
        worst = max(worst, high - low + 1)
    return worst


async def grant(budget: OpenInterestBudget, t: ManualTime, into: list[int]) -> None:
    await budget.acquire()
    into.append(t.monotonic_ns())


def test_window_is_300_seconds() -> None:
    assert WINDOW_MS == 300_000


def test_b_grants_at_one_instant_then_the_next_waits_until_the_first_is_300_seconds_old() -> None:
    async def scenario() -> None:
        t = ManualTime()
        budget = OpenInterestBudget(3, t, t.sleep)
        granted: list[int] = []
        first = t.monotonic_ns()
        tasks = [asyncio.create_task(grant(budget, t, granted)) for _ in range(4)]
        await t.settle()
        assert granted == [first] * 3
        await t.advance(WINDOW_MS - 1)
        assert len(granted) == 3
        await t.advance(1)
        await asyncio.gather(*tasks)
        assert granted == [first] * 3 + [first + WINDOW_NS]

    run(scenario)


def test_a_start_frees_its_slot_at_exactly_300_seconds() -> None:
    async def scenario() -> None:
        t = ManualTime()
        budget = OpenInterestBudget(1, t, t.sleep)
        granted: list[int] = []
        await grant(budget, t, granted)
        waiter = asyncio.create_task(grant(budget, t, granted))
        await t.advance(WINDOW_MS - 1)
        assert not waiter.done()
        await t.advance(1)
        await waiter
        assert granted[1] - granted[0] == WINDOW_NS

    run(scenario)


def test_an_early_wake_up_does_not_over_grant() -> None:
    async def scenario() -> None:
        t = ManualTime()
        early = {"left": 1}

        async def sleep(ms: int) -> None:
            if early["left"]:
                early["left"] -= 1
                return  # returns at once: the budget must re-read the clock and wait again
            await t.sleep(ms)

        budget = OpenInterestBudget(1, t, sleep)
        granted: list[int] = []
        await grant(budget, t, granted)
        waiter = asyncio.create_task(grant(budget, t, granted))
        await t.settle()
        assert not waiter.done()
        await t.advance(WINDOW_MS)
        await waiter
        assert granted[1] - granted[0] == WINDOW_NS

    run(scenario)


def test_a_cancelled_waiter_consumes_nothing() -> None:
    async def scenario() -> None:
        t = ManualTime()
        budget = OpenInterestBudget(1, t, t.sleep)
        granted: list[int] = []
        await grant(budget, t, granted)
        cancelled = asyncio.create_task(grant(budget, t, granted))
        queued = asyncio.create_task(grant(budget, t, granted))
        await t.settle()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await t.advance(WINDOW_MS)
        await queued
        assert len(granted) == 2
        assert max_starts_in_any_window(granted) == 1
        await assert_no_leftover_tasks()

    run(scenario)


@pytest.mark.parametrize("seed", range(15))
def test_property_never_more_than_b_starts_in_any_300_seconds(seed: int) -> None:
    async def scenario() -> None:
        rng = random.Random(seed)
        b = rng.randint(1, 20)
        t = ManualTime()
        budget = OpenInterestBudget(b, t, t.sleep)
        granted: list[int] = []

        async def consumer() -> None:
            for _ in range(rng.randint(5, 30)):
                await t.sleep(rng.randint(1, 120_000))
                await grant(budget, t, granted)

        tasks = [asyncio.create_task(consumer()) for _ in range(rng.randint(1, 8))]
        while not all(task.done() for task in tasks):
            await t.advance(rng.randint(1, 30_000))
        await asyncio.gather(*tasks)
        assert granted
        assert max_starts_in_any_window(granted) <= b

    run(scenario)


def test_b_100_never_exceeded_with_rounds_retries_time_and_backfill_competing() -> None:
    async def scenario() -> None:
        t = ManualTime()
        budget = OpenInterestBudget(100, t, t.sleep)
        granted: list[int] = []

        async def consumer(kind: int) -> None:
            for _ in range(400):
                await grant(budget, t, granted)
                if kind == 2:
                    await t.sleep(1)  # the "time" consumer asks as fast as it can

        tasks = [asyncio.create_task(consumer(kind)) for kind in range(4)]
        while not all(task.done() for task in tasks):
            await t.advance(7_000)
        await asyncio.gather(*tasks)
        assert len(granted) == 1600
        assert max_starts_in_any_window(granted) == 100  # reached, never exceeded

    run(scenario)


def test_window_checker_detects_a_violation() -> None:
    base = 0
    assert max_starts_in_any_window([base, base + 1, base + WINDOW_NS - 1]) == 3
    assert max_starts_in_any_window([base, base + WINDOW_NS]) == 1


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "5", None])
def test_invalid_budget_is_rejected(bad: object) -> None:
    t = ManualTime()
    with pytest.raises((TypeError, ValueError)):
        OpenInterestBudget(untyped(bad), t, t.sleep)


def test_committed_at_counts_starts_whose_window_still_covers_the_instant() -> None:
    async def scenario() -> None:
        t = ManualTime()
        budget = OpenInterestBudget(5, t, t.sleep)
        origin = t.monotonic_ns()
        await budget.acquire()
        await t.advance(100_000)
        await budget.acquire()
        await t.advance(100_000)
        await budget.acquire()
        # starts at 0, 100 s and 200 s of fake time; the clock now stands at 200 s
        assert budget.committed_at(origin) == 3
        assert budget.committed_at(origin + 300_000 * NS_PER_MS) == 2  # the start at 0 is no longer counted at 300 s
        assert budget.committed_at(origin + 400_000 * NS_PER_MS) == 1
        assert budget.committed_at(origin + 499_999 * NS_PER_MS) == 1
        assert budget.committed_at(origin + 500_000 * NS_PER_MS) == 0  # the start at 200 s expires exactly then

    run(scenario)


def test_committed_at_never_mutates_and_equals_b_after_b_acquires() -> None:
    async def scenario() -> None:
        t = ManualTime()
        budget = OpenInterestBudget(4, t, t.sleep)
        for _ in range(4):
            await budget.acquire()
        now = t.monotonic_ns()
        assert budget.committed_at(now) == 4
        assert budget.committed_at(now + 10 * WINDOW_NS) == 0  # a far instant does not drop anything
        assert budget.committed_at(now) == 4
        granted: list[int] = []
        task = asyncio.create_task(grant(budget, t, granted))
        await t.settle()
        assert granted == []  # the fifth still waits: the view removed nothing
        await t.advance(WINDOW_MS)
        await task
        assert len(granted) == 1

    run(scenario)


def test_committed_at_rejects_non_int() -> None:
    t = ManualTime()
    with pytest.raises(TypeError):
        OpenInterestBudget(1, t, t.sleep).committed_at(1.5)  # type: ignore[arg-type]


def test_committed_at_can_leave_out_the_starts_of_a_range() -> None:
    async def scenario() -> None:
        t = ManualTime()
        budget = OpenInterestBudget(10, t, t.sleep)
        origin = t.monotonic_ns()
        await budget.acquire()  # before the range
        await t.advance(10_000)
        for _ in range(3):  # inside the range
            await budget.acquire()
        await t.advance(10_000)
        await budget.acquire()  # after the range
        span = (origin + 10_000 * NS_PER_MS, origin + 10_000 * NS_PER_MS)
        assert budget.committed_at(origin) == 5
        assert budget.committed_at(origin, excluding=span) == 2
        assert budget.committed_at(origin, excluding=(span[0], span[0] - 1)) == 5  # an empty range excludes nothing

    run(scenario)
