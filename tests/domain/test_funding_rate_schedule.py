import random

import pytest

from quasar_market_data.domain.funding_rate_schedule import (
    ROUND_DEADLINE_MARGIN_MS,
    ROUND_PERIOD_MS,
    RoundPlan,
    next_round,
    round_deadline_ms,
    round_wake_ms,
)
from tests.typing_helpers import untyped

HOUR = ROUND_PERIOD_MS
B = 1_793_836_800_000  # a whole hour
assert B % HOUR == 0


def test_constants() -> None:
    assert (ROUND_PERIOD_MS, ROUND_DEADLINE_MARGIN_MS) == (3_600_000, 15_000)


def test_wake_and_deadline() -> None:
    assert round_wake_ms(B, 60_000) == B + 60_000
    assert round_deadline_ms(B) == B + 3_585_000  # 15 s before the next boundary, not before the next wake
    for offset in (60_000, 600_000):
        assert round_deadline_ms(B) < round_wake_ms(B + HOUR, offset)  # two rounds never overlap


def test_arguments_are_checked() -> None:
    with pytest.raises(ValueError):
        round_wake_ms(B + 1, 60_000)  # not a whole hour
    with pytest.raises(ValueError):
        round_wake_ms(B, 59_999)
    with pytest.raises(ValueError):
        round_wake_ms(B, 600_001)
    with pytest.raises(TypeError):
        round_wake_ms(untyped(True), 60_000)
    with pytest.raises(TypeError):
        next_round(untyped(1.5), None, 60_000)


def model(now: int, last: int | None, offset: int) -> RoundPlan:
    """Brute force: walk the boundaries around ``now`` and take the first one that may still run."""
    candidates = [B + k * HOUR for k in range(-3, 400)]
    for boundary in candidates:
        if last is not None and boundary <= last:
            continue
        wake, deadline = boundary + offset, boundary + HOUR - ROUND_DEADLINE_MARGIN_MS
        if now < deadline:  # the wake may be in the past (run late) or in the future
            skipped = 0 if last is None else (boundary - last) // HOUR - 1
            return RoundPlan(boundary, wake, skipped)
    raise AssertionError("no boundary")


@pytest.mark.parametrize("seed", range(6))
def test_next_round_against_a_brute_force_model(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(300):
        offset = rng.choice([60_000, 120_000, 505_001, 600_000, rng.randint(60_000, 600_000)])
        now = B + rng.randint(0, 120 * HOUR)
        last = None if rng.random() < 0.2 else B + rng.randint(-2, 118) * HOUR
        if last is not None and last < B:
            last = B
        assert next_round(now, last, offset) == model(now, last, offset), (now, last, offset)


@pytest.mark.parametrize(
    ("now_offset", "expected_boundary_hours"),
    [
        (60_000 - 1, 0),  # one ms before the wake of boundary 0: wait for it
        (60_000, 0),  # exactly at the wake
        (3_585_000 - 1, 0),  # one ms before the deadline: still runs, late
        (3_585_000, 1),  # exactly at the deadline: skipped, the next wake
        (3_599_999, 1),  # inside the last 15 s
        (3_600_000 + 60_000, 1),  # the wake of boundary 1
    ],
)
def test_the_edges_with_no_previous_round(now_offset: int, expected_boundary_hours: int) -> None:
    plan = next_round(B + now_offset, None, 60_000)
    assert plan.boundary_ms == B + expected_boundary_hours * HOUR and plan.skipped == 0


def test_skipped_boundaries_are_counted() -> None:
    plan = next_round(B + 5 * HOUR + 70_000, B, 60_000)
    assert (plan.boundary_ms, plan.skipped) == (B + 5 * HOUR, 4)
    again = next_round(B + 5 * HOUR + 70_000, B + 5 * HOUR, 60_000)
    assert (again.boundary_ms, again.skipped) == (B + 6 * HOUR, 0)
