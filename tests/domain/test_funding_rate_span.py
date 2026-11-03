import random
from collections.abc import Sequence

import pytest

from quasar_market_data.domain.funding_rate import FUNDING_RATE_PAGE_LIMIT, FundingRate, FundingRateKey, RateType
from quasar_market_data.domain.funding_rate_span import (
    BEHIND_THRESHOLD_MS,
    DAY_MS,
    HISTORY_CHUNK_MS,
    Interval,
    NotAdjacentError,
    history_pending,
    history_target_from_ms,
    is_behind,
    merge_span,
    next_history_chunk,
    page_coverage,
    touches,
)
from quasar_market_data.domain.timestamps import MIN_TIMESTAMP_MS
from tests.typing_helpers import untyped

X = 1_793_000_000_000
HOUR = 3_600_000


def row(time_ms: int, rate_type: RateType = RateType.REGULAR) -> FundingRate:
    return FundingRate(FundingRateKey("binance-usdm", "BTC/USDT:USDT", time_ms, rate_type), "0.0001", None)


def rows_at(times: Sequence[int]) -> list[FundingRate]:
    return [row(t) for t in times]


def test_interval_validation() -> None:
    assert Interval(X, X).from_ms == X
    with pytest.raises(ValueError):
        Interval(X + 1, X)
    with pytest.raises(TypeError):
        Interval(untyped(True), X)
    with pytest.raises(ValueError):
        Interval(MIN_TIMESTAMP_MS - 1, X)


def test_touches() -> None:
    assert touches(Interval(X, X + 10), Interval(X + 11, X + 20)) is True
    assert touches(Interval(X + 11, X + 20), Interval(X, X + 10)) is True
    assert touches(Interval(X, X + 10), Interval(X + 12, X + 20)) is False
    assert touches(Interval(X, X + 10), Interval(X + 5, X + 6)) is True


def test_merge_span_boundaries() -> None:
    span = Interval(X, X + 1_000_000)
    assert merge_span(None, span) == span
    assert merge_span(span, Interval(span.through_ms + 1, span.through_ms + 50)) == Interval(X, X + 1_000_050)
    with pytest.raises(NotAdjacentError):
        merge_span(span, Interval(span.through_ms + 2, span.through_ms + 50))
    assert merge_span(span, Interval(X - 50, X - 1)) == Interval(X - 50, X + 1_000_000)
    with pytest.raises(NotAdjacentError):
        merge_span(span, Interval(X - 50, X - 2))
    assert merge_span(span, Interval(X + 10, X + 20)) == span
    assert merge_span(span, Interval(X - 5, X + 2_000_000)) == Interval(X - 5, X + 2_000_000)
    assert merge_span(span, Interval(X + 500_000, X + 1_500_000)) == Interval(X, X + 1_500_000)
    assert issubclass(NotAdjacentError, ValueError)


def test_page_coverage_short_page() -> None:
    start, end = X, X + 500_000_000
    short = page_coverage(rows_at([X + i * 1_000 for i in range(999)]), start_ms=start, end_ms=end)
    assert short.covered == Interval(start, end)
    assert short.next_start_ms is None
    empty = page_coverage([], start_ms=start, end_ms=end)
    assert empty.covered == Interval(start, end)
    assert empty.next_start_ms is None


def test_page_coverage_full_page_continues_at_the_last_row() -> None:
    times = [X + i * 1_000 for i in range(FUNDING_RATE_PAGE_LIMIT)]
    start, end = X, X + 5_000_000
    first = page_coverage(rows_at(times), start_ms=start, end_ms=end)
    assert first.covered == Interval(start, times[-1] - 1)
    assert first.next_start_ms == times[-1]
    second = page_coverage(rows_at([times[-1]]), start_ms=times[-1], end_ms=end)
    assert second.covered == Interval(times[-1], end)
    assert second.next_start_ms is None
    assert merge_span(first.covered, second.covered) == Interval(start, end)


