import asyncio
import random
from collections.abc import Callable, Coroutine

import pytest

from quasar_market_data.collector.funding_rate_limiter import (
    MAX_IN_FLIGHT,
    WINDOW_MS,
    FundingRateLimiter,
    RequestClass,
)
from tests.collector.manual_time import NS_PER_MS, ManualTime, assert_no_leftover_tasks
from tests.typing_helpers import untyped

R, C, H = RequestClass.R, RequestClass.C, RequestClass.H
WINDOW_NS = WINDOW_MS * NS_PER_MS
Grants = list[tuple[RequestClass, int]]


REAL_TIMEOUT_S = 30  # a regression in the boundary arithmetic spins the fake clock forever: fail instead of hanging


def run(scenario: Callable[[], Coroutine[None, None, None]]) -> None:
    async def guarded() -> None:
        await asyncio.wait_for(scenario(), REAL_TIMEOUT_S)

    asyncio.run(guarded())


def shares(b: int) -> tuple[int, int]:
    return b // 2, b - b // 2


async def use(
    limiter: FundingRateLimiter,
    t: ManualTime,
    request_class: RequestClass,
    into: Grants,
    hold: asyncio.Event | None = None,
) -> None:
    async with limiter.grant(request_class):
        into.append((request_class, t.monotonic_ns()))
        if hold is not None:
            await hold.wait()


async def drain(t: ManualTime) -> None:
    """``settle`` grants 100 hops; a batch of 125 queued grants needs more, so settle until nothing moves."""
    for _ in range(10):
        await t.settle()


def max_in_any_window(starts: list[int]) -> int:
    ordered = sorted(starts)
    worst = low = 0
    for high, instant in enumerate(ordered):
        while ordered[low] <= instant - WINDOW_NS:
            low += 1
        worst = max(worst, high - low + 1)
    return worst


def test_constants() -> None:
    assert WINDOW_MS == 305_000
    assert MAX_IN_FLIGHT == 2


@pytest.mark.parametrize("b", [50, 51, 150, 151, 250])
def test_lockstep_request_k_plus_b_starts_exactly_305_s_after_request_k(b: int) -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(b, t, t.sleep)
        m, other = shares(b)
        grants: Grants = []
        first = t.monotonic_ns()
        tasks = [asyncio.create_task(use(limiter, t, R, grants)) for _ in range(3 * m)]
        tasks += [asyncio.create_task(use(limiter, t, C if i % 2 else H, grants)) for i in range(3 * other)]
        # One window at a time: ``advance`` must not jump past instants at which queued grants still have to run.
        for step in range(3):
            await drain(t)
            assert len(grants) == b * (step + 1)
            if step < 2:
                await t.advance(WINDOW_MS - 1)
                await drain(t)
                assert len(grants) == b * (step + 1)
                await t.advance(1)
        await asyncio.gather(*tasks)
        starts = sorted(instant for _, instant in grants)
        assert len(starts) == 3 * b
        assert [starts[k + b] - starts[k] for k in range(2 * b)] == [WINDOW_NS] * (2 * b)
        assert starts[0] == first
        assert max_in_any_window(starts) == b

    run(scenario)


@pytest.mark.parametrize("b", [50, 151])
def test_single_group_streams_grant_the_cap_per_window(b: int) -> None:
    async def scenario() -> None:
        m, other = shares(b)
        for request_class, cap in ((R, m), (C, other), (H, other)):
            t = ManualTime()
            limiter = FundingRateLimiter(b, t, t.sleep)
            grants: Grants = []
            first = t.monotonic_ns()
            tasks = [asyncio.create_task(use(limiter, t, request_class, grants)) for _ in range(2 * cap)]
            await t.settle()
            assert [instant for _, instant in grants] == [first] * cap
            await t.advance(WINDOW_MS)
            await asyncio.gather(*tasks)
            assert sorted(instant for _, instant in grants) == [first] * cap + [first + WINDOW_NS] * cap

    run(scenario)


