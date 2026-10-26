"""UTC boundary and publication-delay planner (S13).

A slot is expected at ``expected_at = slot end + publication delay``, the same instant as in
``is_closed_eligible`` (ADR-0001 P-3) and in ``timed_coverage``. Valid slots are the epoch-aligned open times
from ``MIN_TIMESTAMP_MS`` on; every supported timeframe divides one UTC day, so ``MIN_TIMESTAMP_MS`` is the first
one. Before the first ``expected_at`` there is no expected slot and the next wake is that first ``expected_at``.

The planner only says when to look. It does not decide that a candle is closed: that stays with
``select_closed_candles`` against the trusted server lower bound, which deliberately lags the wall clock, so
a look at a fresh slot can still find it pending. A pending candle needs its own bounded retry, which is not
planned here (S14); the next boundary wake does not replace it.

The domain never reads a clock: ``now_ms`` is an epoch-millisecond ``int`` supplied by the caller.
"""

from dataclasses import dataclass

from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import MIN_TIMESTAMP_MS, ensure_timestamp_ms


def _check(timeframe: Timeframe, publication_delay_ms: int) -> None:
    if not isinstance(timeframe, Timeframe):
        raise TypeError(f"timeframe must be Timeframe, got {type(timeframe).__name__}")
    if type(publication_delay_ms) is not int:
        raise TypeError(f"publication_delay_ms must be int, got {type(publication_delay_ms).__name__}")
    if publication_delay_ms < 0:
        raise ValueError(f"publication_delay_ms must be >= 0, got {publication_delay_ms}")


def expected_at_ms(open_time_ms: int, timeframe: Timeframe, publication_delay_ms: int) -> int:
    """The instant the slot opening at ``open_time_ms`` is expected: its end plus the publication delay."""
    _check(timeframe, publication_delay_ms)
    return ensure_timestamp_ms(timeframe.end_exclusive_ms(open_time_ms) + publication_delay_ms, "expected_at_ms")


def _newest_and_wake(now_ms: int, timeframe: Timeframe, publication_delay_ms: int) -> tuple[int | None, int]:
    """The newest expected open time (or ``None``) and the unchecked next wake."""
    _check(timeframe, publication_delay_ms)
    ensure_timestamp_ms(now_ms, "now_ms")
    tf = timeframe.milliseconds
    first_expected_at = MIN_TIMESTAMP_MS + tf + publication_delay_ms
    if now_ms < first_expected_at:
        return None, first_expected_at
    slot_end = (now_ms - publication_delay_ms) // tf * tf
    return slot_end - tf, slot_end + tf + publication_delay_ms


def newest_expected_open_ms(now_ms: int, timeframe: Timeframe, publication_delay_ms: int) -> int | None:
    """The largest valid open time with ``expected_at <= now_ms``, or ``None`` when no slot is expected yet."""
    return _newest_and_wake(now_ms, timeframe, publication_delay_ms)[0]


def next_wake_ms(now_ms: int, timeframe: Timeframe, publication_delay_ms: int) -> int:
    """The smallest ``expected_at`` of a valid slot strictly greater than ``now_ms``.

    Raises ``ValueError`` when it would pass ``MAX_TIMESTAMP_MS``.
    """
    return ensure_timestamp_ms(_newest_and_wake(now_ms, timeframe, publication_delay_ms)[1], "next_wake_ms")


@dataclass(frozen=True)
class CollectionPlan:
    now_ms: int
    newest_expected_open_ms: int | None
    next_wake_ms: int


def plan_collection(now_ms: int, timeframe: Timeframe, publication_delay_ms: int) -> CollectionPlan:
    """Both answers at once; fails closed as a whole when ``next_wake_ms`` does."""
    newest, wake = _newest_and_wake(now_ms, timeframe, publication_delay_ms)
    return CollectionPlan(now_ms, newest, ensure_timestamp_ms(wake, "next_wake_ms"))
