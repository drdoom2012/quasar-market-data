"""Arithmetic of the funding rate verified span (ADR-0004 P-5, P-7, P-10, P-11).

Funding settlements have no time grid, so a stored row says nothing about its neighbours. Coverage is a
per-series **verified span** ``[from_ms, through_ms]``: a closed interval of integer milliseconds for
which valid responses were received and whose rows are durably stored. This module is the pure
specification of how that interval may change; the single SQL ``merge`` statement of the writer (FR-2)
is the enforcement and must agree with ``merge_span`` at every boundary.

* ``page_coverage``: the interval one valid response verifies. A short page covers the whole request;
  a page of exactly ``limit`` rows covers ``[a, last - 1]`` and the range continues at ``last`` (that row
  is fetched again), which is correct whatever the order of two rows with the same ``fundingTime``.
* ``merge_span``: the union with a covered interval, only when the two touch (overlap or are one
  millisecond apart); otherwise ``NotAdjacentError``.
* ``history_pending``, ``next_history_chunk``: history grows backward from the span until the span is as
  wide as the configured depth, which is at most 30 days (``MAX_BACKFILL_DAYS``): the whole missing depth is
  one range, fetched page by page and merged once. No clock and no tolerance are involved, so the Collector
  and the API agree.

Integer arithmetic only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from quasar_market_data.domain.funding_rate import FUNDING_RATE_PAGE_LIMIT, FundingRate
from quasar_market_data.domain.timestamps import ensure_timestamp_ms

DAY_MS: Final = 86_400_000
BEHIND_THRESHOLD_MS: Final = 7_200_000  # ADR-0004 P-10
MIN_BACKFILL_DAYS: Final = 1
MAX_BACKFILL_DAYS: Final = 30  # ADR-0004 A-4: a larger depth is a configuration error, the process does not start


class NotAdjacentError(ValueError):
    """A covered interval does not touch the verified span, so it must not be merged into it."""


@dataclass(frozen=True)
class Interval:
    """A closed interval of epoch milliseconds."""

    from_ms: int
    through_ms: int

    def __post_init__(self) -> None:
        start = ensure_timestamp_ms(self.from_ms, "from_ms")
        end = ensure_timestamp_ms(self.through_ms, "through_ms")
        if start > end:
            raise ValueError(f"from_ms {start} is after through_ms {end}")


def touches(first: Interval, second: Interval) -> bool:
    """True when the two intervals overlap or one starts exactly one millisecond after the other ends."""
    return first.from_ms <= second.through_ms + 1 and second.from_ms <= first.through_ms + 1


def merge_span(current: Interval | None, covered: Interval) -> Interval:
    """The verified span after a successful unit covering ``covered``; ``NotAdjacentError`` if it does not touch."""
    if current is None:
        return covered
    if not touches(current, covered):
        raise NotAdjacentError(f"{covered} does not touch the verified span {current}")
    return Interval(min(current.from_ms, covered.from_ms), max(current.through_ms, covered.through_ms))


@dataclass(frozen=True)
class PageCoverage:
    covered: Interval
    next_start_ms: int | None


def page_coverage(
    rows: Sequence[FundingRate], *, start_ms: int, end_ms: int, limit: int = FUNDING_RATE_PAGE_LIMIT
) -> PageCoverage:
    """What one valid response to the request ``[start_ms, end_ms]`` verifies (ADR-0004 P-5)."""
    start = ensure_timestamp_ms(start_ms, "start_ms")
    end = ensure_timestamp_ms(end_ms, "end_ms")
    if start > end:
        raise ValueError(f"start_ms {start} is after end_ms {end}")
    if type(limit) is not int:
        raise TypeError(f"limit must be int, got {type(limit).__name__}")
    if not 1 <= limit <= FUNDING_RATE_PAGE_LIMIT:
        raise ValueError(f"limit must be within [1, {FUNDING_RATE_PAGE_LIMIT}], got {limit}")
    if len(rows) > limit:
        raise ValueError(f"{len(rows)} rows is more than limit {limit}")
    previous = start
    for row in rows:
        time = row.key.funding_time_ms
        if not start <= time <= end:
            raise ValueError(f"funding_time_ms {time} is outside [{start}, {end}]")
        if time < previous:
            raise ValueError(f"funding_time_ms {time} is not non-decreasing after {previous}")
        previous = time
    if len(rows) < limit:
        return PageCoverage(covered=Interval(start, end), next_start_ms=None)
    last = rows[-1].key.funding_time_ms
    if last <= start:
        raise ValueError(f"a full page ending at {last} does not advance past the range start {start}")
    return PageCoverage(covered=Interval(start, last - 1), next_start_ms=last)


def history_target_from_ms(through_ms: int, backfill_days: int) -> int:
    """The oldest millisecond the configured depth asks for: ``through_ms - backfill_days * DAY_MS``."""
    through = ensure_timestamp_ms(through_ms, "through_ms")
    if type(backfill_days) is not int:
        raise TypeError(f"backfill_days must be int, got {type(backfill_days).__name__}")
    if not MIN_BACKFILL_DAYS <= backfill_days <= MAX_BACKFILL_DAYS:
        raise ValueError(
            f"backfill_days must be within [{MIN_BACKFILL_DAYS}, {MAX_BACKFILL_DAYS}], got {backfill_days}"
        )
    return through - backfill_days * DAY_MS


def history_pending(span: Interval, backfill_days: int) -> bool:
    """True while the span is narrower than the configured depth (a function of the span and the config only)."""
    return span.from_ms > history_target_from_ms(span.through_ms, backfill_days)


def next_history_chunk(span: Interval, backfill_days: int) -> Interval | None:
    """The backward range ``[target_from, from_ms - 1]`` that completes the configured depth, or ``None`` when not pending.

    It is never longer than ``MAX_BACKFILL_DAYS``. A range that would start below the schema's minimum timestamp
    raises ``ValueError`` (fail closed).
    """
    if not history_pending(span, backfill_days):
        return None
    target = history_target_from_ms(span.through_ms, backfill_days)
    return Interval(target, span.from_ms - 1)


def is_behind(b_now_ms: int, span: Interval) -> bool:
    """True when the span's end lags ``b_now_ms`` by at least two hours (ADR-0004 P-10, boundary inclusive).

    The API's ``behind`` (P-13) must reuse this with ``b_now_ms = as_of - publication_delay_ms`` rather than
    derive it again, so the two agree to the millisecond.
    """
    return ensure_timestamp_ms(b_now_ms, "b_now_ms") - span.through_ms >= BEHIND_THRESHOLD_MS
