"""Freshness of one series from its newest stored open time (S17).

The rule is S11's, applied to the oldest slot after the newest stored one: that slot has the earliest
``expected_at`` (``slot end + publication delay``), so it is the most overdue and its state is the worst of all
the missing slots after the newest candle. With ``x`` its ``expected_at``, ``L`` the lag threshold and ``tf``
the timeframe:

* ``as_of < x`` -> ``current`` (nothing after the newest stored slot is expected yet; also true for a clock
  behind the stored data), ``lag_slots = 0``;
* ``x <= as_of <= x + L`` -> ``due``; ``as_of > x + L`` -> ``stale``;
* ``lag_slots = 1 + (as_of - x) // tf``, the number of expected slots after the newest stored one.

So a ``due`` series has ``1 <= lag_slots <= 1 + L // tf`` (exactly 1 only when ``L < tf``) and a ``stale`` one
``lag_slots >= 1``. Only the newest stored candle is read: holes behind it are not freshness. The domain reads
no clock; ``as_of_ms`` is an input.
"""

from dataclasses import dataclass
from enum import StrEnum

from quasar_market_data.domain.schedule import expected_at_ms, newest_expected_open_ms
from quasar_market_data.domain.timed_coverage import ExpectationRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import ensure_timestamp_ms


class Freshness(StrEnum):
    CURRENT = "current"
    DUE = "due"
    STALE = "stale"


@dataclass(frozen=True)
class SeriesFreshness:
    state: Freshness
    lag_slots: int


def series_freshness(
    newest_open_time_ms: int, as_of_ms: int, timeframe: Timeframe, rule: ExpectationRule
) -> SeriesFreshness:
    """Judge a series whose newest stored candle opens at ``newest_open_time_ms`` at ``as_of_ms``.

    Raises ``TypeError`` or ``ValueError`` for wrong types, out-of-range times or an unaligned open time.
    """
    if not isinstance(timeframe, Timeframe):
        raise TypeError(f"timeframe must be Timeframe, got {type(timeframe).__name__}")
    if not isinstance(rule, ExpectationRule):
        raise TypeError(f"rule must be ExpectationRule, got {type(rule).__name__}")
    ensure_timestamp_ms(newest_open_time_ms, "newest_open_time_ms")
    ensure_timestamp_ms(as_of_ms, "as_of_ms")
    if not timeframe.is_aligned(newest_open_time_ms):
        raise ValueError(f"newest_open_time_ms {newest_open_time_ms} is not aligned to the timeframe")

    newest_expected = newest_expected_open_ms(as_of_ms, timeframe, rule.publication_delay_ms)
    if newest_expected is None or newest_open_time_ms >= newest_expected:
        return SeriesFreshness(Freshness.CURRENT, 0)
    x = expected_at_ms(newest_open_time_ms + timeframe.milliseconds, timeframe, rule.publication_delay_ms)
    lag_slots = (newest_expected - newest_open_time_ms) // timeframe.milliseconds
    state = Freshness.DUE if as_of_ms <= x + rule.lag_threshold_ms else Freshness.STALE
    return SeriesFreshness(state, lag_slots)
