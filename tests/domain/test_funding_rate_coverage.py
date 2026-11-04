import random

import pytest

from quasar_market_data.domain.funding_rate_coverage import (
    MAX_FUNDING_WINDOW_MS,
    QUIET_AFTER_MS,
    FundingSeriesHealth,
    WindowState,
    behind_after_ms,
    defaulted_window_end,
    funding_series_health,
    is_complete,
    window_state,
)
from quasar_market_data.domain.funding_rate_span import BEHIND_THRESHOLD_MS, DAY_MS, Interval, is_behind
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, MIN_TIMESTAMP_MS
from tests.typing_helpers import untyped

F = 1_793_000_000_000
T = F + 10 * DAY_MS
SPAN = Interval(F, T)


def test_the_window_edges_of_the_verified_span() -> None:
    assert window_state(SPAN, F, T) is WindowState.VERIFIED
    assert window_state(SPAN, F - 1, T) is WindowState.PARTLY_VERIFIED
    assert window_state(SPAN, F, T + 1) is WindowState.PARTLY_VERIFIED
    assert window_state(SPAN, F + 5, F + 5) is WindowState.VERIFIED  # one millisecond inside
    assert window_state(SPAN, F - DAY_MS, T + DAY_MS) is WindowState.PARTLY_VERIFIED  # contains the whole span


def test_disjoint_adjacent_and_missing_spans_are_unverified() -> None:
    assert window_state(SPAN, F - DAY_MS, F - 1) is WindowState.UNVERIFIED  # ends one ms before the span
    assert window_state(SPAN, T + 1, T + DAY_MS) is WindowState.UNVERIFIED  # starts one ms after it
    assert window_state(SPAN, F - DAY_MS, F) is WindowState.PARTLY_VERIFIED  # shares exactly one millisecond
    assert window_state(SPAN, T, T + DAY_MS) is WindowState.PARTLY_VERIFIED
    assert window_state(None, F, T) is WindowState.UNVERIFIED


def test_window_state_matches_a_millisecond_model_on_small_spans() -> None:
    rng = random.Random(7)
    for _ in range(500):
        a = rng.randrange(F, F + 20)
        span = Interval(a, a + rng.randrange(0, 10))
        start = rng.randrange(F - 5, F + 30)
        end = start + rng.randrange(0, 15)
        window = set(range(start, end + 1))
        covered = set(range(span.from_ms, span.through_ms + 1))
        expected = (
            WindowState.VERIFIED
            if window <= covered
            else WindowState.PARTLY_VERIFIED
            if window & covered
            else WindowState.UNVERIFIED
        )
        assert window_state(span, start, end) is expected, (span, start, end)


@pytest.mark.parametrize("bad", [True, 1.0, "1", None])
def test_window_state_refuses_non_int_bounds(bad: object) -> None:
    with pytest.raises(TypeError):
        window_state(SPAN, untyped(bad), T)
    with pytest.raises(TypeError):
        window_state(SPAN, F, untyped(bad))


def test_window_state_refuses_an_end_before_the_start_and_out_of_range_times() -> None:
    with pytest.raises(ValueError):
        window_state(SPAN, F + 1, F)
    with pytest.raises(ValueError):
        window_state(SPAN, MIN_TIMESTAMP_MS - 1, F)
    with pytest.raises(ValueError):
        window_state(SPAN, F, MAX_TIMESTAMP_MS + 1)


def test_complete_needs_a_verified_window_and_no_conflicted_event() -> None:
    assert is_complete(WindowState.VERIFIED, 0) is True
    assert is_complete(WindowState.VERIFIED, 1) is False
    assert is_complete(WindowState.PARTLY_VERIFIED, 0) is False
    assert is_complete(WindowState.UNVERIFIED, 0) is False
    with pytest.raises(ValueError):
        is_complete(WindowState.VERIFIED, -1)
    with pytest.raises(TypeError):
        is_complete(WindowState.VERIFIED, untyped(True))