def test_c_and_h_share_one_cap() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(150, t, t.sleep)  # m = 75, C+H = 75
        grants: Grants = []
        tasks = [asyncio.create_task(use(limiter, t, C if i % 2 else H, grants)) for i in range(80)]
        await t.settle()
        assert len(grants) == 75 and {request_class for request_class, _ in grants} == {C, H}
        await t.advance(WINDOW_MS)
        await asyncio.gather(*tasks)
        assert len(grants) == 80

    run(scenario)


def test_groups_do_not_borrow_from_each_other() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)  # 25 + 25
        grants: Grants = []
        for _ in range(25):
            await use(limiter, t, R, grants)
        waiting_r = asyncio.create_task(use(limiter, t, R, grants))
        await t.settle()
        await use(limiter, t, C, grants)  # R is saturated: C still starts at once
        assert (C, t.monotonic_ns()) in grants and not waiting_r.done()
        for _ in range(24):
            await use(limiter, t, H, grants)
        other_waiting = asyncio.create_task(use(limiter, t, C, grants))
        await t.settle()
        assert not other_waiting.done()  # C+H saturated at 25
        await t.advance(WINDOW_MS)
        await asyncio.gather(waiting_r, other_waiting)

    run(scenario)


def test_a_saturated_group_does_not_hold_the_slots_against_the_other() -> None:
    async def scenario() -> None:
        for saturated, free in ((R, C), (C, R)):
            t = ManualTime()
            limiter = FundingRateLimiter(50, t, t.sleep)
            grants: Grants = []
            for _ in range(25):
                await use(limiter, t, saturated, grants)
            sleepers = [asyncio.create_task(use(limiter, t, saturated, grants)) for _ in range(3)]
            await t.settle()
            assert not any(task.done() for task in sleepers)
            before = len(grants)
            await use(limiter, t, free, grants)  # both slots must still be free
            assert len(grants) == before + 1 and grants[-1][1] == t.monotonic_ns()
            await t.advance(WINDOW_MS)
            await asyncio.gather(*sleepers)

    run(scenario)


def test_a_start_is_recorded_only_when_a_slot_is_held() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(150, t, t.sleep)
        grants: Grants = []
        hold = [asyncio.Event(), asyncio.Event()]
        busy = [asyncio.create_task(use(limiter, t, R, grants, hold[i])) for i in range(2)]
        await t.settle()
        assert limiter.starts_in_window(t.monotonic_ns()) == 2
        third = asyncio.create_task(use(limiter, t, C, grants))
        await t.settle()
        assert not third.done()
        assert limiter.starts_in_window(t.monotonic_ns()) == 2  # room exists but no slot: no budget used
        await t.advance(1_000)
        hold[0].set()
        await third
        assert limiter.starts_in_window(t.monotonic_ns()) == 3
        assert grants[-1] == (C, t.monotonic_ns())  # recorded with the reading taken when the slot was held
        assert grants[-1][1] - grants[0][1] == 1_000 * NS_PER_MS
        # the recorded start is the reading taken when the slot was held (1 s after the first), not before the wait
        after_first_window = grants[0][1] + WINDOW_NS + 500 * NS_PER_MS
        assert limiter.starts_in_window(after_first_window, C) == 1
        hold[1].set()
        await asyncio.gather(*busy)

    run(scenario)


def test_at_most_two_in_flight_and_a_cancelled_call_releases_its_slot() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(150, t, t.sleep)
        grants: Grants = []
        holds = [asyncio.Event() for _ in range(4)]
        tasks = [asyncio.create_task(use(limiter, t, (R, C, H, R)[i], grants, holds[i])) for i in range(4)]
        await t.settle()
        assert len(grants) == MAX_IN_FLIGHT
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        await t.settle()
        assert len(grants) == 3  # the freed slot went to the next waiter
        holds[1].set()
        await t.settle()
        assert len(grants) == 4
        for hold in holds:
            hold.set()
        await asyncio.gather(*tasks[1:])

    run(scenario)


