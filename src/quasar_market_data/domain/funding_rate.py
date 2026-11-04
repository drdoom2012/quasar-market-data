"""Funding rate identity, values and the off-hour heuristic (ADR-0004 P-2, P-3).

Identity
--------
A funding rate record is identified by ``(venue, symbol, funding_time_ms, rate_type)``.
``funding_time_ms`` is the settlement time exactly as Binance reports it: it is never rounded to the hour
(that would merge a ``Regular`` and a ``Special`` record of one hour) and it must never be joined to a
candle ``open_time_ms`` by equality; a consumer joins by the hour the settlement falls in.

Values
------
``funding_rate`` is the signed decimal string Binance reports, stored as canonical decimal text
(ADR-0001 P-7: trailing fractional zeros removed, no rounding, zero is ``0`` and never ``-0``). It is a
fraction per settlement; the length of the interval it applies to is not part of the record.
``mark_price`` is canonical non-negative decimal text or ``None``: Binance sends ``""`` for every record
settled before 2023-10-31 and the price is never computed, copied or filled in.

Hour alignment
--------------
``is_off_hour`` reports a settlement more than ``OFF_HOUR_TOLERANCE_MS`` after a whole hour. It is a
heuristic that is only counted, never a rejection criterion (P-3).

All times are epoch milliseconds.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from quasar_market_data.domain.candle import ensure_symbol, ensure_venue
from quasar_market_data.domain.decimal_text import InvalidDecimalError, canonical_decimal
from quasar_market_data.domain.server_time import TimeRejection, TrustedTime, UntrustedTime
from quasar_market_data.domain.timestamps import ensure_timestamp_ms

FUNDING_RATE_PAGE_LIMIT: Final = 1_000  # ADR-0004 P-5
OFF_HOUR_TOLERANCE_MS: Final = 5_000  # ADR-0004 P-3: observed range, never a rejection
HOUR_MS: Final = 3_600_000
# ADR-0004 P-12: ``round_offset_ms`` is at most 600 000 and at least ``publication_delay_ms + 5 000``.
MAX_FUNDING_RATE_PUBLICATION_DELAY_MS: Final = 595_000
# ADR-0004 P-4: a margin from one settlement of three symbols, not a proven bound.
DEFAULT_FUNDING_RATE_PUBLICATION_DELAY_MS: Final = 30_000


class RateType(StrEnum):
    REGULAR = "Regular"
    SPECIAL = "Special"


def _mark_price_text(value: str | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError(f"mark_price must be str or None, got {type(value).__name__}")
    if value.startswith(("+", "-")):
        raise InvalidDecimalError(f"mark_price must not carry a sign, got {value!r}")
    return canonical_decimal(value)


@dataclass(frozen=True)
class FundingRateKey:
    venue: str
    symbol: str
    funding_time_ms: int
    rate_type: RateType

    def __post_init__(self) -> None:
        ensure_venue(self.venue)
        ensure_symbol(self.symbol)
        ensure_timestamp_ms(self.funding_time_ms, "funding_time_ms")
        if type(self.rate_type) is not RateType:
            raise TypeError(f"rate_type must be RateType, got {type(self.rate_type).__name__}")


@dataclass(frozen=True)
class FundingRate:
    key: FundingRateKey
    funding_rate: str
    mark_price: str | None

    def __post_init__(self) -> None:
        if type(self.key) is not FundingRateKey:
            raise TypeError(f"key must be FundingRateKey, got {type(self.key).__name__}")
        object.__setattr__(self, "funding_rate", canonical_decimal(self.funding_rate))
        object.__setattr__(self, "mark_price", _mark_price_text(self.mark_price))


def is_off_hour(funding_time_ms: int) -> bool:
    """True when ``funding_time_ms`` is more than ``OFF_HOUR_TOLERANCE_MS`` after a whole hour."""
    return ensure_timestamp_ms(funding_time_ms, "funding_time_ms") % HOUR_MS > OFF_HOUR_TOLERANCE_MS


def count_off_hour_rows(rows: Sequence[FundingRate]) -> int:
    """How many of ``rows`` are off-hour (the ``off_hour_rows`` figure of the hourly log line)."""
    return sum(1 for row in rows if is_off_hour(row.key.funding_time_ms))


@dataclass(frozen=True)
class FundingRateSelection:
    eligible: tuple[FundingRate, ...]
    pending: tuple[FundingRate, ...]
    untrusted_reason: TimeRejection | None


def _ensure_delay(publication_delay_ms: int) -> None:
    if type(publication_delay_ms) is not int:
        raise TypeError(f"publication_delay_ms must be int, got {type(publication_delay_ms).__name__}")
    if publication_delay_ms < 0:
        raise ValueError(f"publication_delay_ms must be >= 0, got {publication_delay_ms}")


def _ensure_lower_bound(lower_bound_ms: int) -> None:
    if type(lower_bound_ms) is not int:
        raise TypeError(f"lower_bound_ms must be int, got {type(lower_bound_ms).__name__}")


def is_funding_rate_eligible(funding_time_ms: int, publication_delay_ms: int, lower_bound_ms: int) -> bool:
    """True when the settlement time plus the publication delay is at or before ``lower_bound_ms``."""
    time = ensure_timestamp_ms(funding_time_ms, "funding_time_ms")
    _ensure_delay(publication_delay_ms)
    _ensure_lower_bound(lower_bound_ms)
    return time + publication_delay_ms <= lower_bound_ms


def forward_end_bound_ms(publication_delay_ms: int, lower_bound_ms: int) -> int:
    """End of every forward request: the latest settlement time that is eligible (ADR-0004 P-4, P-7)."""
    _ensure_delay(publication_delay_ms)
    _ensure_lower_bound(lower_bound_ms)
    return lower_bound_ms - publication_delay_ms


def select_eligible_funding_rates(
    rows: Sequence[FundingRate], publication_delay_ms: int, time: TrustedTime | UntrustedTime
) -> FundingRateSelection:
    """Split ``rows`` into eligible and pending, each in input order; untrusted time selects nothing.

    Never drops a row: the two groups always sum to ``len(rows)``. An absent row is never inferred.
    """
    _ensure_delay(publication_delay_ms)
    if isinstance(time, UntrustedTime):
        return FundingRateSelection(eligible=(), pending=tuple(rows), untrusted_reason=time.reason)
    if type(time) is not TrustedTime:
        raise TypeError(f"time must be TrustedTime or UntrustedTime, got {type(time).__name__}")
    eligible: list[FundingRate] = []
    pending: list[FundingRate] = []
    for row in rows:
        if is_funding_rate_eligible(row.key.funding_time_ms, publication_delay_ms, time.lower_bound_ms):
            eligible.append(row)
        else:
            pending.append(row)
    return FundingRateSelection(eligible=tuple(eligible), pending=tuple(pending), untrusted_reason=None)
