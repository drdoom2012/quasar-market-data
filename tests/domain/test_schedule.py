import random
from typing import Any

import pytest

from quasar_market_data.domain.closed_candles import is_closed_eligible
from quasar_market_data.domain.schedule import (
    CollectionPlan,
    expected_at_ms,
    newest_expected_open_ms,
    next_wake_ms,
    plan_collection,
)
from quasar_market_data.domain.timed_coverage import ExpectationRule, classify_span_at
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, MIN_TIMESTAMP_MS

MIN = MIN_TIMESTAMP_MS
D_DAY = 86_400_000
TF = Timeframe("5m")
TF_MS = 300_000
LABELS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]


@pytest.mark.parametrize("label", LABELS)
def test_the_first_valid_open_time_is_min_for_every_timeframe(label: str) -> None:
    assert Timeframe(label).is_aligned(MIN)
    assert not Timeframe(label).is_aligned(MIN + 1)  # MIN - 1 is out of range and raises


def _rows(delay: int) -> list[tuple[int, int | None, int]]:
    first = MIN + TF_MS + delay
    return [
        (MIN, None, first),
        (first - 1, None, first),
        (first, MIN, first + TF_MS),
        (first + 1, MIN, first + TF_MS),
        (first + TF_MS - 1, MIN, first + TF_MS),
        (first + TF_MS, MIN + TF_MS, first + 2 * TF_MS),
    ]


@pytest.mark.parametrize("delay", [0, 15_000, 300_000, 450_000])
def test_exact_values_at_the_start_of_the_range(delay: int) -> None:
    for now, newest, wake in _rows(delay):
        assert newest_expected_open_ms(now, TF, delay) == newest, now
        assert next_wake_ms(now, TF, delay) == wake, now
        assert plan_collection(now, TF, delay) == CollectionPlan(now, newest, wake), now


def test_delay_larger_than_the_timeframe_at_the_start_of_the_range() -> None:
    tf = Timeframe("1m")
    first = MIN + 150_000
    assert (newest_expected_open_ms(MIN, tf, 90_000), next_wake_ms(MIN, tf, 90_000)) == (None, first)
    assert (newest_expected_open_ms(first - 1, tf, 90_000), next_wake_ms(first - 1, tf, 90_000)) == (None, first)
    assert (newest_expected_open_ms(first, tf, 90_000), next_wake_ms(first, tf, 90_000)) == (MIN, first + 60_000)


def test_delay_larger_than_the_timeframe_in_the_middle_of_the_range() -> None:
    tf = Timeframe("1m")
    end = MIN + 10 * 60_000
    assert newest_expected_open_ms(end + 90_000, tf, 90_000) == end - 60_000
    assert newest_expected_open_ms(end + 90_000 - 1, tf, 90_000) == end - 120_000


def test_one_day_timeframe_aligns_to_utc_midnight() -> None:
    tf = Timeframe("1d")
    now = MIN + 3 * D_DAY + 20_000
    assert newest_expected_open_ms(now, tf, 15_000) == MIN + 2 * D_DAY
    assert next_wake_ms(now, tf, 15_000) == MIN + 4 * D_DAY + 15_000


def test_a_first_expected_at_beyond_the_maximum_has_no_slot_and_no_plan() -> None:
    tf = Timeframe("1d")
    delay = MAX_TIMESTAMP_MS - MIN
    for now in (MIN, MAX_TIMESTAMP_MS):
        assert newest_expected_open_ms(now, tf, delay) is None
        with pytest.raises(ValueError):
            next_wake_ms(now, tf, delay)
        with pytest.raises(ValueError):
            plan_collection(now, tf, delay)
    assert next_wake_ms(MIN, tf, MAX_TIMESTAMP_MS - MIN - D_DAY) == MAX_TIMESTAMP_MS


def test_next_wake_past_the_maximum_raises() -> None:
    assert newest_expected_open_ms(MAX_TIMESTAMP_MS, TF, 15_000) is not None
    with pytest.raises(ValueError):
        next_wake_ms(MAX_TIMESTAMP_MS, TF, 15_000)
    with pytest.raises(ValueError):
        plan_collection(MAX_TIMESTAMP_MS, TF, 15_000)


_TIME_FUNCTIONS: list[Any] = [expected_at_ms, newest_expected_open_ms, next_wake_ms, plan_collection]


