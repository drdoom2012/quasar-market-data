"""Fail-closed candle validation.

Construction vs validation
--------------------------
Constructing a ``Candle`` checks **structure only** (see ``quasar_market_data.domain.candle``): a candle
with ``high < low`` or non-positive prices constructs without error. Content rules live here, and
``require_valid()`` is the **only** way to obtain a ``ValidatedCandle``. ``ValidatedCandle`` is a
type-level marker so a type checker can enforce that only validated candles are persisted; it is not a
runtime guard and does not prevent a deliberate cast.

Rules
-----
All rules are independent and all violations are reported (overlapping defects yield several
violations). Equality is allowed; flat zero-volume candles are valid. Comparisons use exact ``Decimal``
values obtained through ``to_decimal``; nothing is rounded or approximated.

Violations are reported in this deterministic order:

1. ``NON_POSITIVE_PRICE`` for ``open``, ``high``, ``low``, ``close`` (in that field order): price ``> 0``
2. ``HIGH_BELOW_OPEN``: ``high >= open``
3. ``HIGH_BELOW_CLOSE``: ``high >= close``
4. ``HIGH_BELOW_LOW``: ``high >= low``
5. ``LOW_ABOVE_OPEN``: ``low <= open``
6. ``LOW_ABOVE_CLOSE``: ``low <= close``
7. ``NEGATIVE_VOLUME``: ``volume >= 0``
8. ``NEGATIVE_TURNOVER``: ``turnover is None or turnover >= 0``
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import NewType

from quasar_market_data.domain.candle import Candle
from quasar_market_data.domain.decimal_text import to_decimal


class ViolationCode(StrEnum):
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    HIGH_BELOW_OPEN = "HIGH_BELOW_OPEN"
    HIGH_BELOW_CLOSE = "HIGH_BELOW_CLOSE"
    HIGH_BELOW_LOW = "HIGH_BELOW_LOW"
    LOW_ABOVE_OPEN = "LOW_ABOVE_OPEN"
    LOW_ABOVE_CLOSE = "LOW_ABOVE_CLOSE"
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
    NEGATIVE_TURNOVER = "NEGATIVE_TURNOVER"


@dataclass(frozen=True)
class Violation:
    code: ViolationCode
    field: str
    detail: str


ValidatedCandle = NewType("ValidatedCandle", Candle)


class CandleValidationError(ValueError):
    """Raised by ``require_valid`` when a candle breaks one or more validation rules."""

    def __init__(self, violations: tuple[Violation, ...]) -> None:
        self.violations = violations
        summary = "; ".join(f"{v.code}({v.field}): {v.detail}" for v in violations)
        super().__init__(f"candle failed validation: {summary}")


def validate_candle(candle: Candle) -> tuple[Violation, ...]:
    """Return every rule violation of ``candle`` in the documented order; ``()`` means valid."""
    open_, high, low, close = (
        to_decimal(candle.open),
        to_decimal(candle.high),
        to_decimal(candle.low),
        to_decimal(candle.close),
    )
    volume = to_decimal(candle.volume)
    violations: list[Violation] = []

    def add(code: ViolationCode, field: str, detail: str) -> None:
        violations.append(Violation(code=code, field=field, detail=detail))

    prices: tuple[tuple[str, Decimal, str], ...] = (
        ("open", open_, candle.open),
        ("high", high, candle.high),
        ("low", low, candle.low),
        ("close", close, candle.close),
    )
    for name, value, text in prices:
        if not value > 0:
            add(ViolationCode.NON_POSITIVE_PRICE, name, f"{name} {text} must be > 0")
    if not high >= open_:
        add(ViolationCode.HIGH_BELOW_OPEN, "high", f"high {candle.high} is below open {candle.open}")
    if not high >= close:
        add(ViolationCode.HIGH_BELOW_CLOSE, "high", f"high {candle.high} is below close {candle.close}")
    if not high >= low:
        add(ViolationCode.HIGH_BELOW_LOW, "high", f"high {candle.high} is below low {candle.low}")
    if not low <= open_:
        add(ViolationCode.LOW_ABOVE_OPEN, "low", f"low {candle.low} is above open {candle.open}")
    if not low <= close:
        add(ViolationCode.LOW_ABOVE_CLOSE, "low", f"low {candle.low} is above close {candle.close}")
    if not volume >= 0:
        add(ViolationCode.NEGATIVE_VOLUME, "volume", f"volume {candle.volume} must be >= 0")
    if candle.turnover is not None and not to_decimal(candle.turnover) >= 0:
        add(ViolationCode.NEGATIVE_TURNOVER, "turnover", f"turnover {candle.turnover} must be >= 0")
    return tuple(violations)


def require_valid(candle: Candle) -> ValidatedCandle:
    """Return ``candle`` itself (same object) as a ``ValidatedCandle`` if it has no violations.

    Raises ``CandleValidationError`` carrying every violation otherwise.
    """
    violations = validate_candle(candle)
    if violations:
        raise CandleValidationError(violations)
    return ValidatedCandle(candle)
