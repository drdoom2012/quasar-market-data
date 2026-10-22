"""Closed-candle eligibility and fail-closed selection (ADR-0001 P-3, P-5).

A candle may be treated as closed only when ``end + publication delay <= trusted server lower bound``.
Untrusted time confirms nothing: every candle stays pending and the reason is reported. A candle that is
old enough but fails validation is reported as invalid and is never selected. A pending candle is
retried later and never stored.

Eligibility is not immutability (ADR-0001 P-6): it says the candle may be persisted now, not that the
exchange can never revise it. Detecting a later revision is the writer's job.

All times are epoch milliseconds.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from quasar_market_data.domain.candle import Candle
from quasar_market_data.domain.server_time import TimeRejection, TrustedTime, UntrustedTime
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.validation import (
    CandleValidationError,
    ValidatedCandle,
    Violation,
    require_valid,
)

DEFAULT_PUBLICATION_DELAY_MS: Final = 15_000  # ADR-0001 P-3 initial value


@dataclass(frozen=True)
class InvalidCandle:
    candle: Candle
    violations: tuple[Violation, ...]


@dataclass(frozen=True)
class ClosedCandleSelection:
    eligible: tuple[ValidatedCandle, ...]
    pending: tuple[Candle, ...]
    invalid: tuple[InvalidCandle, ...]
    untrusted_reason: TimeRejection | None


def _ensure_delay(publication_delay_ms: int) -> None:
    if type(publication_delay_ms) is not int:
        raise TypeError(f"publication_delay_ms must be int, got {type(publication_delay_ms).__name__}")
    if publication_delay_ms < 0:
        raise ValueError(f"publication_delay_ms must be >= 0, got {publication_delay_ms}")


def is_closed_eligible(open_time_ms: int, timeframe: Timeframe, publication_delay_ms: int, lower_bound_ms: int) -> bool:
    """True when the candle end plus the publication delay is at or before ``lower_bound_ms``."""
    _ensure_delay(publication_delay_ms)
    if type(lower_bound_ms) is not int:
        raise TypeError(f"lower_bound_ms must be int, got {type(lower_bound_ms).__name__}")
    return timeframe.end_exclusive_ms(open_time_ms) + publication_delay_ms <= lower_bound_ms


def select_closed_candles(
    candles: Sequence[Candle], publication_delay_ms: int, time: TrustedTime | UntrustedTime
) -> ClosedCandleSelection:
    """Split ``candles`` into eligible, pending and invalid groups, each in input order.

    Never drops a candle: the three groups always sum to ``len(candles)``.
    """
    _ensure_delay(publication_delay_ms)
    if isinstance(time, UntrustedTime):
        return ClosedCandleSelection(eligible=(), pending=tuple(candles), invalid=(), untrusted_reason=time.reason)
    if type(time) is not TrustedTime:
        raise TypeError(f"time must be TrustedTime or UntrustedTime, got {type(time).__name__}")

    eligible: list[ValidatedCandle] = []
    pending: list[Candle] = []
    invalid: list[InvalidCandle] = []
    for candle in candles:
        key = candle.key
        if not is_closed_eligible(key.open_time_ms, key.timeframe, publication_delay_ms, time.lower_bound_ms):
            pending.append(candle)
            continue
        try:
            eligible.append(require_valid(candle))
        except CandleValidationError as error:
            invalid.append(InvalidCandle(candle=candle, violations=error.violations))
    return ClosedCandleSelection(
        eligible=tuple(eligible), pending=tuple(pending), invalid=tuple(invalid), untrusted_reason=None
    )
