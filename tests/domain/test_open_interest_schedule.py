import pytest

from quasar_market_data.domain.open_interest_schedule import (
    ROUND_DEADLINE_RESERVE_MS,
    NextRound,
    backfill_may_start,
    expected_after_ms,
    idle_deadline_ms,
    next_round,
    round_deadline_ms,
    round_range,
    round_wake_ms,
)
from quasar_market_data.domain.timeframe import Timeframe

P = Timeframe("5m")
STEP = P.milliseconds
T0 = 1_792_677_000_000 // STEP * STEP  # an aligned boundary


@pytest.mark.parametrize(("c", "ends"), [(1, 3), (5, 11), (12, 25)])
def test_the_round_range_has_two_c_plus_one_period_ends(c: int, ends: int) -> None:
    start, end = round_range(T0, c, P)
    assert end == T0 and start == T0 - 2 * c * STEP and (end - start) // STEP + 1 == ends


@pytest.mark.parametrize(("c", "offset", "expected"), [(1, 200_000, 500_000), (5, 200_000, 1_700_000)])
def test_expected_after_is_the_adr_figure(c: int, offset: int, expected: int) -> None:
    assert expected_after_ms(c, offset, P) == expected


def test_wake_and_deadline_follow_the_adr() -> None:
    assert round_wake_ms(T0, 200_000, P) == T0 + 200_000
    assert round_deadline_ms(T0, 200_000, P) == T0 + 300_000 + 200_000 - 15_000
    assert idle_deadline_ms(T0 + 200_000) == T0 + 200_000 - ROUND_DEADLINE_RESERVE_MS


@pytest.mark.parametrize(
    ("now_offset", "last", "boundary", "skipped"),
    [
        (100_000, None, T0 - STEP, 0),  # T0 - 5 min woke at T0 - 100 s and its deadline is T0 + 185 s: runs late
        (190_000, None, T0, 0),  # that one is past its deadline now: wait for T0
        (200_000, None, T0, 0),  # exactly at the wake: run now
        (484_999, None, T0, 0),  # one ms before the deadline: still runs, late
        (485_000, None, T0 + STEP, 0),  # exactly at the deadline: skipped, wait for the next
        (200_000, T0 - STEP, T0, 0),
        (200_000, T0, T0 + STEP, 0),  # T0 just finished: the next one
        (200_000, T0 - 4 * STEP, T0, 3),  # a stall: three boundaries never run
        (485_000, T0 - STEP, T0 + STEP, 1),  # the deadline passed while the last one ran
    ],
)
def test_next_round_cases(now_offset: int, last: int | None, boundary: int, skipped: int) -> None:
    result = next_round(T0 + now_offset, last, 200_000, P)
    assert result == NextRound(boundary, boundary + 200_000, skipped)


@pytest.mark.parametrize("offset", [60_000, 200_000, 280_000])
@pytest.mark.parametrize("last_choice", ["none", "recent", "stalled", "future"])
def test_next_round_matches_a_brute_force_model(offset: int, last_choice: str) -> None:
    for second in range(20 * 60):
        now = T0 + second * 1000
        last = {"none": None, "recent": now // STEP * STEP - STEP, "stalled": now // STEP * STEP - 4 * STEP}.get(
            last_choice, now // STEP * STEP + STEP
        )
        result = next_round(now, last, offset, P)
        base = (now // STEP) * STEP
        candidates = [b for b in range(base - 3 * STEP, base + 4 * STEP, STEP) if last is None or b > last]
        wanted = next(b for b in candidates if now < b + offset + STEP - ROUND_DEADLINE_RESERVE_MS)
        assert result.boundary_ms == wanted, (now, last)
        assert result.wake_ms == wanted + offset
        assert result.skipped == (0 if last is None else (wanted - last) // STEP - 1)


@pytest.mark.parametrize(
    ("committed_now", "committed_at_wake", "allowed"),
    [(157, 157, True), (158, 158, False), (0, 157, True), (0, 158, False), (400, 0, False), (399, 0, True)],
)
def test_backfill_may_start(committed_now: int, committed_at_wake: int, allowed: bool) -> None:
    # S = 600 at b = 400: round cost 242
    assert backfill_may_start(committed_now, committed_at_wake, 400, 242) is allowed


def test_the_budget_full_now_but_free_at_the_next_wake_refuses() -> None:
    assert backfill_may_start(400, 0, 400, 22) is False


def test_the_boundary_is_exact() -> None:
    assert backfill_may_start(0, 400 - 1 - 22, 400, 22) is True
    assert backfill_may_start(0, 400 - 22, 400, 22) is False


def test_wrong_types_are_rejected() -> None:
    with pytest.raises(TypeError):
        next_round(float(T0), None, 200_000, P)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        backfill_may_start(True, 0, 400, 22)
    with pytest.raises(ValueError):
        next_round(T0 + 1, T0 + 1, 200_000, P)  # unaligned last boundary
    with pytest.raises(ValueError):
        round_wake_ms(T0, 300_000, P)  # offset must be inside the period