def earliest_starts(b: int, requests: list[tuple[int, RequestClass]]) -> list[int]:
    """Independent oracle: FIFO per group, earliest instant (ms) with room in (t - 305 s, t]."""
    m, other = shares(b)
    starts: dict[str, list[int]] = {"R": [], "CH": []}
    result = [0] * len(requests)
    order = sorted(range(len(requests)), key=lambda i: (requests[i][0], i))
    last = {"R": 0, "CH": 0}
    for i in order:
        arrival, request_class = requests[i]
        name, cap = ("R", m) if request_class is R else ("CH", other)
        instant = max(arrival, last[name])
        while True:
            counted = [s for s in starts[name] if s > instant - WINDOW_MS]
            if len(counted) < cap:
                break
            instant = min(counted) + WINDOW_MS
        starts[name].append(instant)
        last[name] = instant
        result[i] = instant
    return result


@pytest.mark.parametrize("seed", range(6))
def test_property_each_request_starts_at_its_earliest_allowed_instant(seed: int) -> None:
    async def scenario() -> None:
        rng = random.Random(seed)
        b = rng.choice([50, 51, 150, 250])
        t = ManualTime()
        base = t.monotonic_ns() // NS_PER_MS
        limiter = FundingRateLimiter(b, t, t.sleep)
        arrival = 0
        requests: list[tuple[int, RequestClass]] = []
        for _ in range(150):
            arrival += rng.choice([0, 0, 1, rng.randint(2, 5_000), 304_999, 305_000])
            requests.append((arrival, rng.choice([R, R, C, H])))
        granted: dict[int, int] = {}

        async def one(index: int) -> None:
            await t.sleep(requests[index][0])
            async with limiter.grant(requests[index][1]):
                granted[index] = t.monotonic_ns() // NS_PER_MS - base

        tasks = [asyncio.create_task(one(i)) for i in range(len(requests))]
        await t.settle()  # every task has registered its arrival sleep
        while not all(task.done() for task in tasks):
            await t.advance(rng.randint(1, 30_000))
        await asyncio.gather(*tasks)
        expected = earliest_starts(b, requests)
        assert [granted[i] for i in range(len(requests))] == expected
        m, other = shares(b)
        r_starts = [granted[i] * NS_PER_MS for i, (_, c) in enumerate(requests) if c is R]
        ch_starts = [granted[i] * NS_PER_MS for i, (_, c) in enumerate(requests) if c is not R]
        assert max_in_any_window(r_starts) <= m and max_in_any_window(ch_starts) <= other
        assert max_in_any_window(r_starts + ch_starts) <= b

    run(scenario)


def test_a_cancelled_waiter_consumes_nothing_queued_or_sleeping() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)
        grants: Grants = []
        for _ in range(25):
            await use(limiter, t, R, grants)
        sleeping = asyncio.create_task(use(limiter, t, R, grants))
        queued = asyncio.create_task(use(limiter, t, R, grants))
        survivor = asyncio.create_task(use(limiter, t, R, grants))
        await t.settle()
        sleeping.cancel()
        queued.cancel()
        for task in (sleeping, queued):
            with pytest.raises(asyncio.CancelledError):
                await task
        await t.advance(WINDOW_MS)
        await survivor
        assert len(grants) == 26
        assert limiter.starts_in_window(t.monotonic_ns(), R) == 1  # the first batch left the window; one start counts
        await assert_no_leftover_tasks()

    run(scenario)


def test_a_request_cancelled_at_its_deadline_records_nothing_and_holds_no_slot() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)
        grants: Grants = []
        for _ in range(25):
            await use(limiter, t, R, grants)
        waiting = asyncio.create_task(use(limiter, t, R, grants))
        await t.settle()
        await t.advance(10_000)  # the caller's deadline passes
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert len(grants) == 25 and limiter.starts_in_window(t.monotonic_ns(), R) == 25
        later = asyncio.create_task(use(limiter, t, R, grants))
        await t.settle()
        await t.advance(WINDOW_MS - 10_000)  # the window of the first batch ends: the next request starts
        await later
        assert grants[-1][1] - grants[0][1] == WINDOW_NS
        await use(limiter, t, C, grants)  # no slot leaked
        await assert_no_leftover_tasks()

    run(scenario)


