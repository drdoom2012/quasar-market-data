from typing import Any

import pytest

from quasar_market_data.domain.freshness import Freshness, SeriesFreshness, series_freshness
from quasar_market_data.domain.schedule import expected_at_ms, newest_expected_open_ms
from quasar_market_data.domain.timed_coverage import ExpectationRule, classify_span_at
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, MIN_TIMESTAMP_MS

TF = Timeframe("5m")
TF_MS = 300_000
DELAY = 15_000
LAG = 60_000
RULE = ExpectationRule(DELAY, LAG)
N = TF.align_down(1_792_677_000_000)  # the newest stored open time in most tests
X = expected_at_ms(N + TF_MS, TF, DELAY)  # the instant the slot after N is expected


def judge(as_of: int, rule: ExpectationRule = RULE, tf: Timeframe = TF, n: int = N) -> SeriesFreshness:
    return series_freshness(n, as_of, tf, rule)


def test_current_when_newest_is_the_newest_expected() -> None:
    assert judge(X - 1) == SeriesFreshness(Freshness.CURRENT, 0)


def test_one_slot_behind_is_due_until_the_lag_threshold_inclusive() -> None:
    assert judge(X) == SeriesFreshness(Freshness.DUE, 1)
    assert judge(X + LAG) == SeriesFreshness(Freshness.DUE, 1)


def test_stale_one_millisecond_after_the_threshold() -> None:
    assert judge(X + LAG + 1) == SeriesFreshness(Freshness.STALE, 1)


def test_stored_ahead_of_the_clock_is_current_with_zero_lag() -> None:
    assert judge(N - 10 * TF_MS) == SeriesFreshness(Freshness.CURRENT, 0)
    assert judge(MIN_TIMESTAMP_MS) == SeriesFreshness(Freshness.CURRENT, 0)


def test_lag_slots_counts_whole_missing_slots() -> None:
    assert judge(X + 2 * TF_MS) == SeriesFreshness(Freshness.STALE, 3)


def test_before_the_first_expected_slot_is_current() -> None:
    first = MIN_TIMESTAMP_MS
    assert newest_expected_open_ms(first, TF, DELAY) is None
    assert judge(first, n=first) == SeriesFreshness(Freshness.CURRENT, 0)


@pytest.mark.parametrize(
    ("tf_label", "lag", "max_due_slots"),
    [("5m", 60_000, 1), ("5m", 299_999, 1), ("5m", 300_000, 2), ("5m", 900_000, 4), ("1m", 900_000, 16)],
)
def test_due_bounds_lag_slots_by_one_plus_lag_over_timeframe(tf_label: str, lag: int, max_due_slots: int) -> None:
    tf = Timeframe(tf_label)
    rule = ExpectationRule(DELAY, lag)
    n = tf.align_down(N)
    x = expected_at_ms(n + tf.milliseconds, tf, DELAY)
    assert max_due_slots == 1 + lag // tf.milliseconds
    seen: set[int] = set()
    for as_of in range(x, x + lag + 1, 1_000):
        result = series_freshness(n, as_of, tf, rule)
        assert result.state is Freshness.DUE
        assert 1 <= result.lag_slots <= max_due_slots
        seen.add(result.lag_slots)
    assert max(seen) == max_due_slots  # the bound is reached, not only respected (as_of = x + lag is in the sweep)
    assert series_freshness(n, x + lag, tf, rule) == SeriesFreshness(Freshness.DUE, max_due_slots)


def test_lag_slots_equals_one_plus_elapsed_slots_since_x() -> None:
    for as_of in range(X, X + 6 * TF_MS, 7_000):
        assert judge(as_of).lag_slots == 1 + (as_of - X) // TF_MS


@pytest.mark.parametrize("tf_label", ["1m", "5m", "1h"])
@pytest.mark.parametrize("delay", [0, 15_000, 300_000])
@pytest.mark.parametrize("lag", [0, 60_000, 300_000, 900_000])
def test_agrees_with_classify_span_at_over_all_slots_after_newest(tf_label: str, delay: int, lag: int) -> None:
    tf = Timeframe(tf_label)
    rule = ExpectationRule(delay, lag)
    n = tf.align_down(N)
    x = expected_at_ms(n + tf.milliseconds, tf, delay)
    offsets = {-1, 0, 1, lag - 1, lag, lag + 1, lag + tf.milliseconds, 2 * tf.milliseconds + 3}
    for off in sorted(offsets):
        as_of = x + off
        e = newest_expected_open_ms(as_of, tf, delay)
        assert e is not None
        end = max(e, n) + 2 * tf.milliseconds  # all expected slots plus the first not yet expected one
        coverage = classify_span_at(
            start=n + tf.milliseconds,
            end=end,
            timeframe_ms=tf.milliseconds,
            candles=set(),
            conflicts=set(),
            unavailable=set(),
            newest_open_time_ms=n,
            as_of_ms=as_of,
            rule=rule,
        )
        result = series_freshness(n, as_of, tf, rule)
        if coverage.stale > 0:
            assert result.state is Freshness.STALE
        elif coverage.due > 0:
            assert result.state is Freshness.DUE
        else:
            assert result.state is Freshness.CURRENT
        assert result.lag_slots == coverage.due + coverage.stale


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ((1.5, 1, TF, RULE), TypeError),
        ((N, "1", TF, RULE), TypeError),
        ((N, 1, "5m", RULE), TypeError),
        ((N, 1, TF, (1, 2)), TypeError),
        ((MIN_TIMESTAMP_MS - 1, X, TF, RULE), ValueError),
        ((N, MAX_TIMESTAMP_MS + 1, TF, RULE), ValueError),
        ((N + 1, X, TF, RULE), ValueError),  # not aligned to the timeframe
    ],
)
def test_rejects_non_int_unaligned_and_out_of_range_inputs(args: tuple[Any, ...], error: type[Exception]) -> None:
    with pytest.raises(error):
        series_freshness(*args)
