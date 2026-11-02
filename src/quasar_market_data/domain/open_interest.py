"""Open interest identity, values and eligibility (ADR-0003 P-2, P-3, P-4).

Identity
--------
An open interest row is identified by ``(venue, symbol, period, time_ms)``. ``time_ms`` is the **end** of
the period: the row with ``time_ms = T`` belongs to the period ``[T - period, T)``, i.e. to the candle
whose ``open_time_ms`` is ``T - period``. It must never be joined to a candle ``open_time_ms`` by
equality; that is why the field is not called ``open_time_ms``.

Values
------
``open_interest`` and ``open_interest_value`` are the decimal strings Binance reports, stored as
canonical decimal text (ADR-0001 P-7). They are never computed. A value must be non-negative; any sign
character is rejected before canonicalization, because ``canonical_decimal("-0")`` would hide it.

All times are epoch milliseconds.
"""

from dataclasses import dataclass

from quasar_market_data.domain.candle import ensure_symbol, ensure_venue
from quasar_market_data.domain.decimal_text import InvalidDecimalError, canonical_decimal
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import ensure_timestamp_ms


def _ensure_period(period: Timeframe) -> Timeframe:
    if type(period) is not Timeframe:
        raise TypeError(f"period must be Timeframe, got {type(period).__name__}")
    return period


def _unsigned_decimal(value: str, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be str, got {type(value).__name__}")
    if value.startswith(("+", "-")):
        raise InvalidDecimalError(f"{name} must not carry a sign, got {value!r}")
    return canonical_decimal(value)


@dataclass(frozen=True)
class OpenInterestKey:
    venue: str
    symbol: str
    period: Timeframe
    time_ms: int

    def __post_init__(self) -> None:
        ensure_venue(self.venue)
        ensure_symbol(self.symbol)
        _ensure_period(self.period)
        time_ms = ensure_timestamp_ms(self.time_ms, "time_ms")
        if not self.period.is_aligned(time_ms):
            raise ValueError(f"time_ms {time_ms} is not aligned to period {self.period.label}")


@dataclass(frozen=True)
class OpenInterest:
    key: OpenInterestKey
    open_interest: str
    open_interest_value: str

    def __post_init__(self) -> None:
        if type(self.key) is not OpenInterestKey:
            raise TypeError(f"key must be OpenInterestKey, got {type(self.key).__name__}")
        for name in ("open_interest", "open_interest_value"):
            object.__setattr__(self, name, _unsigned_decimal(getattr(self, name), name))


def period_end_count(start_ms: int, end_ms: int, period: Timeframe) -> int:
    """Number of ``period``-aligned instants in ``[start_ms, end_ms]`` (both bounds inclusive)."""
    start = ensure_timestamp_ms(start_ms, "start_ms")
    end = ensure_timestamp_ms(end_ms, "end_ms")
    step = _ensure_period(period).milliseconds
    if start > end:
        raise ValueError(f"start_ms {start} is after end_ms {end}")
    return end // step - -(-start // step) + 1