def test_fifo_within_a_group() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)
        order: list[int] = []

        async def tagged(index: int) -> None:
            async with limiter.grant(R):
                order.append(index)

        tasks = []
        for index in range(60):
            tasks.append(asyncio.create_task(tagged(index)))
            await t.settle()
        await t.advance(WINDOW_MS)
        await t.advance(WINDOW_MS)
        await asyncio.gather(*tasks)
        assert order == list(range(60))

    run(scenario)


def test_an_early_wake_up_does_not_over_grant() -> None:
    async def scenario() -> None:
        t = ManualTime()
        early = {"left": 1}

        async def sleep(ms: int) -> None:
            if early["left"]:
                early["left"] -= 1
                return  # returns at once: the limiter must re-read the clock and wait again
            await t.sleep(ms)

        limiter = FundingRateLimiter(50, t, sleep)
        grants: Grants = []
        for _ in range(25):
            await use(limiter, t, R, grants)
        waiter = asyncio.create_task(use(limiter, t, R, grants))
        await t.settle()
        assert not waiter.done()
        await t.advance(WINDOW_MS)
        await waiter
        assert grants[-1][1] - grants[0][1] == WINDOW_NS

    run(scenario)


def test_starts_in_window_counts_left_open_and_per_group() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(150, t, t.sleep)
        grants: Grants = []
        first = t.monotonic_ns()
        for request_class in (R, R, C, H, H):
            await use(limiter, t, request_class, grants)
        assert limiter.starts_in_window(first) == 5
        assert limiter.starts_in_window(first, R) == 2
        assert limiter.starts_in_window(first, C) == 3 and limiter.starts_in_window(first, H) == 3
        assert limiter.starts_in_window(first + WINDOW_NS - 1) == 5
        assert limiter.starts_in_window(first + WINDOW_NS) == 0  # a start leaves at exactly 305 s

    run(scenario)


def test_budget_and_arguments_are_validated() -> None:
    t = ManualTime()
    for bad in (49, 251, "150", 150.0, True):
        with pytest.raises((TypeError, ValueError)):
            FundingRateLimiter(untyped(bad), t, t.sleep)
    for bad_slots in (0, -1, True, 1.5):
        with pytest.raises((TypeError, ValueError)):
            FundingRateLimiter(150, t, t.sleep, max_in_flight=untyped(bad_slots))
    limiter = FundingRateLimiter(150, t, t.sleep)
    with pytest.raises(TypeError):
        limiter.grant(untyped("R"))
    with pytest.raises(TypeError):
        limiter.starts_in_window(untyped(1.5))
    with pytest.raises(TypeError):
        limiter.starts_in_window(0, untyped("R"))


def test_the_class_aliases_are_the_domain_enum() -> None:
    from quasar_market_data.domain.funding_rate_capacity import RequestClass as DomainRequestClass

    assert RequestClass is DomainRequestClass