@pytest.mark.parametrize("function", _TIME_FUNCTIONS)
@pytest.mark.parametrize(
    ("time_arg", "timeframe", "delay", "error"),
    [
        (True, TF, 0, TypeError),
        (1.5, TF, 0, TypeError),
        (float(MIN), TF, 0, TypeError),
        (MIN, "5m", 0, TypeError),
        (MIN, TF, True, TypeError),
        (MIN, TF, 1.5, TypeError),
        (MIN - 1, TF, 0, ValueError),
        (MAX_TIMESTAMP_MS + 1, TF, 0, ValueError),
        (MIN, TF, -1, ValueError),
    ],
)
def test_invalid_input_raises(function: Any, time_arg: Any, timeframe: Any, delay: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        function(time_arg, timeframe, delay)


def test_expected_at_adds_the_delay_to_the_slot_end() -> None:
    assert expected_at_ms(MIN, TF, 15_000) == MIN + TF_MS + 15_000


def test_expected_at_past_the_maximum_raises() -> None:
    with pytest.raises(ValueError):
        expected_at_ms(MAX_TIMESTAMP_MS, TF, 0)


def _steady_cases(seed: int) -> list[tuple[Timeframe, int, int]]:
    """(timeframe, delay, now) with ``now`` on or near an ``expected_at`` and a reserve of 6 days at both ends."""
    rng = random.Random(seed)
    cases = []
    for _ in range(300):
        tf = Timeframe(rng.choice(["5m", "1h", "1d"]))
        delay = rng.randint(0, 120_000)
        low = (MIN + 6 * D_DAY) // tf.milliseconds
        high = (MAX_TIMESTAMP_MS - 6 * D_DAY) // tf.milliseconds
        slot_end = rng.randint(low, high) * tf.milliseconds
        offset = rng.choice([-1, 0, 1]) if rng.random() < 2 / 3 else rng.randint(-tf.milliseconds, tf.milliseconds)
        cases.append((tf, delay, slot_end + delay + offset))
    return cases


def test_expected_slot_agrees_with_is_closed_eligible() -> None:
    for tf, delay, now in _steady_cases(13):
        newest = newest_expected_open_ms(now, tf, delay)
        assert newest is not None
        for k in range(-2, 3):
            open_time = newest + k * tf.milliseconds
            assert is_closed_eligible(open_time, tf, delay, now) == (open_time <= newest), (tf, delay, now, k)
        following = newest + tf.milliseconds
        assert not is_closed_eligible(following, tf, delay, now)
        assert is_closed_eligible(following, tf, delay, next_wake_ms(now, tf, delay))


def test_next_wake_is_a_valid_slot_instant_after_now_and_at_most_one_timeframe_away() -> None:
    for tf, delay, now in _steady_cases(14):
        wake = next_wake_ms(now, tf, delay)
        assert wake > now
        slot_open = wake - delay - tf.milliseconds
        assert tf.is_aligned(slot_open)
        assert slot_open >= MIN
        assert wake <= now + tf.milliseconds


def test_before_the_first_expected_at_the_wake_is_the_first_expected_at() -> None:
    rng = random.Random(15)
    for _ in range(300):
        tf = Timeframe(rng.choice(["1m", "5m", "1h", "1d"]))
        delay = rng.choice([0, rng.randint(0, 3 * tf.milliseconds)])
        first = MIN + tf.milliseconds + delay
        now = rng.randint(MIN, first - 1)
        assert newest_expected_open_ms(now, tf, delay) is None
        wake = next_wake_ms(now, tf, delay)
        assert wake == first
        assert wake > now
        assert wake - delay - tf.milliseconds == MIN


@pytest.mark.parametrize("delay", [0, 15_000, 300_000, 450_000])
def test_expected_slot_agrees_with_classify_span_at(delay: int) -> None:
    stored = MIN + 1000 * TF_MS
    slot = stored + TF_MS
    expected_at = slot + TF_MS + delay
    for as_of, not_yet, due in [(expected_at - 1, 1, 0), (expected_at, 0, 1), (expected_at + 1, 0, 1)]:
        coverage = classify_span_at(
            start=slot,
            end=slot + TF_MS,
            timeframe_ms=TF_MS,
            candles=set(),
            conflicts=set(),
            unavailable=set(),
            newest_open_time_ms=stored,
            as_of_ms=as_of,
            rule=ExpectationRule(delay),
        )
        assert coverage.slots == 1
        assert (coverage.not_yet_expected, coverage.due, coverage.stale) == (not_yet, due, 0)
        assert (coverage.present, coverage.conflicted, coverage.unavailable, coverage.missing) == (0, 0, 0, 0)
        newest = newest_expected_open_ms(as_of, TF, delay)
        assert newest is not None
        assert (newest < slot) == (not_yet == 1)
    assert newest_expected_open_ms(expected_at - 1, TF, delay) == stored
    assert newest_expected_open_ms(expected_at, TF, delay) == slot
    assert next_wake_ms(expected_at - 1, TF, delay) == expected_at
    assert next_wake_ms(expected_at, TF, delay) == expected_at + TF_MS
