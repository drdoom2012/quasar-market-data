import random

import pytest

from quasar_market_data.domain.funding_rate_suspension import (
    FAILURES_TO_SUSPEND,
    RETRY_DELAYS_MS,
    SUSPEND_MS,
    FailureRecord,
    on_failure,
    on_success,
    unavailable_until_ms,
)

H = 3_600_000
T0 = 1_000_000


def test_constants() -> None:
    assert RETRY_DELAYS_MS == (1_000, 2_000)
    assert SUSPEND_MS == (6 * H, 12 * H, 24 * H) and FAILURES_TO_SUSPEND == 3


def test_a_fresh_record_is_available() -> None:
    assert unavailable_until_ms(FailureRecord()) == 0


def test_the_first_two_failures_retry_after_one_and_two_seconds() -> None:
    first = on_failure(FailureRecord(), T0)
    assert (first.consecutive, first.retry_at_ms, first.suspended_until_ms) == (1, T0 + 1_000, 0)
    second = on_failure(first, T0 + 5_000)
    assert (second.consecutive, second.retry_at_ms, second.suspended_until_ms) == (2, T0 + 7_000, 0)
    assert unavailable_until_ms(second) == T0 + 7_000


def test_the_third_consecutive_failure_suspends_for_six_hours() -> None:
    record = FailureRecord()
    for step in range(3):
        record = on_failure(record, T0 + step * 10_000)
    assert record.suspensions == 1 and record.consecutive == 0
    assert record.suspended_until_ms == T0 + 20_000 + 6 * H
    assert unavailable_until_ms(record) == record.suspended_until_ms


def test_a_failed_probe_suspends_again_with_the_longer_time() -> None:
    record = FailureRecord()
    now = T0
    expected = [6 * H, 12 * H, 24 * H, 24 * H]
    for index, duration in enumerate(expected):
        if index == 0:
            for _ in range(3):
                record = on_failure(record, now)
        else:
            now = record.suspended_until_ms  # the suspension has expired: one probe
            record = on_failure(record, now)  # and it fails
        assert record.suspended_until_ms == now + duration, index
        assert record.suspensions == index + 1


def test_a_success_clears_everything_including_the_suspension_count() -> None:
    record = FailureRecord()
    for _ in range(3):
        record = on_failure(record, T0)
    assert on_success(record) == FailureRecord()
    again = on_failure(on_success(record), T0)  # not a probe any more
    assert again.consecutive == 1 and again.suspended_until_ms == 0


def test_a_success_between_failures_resets_the_count() -> None:
    record = on_failure(on_failure(FailureRecord(), T0), T0)
    record = on_failure(on_success(record), T0)
    assert record.consecutive == 1 and record.suspensions == 0


@pytest.mark.parametrize("seed", range(6))
def test_property_over_random_sequences(seed: int) -> None:
    rng = random.Random(seed)
    record = FailureRecord()
    now = T0
    durations: list[int] = []
    for _ in range(500):
        now += rng.randint(0, 30 * H)
        if rng.random() < 0.15:
            record = on_success(record)
            durations = []
            assert record == FailureRecord()
            continue
        before = record
        record = on_failure(record, now)
        assert 0 <= record.consecutive < FAILURES_TO_SUSPEND
        if record.suspensions > before.suspensions:
            durations.append(record.suspended_until_ms - now)
            assert durations[-1] == SUSPEND_MS[min(len(durations) - 1, 2)]
        assert durations == sorted(durations)  # never shorter without a success in between
        assert unavailable_until_ms(record) >= max(record.suspended_until_ms, record.retry_at_ms)