@pytest.mark.parametrize("order", [(RateType.REGULAR, RateType.SPECIAL), (RateType.SPECIAL, RateType.REGULAR)])
def test_full_page_with_a_shared_final_fundingtime_skips_neither_row(order: tuple[RateType, RateType]) -> None:
    times = [X + i * 1_000 for i in range(FUNDING_RATE_PAGE_LIMIT - 2)]
    last = times[-1] + 1_000
    page = [*rows_at(times), row(last, order[0]), row(last, order[1])]
    assert len(page) == FUNDING_RATE_PAGE_LIMIT
    assert page[-1].key.funding_time_ms == page[-2].key.funding_time_ms == last
    coverage = page_coverage(page, start_ms=X, end_ms=X + 5_000_000)
    assert coverage.covered == Interval(X, last - 1)
    assert coverage.next_start_ms == last


def test_empty_answer_extends_the_span() -> None:
    span = Interval(X, X + 5 * HOUR)
    end = span.through_ms + 3 * HOUR
    extended = merge_span(span, page_coverage([], start_ms=span.through_ms + 1, end_ms=end).covered)
    assert extended == Interval(X, end)


def test_two_thousand_five_hundred_rows_need_three_consecutive_pages() -> None:
    times = [X + i * 1_000 for i in range(2_500)]
    start, end = X, times[-1] + 7
    requested: list[int] = []
    returned: list[int] = []
    span: Interval | None = None
    current = start
    while True:
        requested.append(current)
        page = [t for t in times if current <= t <= end][:FUNDING_RATE_PAGE_LIMIT]
        returned.extend(page)
        coverage = page_coverage(rows_at(page), start_ms=current, end_ms=end)
        span = merge_span(span, coverage.covered)
        if coverage.next_start_ms is None:
            break
        current = coverage.next_start_ms
    assert len(requested) == 3
    assert requested == [start, times[999], times[1998]]
    assert span == Interval(start, end)
    assert set(returned) == set(times)
    assert sorted(t for t in set(returned) if returned.count(t) == 2) == [times[999], times[1998]]
    assert len(returned) == 2_500 + 2


def test_page_coverage_rejects_inconsistent_input() -> None:
    start, end = X, X + 1_000_000
    with pytest.raises(ValueError, match="more than"):
        page_coverage(rows_at([X, X + 1, X + 2]), start_ms=start, end_ms=end, limit=2)
    with pytest.raises(ValueError, match="outside"):
        page_coverage(rows_at([X - 1]), start_ms=start, end_ms=end)
    with pytest.raises(ValueError, match="outside"):
        page_coverage(rows_at([end + 1]), start_ms=start, end_ms=end)
    with pytest.raises(ValueError, match="non-decreasing"):
        page_coverage(rows_at([X + 5, X + 4]), start_ms=start, end_ms=end)
    with pytest.raises(ValueError, match="does not advance"):
        page_coverage([row(X, RateType.REGULAR), row(X, RateType.SPECIAL)], start_ms=start, end_ms=end, limit=2)
    for bad_limit in (0, 1_001):
        with pytest.raises(ValueError, match="limit"):
            page_coverage([], start_ms=start, end_ms=end, limit=bad_limit)
    with pytest.raises(ValueError):
        page_coverage([], start_ms=end, end_ms=start)
    with pytest.raises(TypeError):
        page_coverage([], start_ms=start, end_ms=end, limit=untyped(True))
    with pytest.raises(TypeError):
        page_coverage([], start_ms=untyped(True), end_ms=end)
    with pytest.raises(TypeError):
        page_coverage([], start_ms=start, end_ms=untyped(True))


def test_history_pending_boundary_has_no_tolerance() -> None:
    days = 365
    width = days * DAY_MS
    assert history_pending(Interval(X - width, X), days) is False
    assert history_pending(Interval(X - width + 1, X), days) is True
    assert history_pending(Interval(X - width + DAY_MS, X), days) is True
    assert history_pending(Interval(X - width - 1, X), days) is False


def test_moving_target_does_not_reopen_finished_history() -> None:
    days = 365
    span = Interval(X - 300 * DAY_MS, X)
    assert history_pending(span, days) is True
    chunk = next_history_chunk(span, days)
    assert chunk is not None
    advanced = merge_span(span, Interval(X + 1, X + HOUR))
    assert history_pending(advanced, days) is True
    finished = merge_span(advanced, chunk)
    assert history_pending(finished, days) is False
    for _ in range(24):
        finished = merge_span(finished, Interval(finished.through_ms + 1, finished.through_ms + HOUR))
        assert history_pending(finished, days) is False


