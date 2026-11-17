"""The pure core of the symbol detail: windows, exact change figures and the document (design 5.2, 5.3).

Nothing here does I/O. Decimals stay text; the only arithmetic on them is ``percent_change``, done with ``Decimal``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from tools.symbol_overview.model import (
    FUNDING_MIN_SLOTS,
    MAX_TS_MS,
    MIN_TS_MS,
    OI_REFERENCE_SLOTS,
    RANGE_SLOTS,
    TF_MS,
    CandleRow,
    FundingWindow,
    InstrumentRow,
    Invalid,
    OiRow,
    Window,
)


@dataclass(frozen=True)
class DetailWindows:
    shown: Window  # the candle slots the user asked for, [S, E)
    candles: Window  # [S - tf, E): one slot of lookback for the change figure
    open_interest: Window  # period ENDS [S + tf - 1 h, E + tf): one hour of lookback; rows from S + tf are shown
    funding: (
        FundingWindow  # closed [min(S, E - 7 days), as_of]: always at least 7 days, so the latest settlement exists
    )


def detail_windows(as_of_ms: int, range_name: str) -> DetailWindows:
    """The windows of one detail, anchored on the API's clock. ``ValueError`` for an unknown range or a bad clock."""
    if type(as_of_ms) is not int:
        raise ValueError(f"as_of_ms must be int, got {type(as_of_ms).__name__}")
    slots = RANGE_SLOTS.get(range_name)
    if slots is None:
        raise ValueError(f"unknown range {range_name!r}")
    end = (as_of_ms // TF_MS + 1) * TF_MS
    start = end - slots * TF_MS
    windows = DetailWindows(
        shown=Window(start, end),
        candles=Window(start - TF_MS, end),
        open_interest=Window(start + TF_MS - OI_REFERENCE_SLOTS * TF_MS, end + TF_MS),
        funding=FundingWindow(min(start, end - FUNDING_MIN_SLOTS * TF_MS), as_of_ms),
    )
    bounds = (
        windows.candles.start,
        windows.candles.end,
        windows.open_interest.start,
        windows.open_interest.end,
        windows.funding.start,
        windows.funding.end,
    )
    if not all(MIN_TS_MS <= bound <= MAX_TS_MS for bound in bounds):
        raise ValueError("a window of the detail is outside the valid time range")
    return windows


def percent_change(new: str, old: str) -> str | None:
    """``(new - old) / old * 100`` as text with two places, half-even; ``None`` when it cannot be computed.

    The sign is decided by the rounded value, not by the text: a change that rounds to zero is ``"0.00"``.
    """
    try:
        new_value, old_value = Decimal(new), Decimal(old)
        if not (new_value.is_finite() and old_value.is_finite()) or old_value == 0:
            return None
        rounded = ((new_value - old_value) / old_value * 100).quantize(Decimal("0.01"), ROUND_HALF_EVEN)
    except (InvalidOperation, ArithmeticError, TypeError, ValueError):
        return None
    return "0.00" if rounded == 0 else f"{rounded:+.2f}"


def candle_change(rows: Sequence[CandleRow]) -> dict[str, str] | None:
    """The last close against the close one slot earlier; ``None`` when that slot has no candle. Rows may include the lookback."""
    if len(rows) < 2 or rows[-1].t - rows[-2].t != TF_MS:
        return None
    percent = percent_change(rows[-1].c, rows[-2].c)
    return None if percent is None else {"percent": percent, "previous_close": rows[-2].c}


def open_interest_change(rows: Sequence[OiRow]) -> dict[str, object] | None:
    """The latest ``open_interest`` (the quantity) against the row exactly one hour earlier; ``None`` when that row is absent.

    The quantity, not ``open_interest_value``: the value is the quantity times the price, so it moves with the price and
    would show the price's change, not the change in open positions.
    """
    if not rows:
        return None
    reference_t = rows[-1].t - OI_REFERENCE_SLOTS * TF_MS
    reference = next((row for row in reversed(rows) if row.t == reference_t), None)
    if reference is None:
        return None
    percent = percent_change(rows[-1].oi, reference.oi)
    return None if percent is None else {"percent": percent, "reference_ms": reference.t}


def part_ok(**fields: object) -> dict[str, object]:
    return {"status": "ok", "code": "", "detail": "", **fields}


def part_empty() -> dict[str, object]:
    """Nothing is stored for this part of the series (no call was needed, or the API says no row was ever stored)."""
    return {"status": "empty", "code": "", "detail": ""}


def part_disabled() -> dict[str, object]:
    return {"status": "disabled", "code": "", "detail": ""}


def part_not_collected() -> dict[str, object]:
    """The API lists the series with ``open_interest_period: null`` (ADR-0003 A-6): not collected, no call was made."""
    return {"status": "not_collected", "code": "", "detail": ""}


def part_error(invalid: Invalid) -> dict[str, object]:
    """The part could not be loaded or validated; it carries no rows (a half-loaded series is never shown)."""
    return {"status": "error", "code": invalid.code, "detail": invalid.detail}


def build_detail(
    *,
    instrument: InstrumentRow,
    range_name: str,
    as_of_ms: int,
    windows: DetailWindows,
    candles: dict[str, object],
    open_interest: dict[str, object],
    funding_rate: dict[str, object],
) -> dict[str, object]:
    return {
        "symbol": instrument.symbol,
        "venue": instrument.venue,
        "timeframe": instrument.timeframe,
        "range": range_name,
        "as_of_ms": as_of_ms,
        "window": {"start": windows.shown.start, "end": windows.shown.end},
        "candles": candles,
        "open_interest": open_interest,
        "funding_rate": funding_rate,
    }