def test_a_defaulted_end_is_now_or_the_start_capped_at_the_maximum_window() -> None:
    assert defaulted_window_end(F, F + 5 * DAY_MS) == F + 5 * DAY_MS
    assert defaulted_window_end(F + DAY_MS, F) == F + DAY_MS  # start after now: a one-millisecond window
    assert defaulted_window_end(F, F + MAX_FUNDING_WINDOW_MS) == F + MAX_FUNDING_WINDOW_MS
    assert defaulted_window_end(F, F + MAX_FUNDING_WINDOW_MS + 1) == F + MAX_FUNDING_WINDOW_MS  # capped, echoed
    assert MAX_FUNDING_WINDOW_MS == 365 * DAY_MS == 31_536_000_000


def test_a_cap_beyond_the_valid_time_range_is_clamped_to_it() -> None:
    start = MAX_TIMESTAMP_MS - DAY_MS
    assert defaulted_window_end(start, MAX_TIMESTAMP_MS) == MAX_TIMESTAMP_MS


def test_behind_after_ms_is_the_delay_plus_two_hours() -> None:
    assert behind_after_ms(30_000) == 7_230_000
    assert behind_after_ms(30_000) == 30_000 + BEHIND_THRESHOLD_MS
    with pytest.raises(ValueError):
        behind_after_ms(-1)


def health(
    span: Interval = SPAN,
    newest: int | None = T,
    as_of: int = T + 60_000,
    delay: int = 30_000,
    backfill_days: int = 10,
) -> FundingSeriesHealth:
    return funding_series_health(span, newest, as_of, delay, backfill_days)


def test_behind_at_the_exact_inclusive_edge() -> None:
    edge = T + behind_after_ms(30_000)
    assert health(as_of=edge).behind is True
    assert health(as_of=edge - 1).behind is False


def test_behind_is_the_collectors_function_at_every_edge() -> None:
    rng = random.Random(11)
    for _ in range(300):
        through = F + rng.randrange(0, 1_000_000)
        span = Interval(F, through)
        delay = rng.choice([5_000, 30_000, 120_000])
        as_of = through + delay + BEHIND_THRESHOLD_MS + rng.randrange(-3, 4)
        assert health(span, None, as_of, delay).behind is is_behind(as_of - delay, span)


def test_catching_up_at_the_exact_width_and_with_no_clock() -> None:
    width = 10 * DAY_MS
    full = Interval(T - width, T)
    assert health(full, T).catching_up is False
    assert health(Interval(T - width + 1, T), T).catching_up is True  # one millisecond below
    early, late = health(full, T, as_of=T + 1), health(full, T, as_of=T + 10 * DAY_MS)
    assert early.catching_up == late.catching_up  # the clock does not move it


def test_quiet_at_the_exact_strict_edge_and_without_a_row() -> None:
    assert QUIET_AFTER_MS == 172_800_000
    newest = T - 100
    assert health(newest=newest, as_of=newest + QUIET_AFTER_MS).quiet is False
    assert health(newest=newest, as_of=newest + QUIET_AFTER_MS + 1).quiet is True
    assert health(newest=None).quiet is True


def test_health_fails_closed_on_a_delay_that_pushes_the_bound_below_the_minimum() -> None:
    span = Interval(MIN_TIMESTAMP_MS, MIN_TIMESTAMP_MS + 1)
    with pytest.raises(ValueError):
        funding_series_health(span, None, MIN_TIMESTAMP_MS + 10, 30_000, 1)  # as_of - delay is below the minimum


def test_health_refuses_bad_types() -> None:
    with pytest.raises(TypeError):
        funding_series_health(SPAN, T, untyped(1.5), 30_000, 10)
    with pytest.raises(TypeError):
        funding_series_health(SPAN, untyped("1"), T + 1, 30_000, 10)
    with pytest.raises(TypeError):
        funding_series_health(SPAN, T, T + 1, untyped(True), 10)