def test_the_group_lock_is_held_while_waiting_for_a_slot_so_the_room_cannot_shrink() -> None:
    """Room is seen, then both slots are busy: a second waiter of the group must not see the same room."""

    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)  # m = 25
        grants: Grants = []
        first = t.monotonic_ns()
        for _ in range(24):
            await use(limiter, t, R, grants)
        holds = [asyncio.Event(), asyncio.Event()]
        busy = [asyncio.create_task(use(limiter, t, C, grants, holds[i])) for i in range(2)]
        await t.settle()
        r1 = asyncio.create_task(use(limiter, t, R, grants))
        r2 = asyncio.create_task(use(limiter, t, R, grants))
        await t.settle()
        assert limiter.starts_in_window(t.monotonic_ns(), R) == 24  # both wait for a slot, nothing recorded
        holds[0].set()
        await t.settle()
        assert r1.done() and not r2.done()
        assert limiter.starts_in_window(t.monotonic_ns(), R) == 25  # the cap, not 26
        holds[1].set()
        await t.settle()
        assert not r2.done()  # the group is full until the first batch leaves the window
        await t.advance(WINDOW_MS - 1)
        assert not r2.done()
        await t.advance(1)
        await asyncio.gather(r1, r2, *busy)
        assert grants[-1] == (R, first + WINDOW_NS)
        assert max_in_any_window([instant for c, instant in grants if c is R]) <= 25

    run(scenario)


@pytest.mark.parametrize("seed", range(3))
def test_property_with_slots_held_no_group_exceeds_its_cap(seed: int) -> None:
    async def scenario() -> None:
        rng = random.Random(seed)
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)
        starts: dict[RequestClass, list[int]] = {R: [], C: [], H: []}

        async def one() -> None:
            await t.sleep(rng.randint(0, 20_000))
            request_class = rng.choice([R, R, C, H])
            async with limiter.grant(request_class):
                starts[request_class].append(t.monotonic_ns())
                await t.sleep(rng.choice([0, 1, 500, 5_000, 60_000]))  # the call holds its slot

        tasks = [asyncio.create_task(one()) for _ in range(120)]
        await t.settle()
        while not all(task.done() for task in tasks):
            await t.advance(rng.randint(1, 20_000))
        await asyncio.gather(*tasks)
        assert max_in_any_window(starts[R]) <= 25
        assert max_in_any_window(starts[C] + starts[H]) <= 25
        assert max_in_any_window(starts[R] + starts[C] + starts[H]) <= 50

    run(scenario)


def test_the_wait_is_rounded_up_to_a_whole_millisecond() -> None:
    async def scenario() -> None:
        t = ManualTime()
        asked: list[int] = []

        async def sleep(ms: int) -> None:
            asked.append(ms)
            await t.sleep(ms)

        limiter = FundingRateLimiter(50, t, sleep)
        grants: Grants = []
        for _ in range(25):
            await use(limiter, t, R, grants)
        await t.advance_ns(500_000)  # half a millisecond later
        waiter = asyncio.create_task(use(limiter, t, R, grants))
        await t.settle()
        assert asked == [305_000]  # 304 999.5 ms remain: rounded up, never down
        await t.advance(WINDOW_MS)
        await waiter

    run(scenario)


def test_room_is_true_until_counted_plus_waiting_reaches_the_cap_for_each_group() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)  # 25 + 25
        grants: Grants = []
        now = t.monotonic_ns
        assert limiter.room(R, now()) and limiter.room(C, now()) and limiter.room(H, now())
        for _ in range(24):
            await use(limiter, t, R, grants)
        assert limiter.room(R, now())
        await use(limiter, t, R, grants)
        assert not limiter.room(R, now()) and limiter.room(C, now()) and limiter.room(H, now())
        for _ in range(25):
            await use(limiter, t, H if _ % 2 else C, grants)
        assert not limiter.room(C, now()) and not limiter.room(H, now())  # one shared group
        await t.advance(WINDOW_MS)
        assert limiter.room(R, now()) and limiter.room(C, now())

    run(scenario)


def test_a_request_waiting_in_grant_is_reserved_before_its_start_is_recorded() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)
        grants: Grants = []
        hold = asyncio.Event()
        busy = [asyncio.create_task(use(limiter, t, C, grants, hold)) for _ in range(2)]  # both slots held
        await t.settle()
        waiters = [asyncio.create_task(use(limiter, t, R, grants)) for _ in range(25)]
        await asyncio.sleep(0)  # the workers run until their first real wait: the reservation exists
        assert limiter.starts_in_window(t.monotonic_ns(), R) == 0
        assert not limiter.room(R, t.monotonic_ns())  # 25 waiting == the cap
        assert limiter.room(C, t.monotonic_ns())
        hold.set()
        await drain(t)
        await asyncio.gather(*busy, *waiters)
        assert limiter.starts_in_window(t.monotonic_ns(), R) == 25
        assert not limiter.room(R, t.monotonic_ns())

    run(scenario)