@pytest.mark.parametrize("mode", ["raise", "lower"])
@pytest.mark.parametrize("seed", range(6))
def test_history_pending_property(mode: str, seed: int) -> None:
    rng = random.Random(seed)
    span = Interval(X - DAY_MS, X)
    days = 365
    for _ in range(200):
        previous_span, previous_days = span, days
        previous_pending = history_pending(span, days)
        action = rng.choice(["chunk", "crash", "restart", "round", "days"])
        if action == "chunk":
            chunk = next_history_chunk(span, days)
            if chunk is not None:
                span = merge_span(span, chunk)
        elif action == "round":
            span = merge_span(span, Interval(span.through_ms + 1, span.through_ms + HOUR))
        elif action == "days":
            if mode == "raise":
                days = min(3_650, days + rng.randint(0, 400))
            else:
                days = max(1, days - rng.randint(0, 200))
        # "crash" (a planned chunk is never merged) and "restart" (state recomputed from the span only) change nothing.
        now_pending = history_pending(span, days)
        if not previous_pending and now_pending:
            assert days > previous_days
        assert span.from_ms <= previous_span.from_ms
        assert span.through_ms >= previous_span.through_ms
        chunk = next_history_chunk(span, days)
        assert (chunk is not None) == now_pending
        if chunk is not None:
            assert chunk.from_ms >= history_target_from_ms(span.through_ms, days)


@pytest.mark.parametrize(
    ("days", "seed_width", "steps"),
    [(365, 0, 5), (365, DAY_MS, 5), (90, 0, 1), (90, DAY_MS, 1), (91, 0, 2), (91, DAY_MS, 1)],
)
def test_next_history_chunk(days: int, seed_width: int, steps: int) -> None:
    span = Interval(X - seed_width, X)
    done = 0
    while history_pending(span, days):
        chunk = next_history_chunk(span, days)
        assert chunk is not None
        assert chunk.through_ms == span.from_ms - 1
        target = history_target_from_ms(span.through_ms, days)
        remaining = span.from_ms - target
        if remaining > HISTORY_CHUNK_MS:
            assert chunk.through_ms - chunk.from_ms + 1 == HISTORY_CHUNK_MS
        else:
            assert chunk.from_ms == target
        span = merge_span(span, chunk)
        done += 1
    assert done == steps
    assert span.through_ms - span.from_ms == days * DAY_MS
    assert next_history_chunk(span, days) is None


def test_chunk_near_the_minimum_timestamp_fails_closed() -> None:
    at_minimum = Interval(MIN_TIMESTAMP_MS, MIN_TIMESTAMP_MS + 10)
    assert history_pending(at_minimum, 365) is True
    with pytest.raises(ValueError):
        next_history_chunk(at_minimum, 365)
    below = Interval(MIN_TIMESTAMP_MS + DAY_MS, MIN_TIMESTAMP_MS + 2 * DAY_MS)
    with pytest.raises(ValueError):
        next_history_chunk(below, 365)


def test_is_behind() -> None:
    span = Interval(X - DAY_MS, X)
    assert BEHIND_THRESHOLD_MS == 7_200_000
    assert is_behind(X + BEHIND_THRESHOLD_MS - 1, span) is False
    assert is_behind(X + BEHIND_THRESHOLD_MS, span) is True
    with pytest.raises(TypeError):
        is_behind(untyped(True), span)


def test_backfill_days_bounds() -> None:
    assert history_target_from_ms(X, 1) == X - DAY_MS
    assert history_target_from_ms(X, 3_650) == X - 3_650 * DAY_MS
    for bad in (0, 3_651):
        with pytest.raises(ValueError):
            history_target_from_ms(X, bad)
    for wrong in (True, 365.0):
        with pytest.raises(TypeError):
            history_target_from_ms(X, untyped(wrong))
    with pytest.raises(ValueError):
        history_target_from_ms(MIN_TIMESTAMP_MS - 1, 365)