def test_a_cancelled_waiter_releases_its_reservation() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)
        grants: Grants = []
        hold = asyncio.Event()
        busy = [asyncio.create_task(use(limiter, t, C, grants, hold)) for _ in range(2)]
        await t.settle()
        waiter = asyncio.create_task(use(limiter, t, R, grants))
        await asyncio.sleep(0)
        assert limiter._rounds.waiting == 1
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert limiter._rounds.waiting == 0 and limiter.room(R, t.monotonic_ns())
        hold.set()
        await asyncio.gather(*busy)

    run(scenario)


def test_next_room_is_now_with_room_and_otherwise_when_the_oldest_counted_start_leaves() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)
        grants: Grants = []
        first = t.monotonic_ns()
        assert limiter.next_room_ns(R, first) == first
        for index in range(25):
            if index == 10:
                await t.advance(7_000)
            await use(limiter, t, R, grants)
        now = t.monotonic_ns()
        assert limiter.next_room_ns(R, now) == first + WINDOW_NS  # exactly 305 s after the oldest start
        assert limiter.next_room_ns(C, now) == now
        await t.advance(WINDOW_MS)
        assert limiter.next_room_ns(R, t.monotonic_ns()) == t.monotonic_ns()  # the first ten left, 15 remain

    run(scenario)


def test_next_room_with_waiters_beyond_the_counted_starts_is_bounded_by_one_window() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)
        grants: Grants = []
        hold = asyncio.Event()
        busy = [asyncio.create_task(use(limiter, t, C, grants, hold)) for _ in range(2)]
        await t.settle()
        waiters = [asyncio.create_task(use(limiter, t, R, grants)) for _ in range(25)]
        await asyncio.sleep(0)
        now = t.monotonic_ns()
        assert now < limiter.next_room_ns(R, now) <= now + WINDOW_NS
        hold.set()
        await drain(t)
        await asyncio.gather(*busy, *waiters)

    run(scenario)


@pytest.mark.parametrize("seed", range(6))
def test_peak_starts_equals_the_largest_total_in_any_window_and_resets(seed: int) -> None:
    async def scenario() -> None:
        rng = random.Random(seed)
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)
        starts: list[int] = []

        async def one() -> None:
            await t.sleep(rng.randint(0, 700_000))
            async with limiter.grant(rng.choice([R, C, H])):
                starts.append(t.monotonic_ns())

        tasks = [asyncio.create_task(one()) for _ in range(150)]
        await t.settle()
        while not all(task.done() for task in tasks):
            await t.advance(rng.randint(1, 40_000))
        await asyncio.gather(*tasks)
        assert limiter.peak_starts() == max_in_any_window(starts)
        assert limiter.peak_starts() == 0  # reset by the call

    run(scenario)


def test_next_room_counts_the_waiting_requests_ahead_of_the_asker() -> None:
    async def scenario() -> None:
        t = ManualTime()
        limiter = FundingRateLimiter(50, t, t.sleep)
        grants: Grants = []
        for _ in range(25):
            await use(limiter, t, R, grants)
            await t.advance(10)
        starts = [instant for _, instant in grants]
        waiters = [asyncio.create_task(use(limiter, t, R, grants)) for _ in range(2)]
        await t.settle()
        now = t.monotonic_ns()
        # 25 counted + 2 waiting: three starts must leave before a third waiter would find room
        assert limiter.next_room_ns(R, now) == starts[2] + WINDOW_NS
        await t.advance(WINDOW_MS)
        await drain(t)
        await asyncio.gather(*waiters)

    run(scenario)
