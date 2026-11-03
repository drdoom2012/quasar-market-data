"""Funding rate limiter shares, sustainable capacity and planned latency (ADR-0004 P-9, P-10, P-12).

The local limiter allows at most ``b`` request starts in any window of ``LIMITER_WINDOW_MS`` (the
documented 300 s plus a 5 s guard). The budget splits into the round share ``m = floor(b / 2)`` (hourly
rounds and seeds) and the rest (catch-up and history); neither share is lent to the other.

Hourly rounds wake ``round_offset_ms`` after the hour and must have started their last request 30 s
before a deadline that is 15 s before the next hour. ``groups_k`` is the number of whole limiter windows
that fit after the first group; the Collector releases ``m`` requests per window. Rounds repeat every
hour and share the limiter's window, so the capacity of one *isolated* round is not sustainable;
``sustained_capacity`` (``Q * m - 1`` with ``Q = min(K + 1, 11)``) is. The Collector refuses to start
above it and warns above 90 % of it.

Integer arithmetic only.
"""

from dataclasses import dataclass
from typing import Final

MIN_BUDGET_PER_5MIN: Final = 50
MAX_BUDGET_PER_5MIN: Final = 250
DEFAULT_BUDGET_PER_5MIN: Final = 150
MIN_ROUND_OFFSET_MS: Final = 60_000
MAX_ROUND_OFFSET_MS: Final = 600_000
LIMITER_WINDOW_MS: Final = 305_000  # 300 000 ms documented window + 5 000 ms guard
ROUND_LAST_START_BUDGET_MS: Final = 3_555_000  # 3 600 000 - 15 000 deadline margin - 30 000 last-start margin
_HOUR_MS: Final = 3_600_000
MAX_FULL_GROUPS: Final = _HOUR_MS // LIMITER_WINDOW_MS


class CapacityError(ValueError):
    """The symbol count exceeds what hourly rounds can sustain at the configured budget and offset."""


def _ensure_int(value: int, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    return value


def _budget(budget_per_5min: int) -> int:
    budget = _ensure_int(budget_per_5min, "budget_per_5min")
    if not MIN_BUDGET_PER_5MIN <= budget <= MAX_BUDGET_PER_5MIN:
        raise ValueError(f"budget_per_5min must be within [{MIN_BUDGET_PER_5MIN}, {MAX_BUDGET_PER_5MIN}], got {budget}")
    return budget


def _offset(round_offset_ms: int) -> int:
    offset = _ensure_int(round_offset_ms, "round_offset_ms")
    if not MIN_ROUND_OFFSET_MS <= offset <= MAX_ROUND_OFFSET_MS:
        raise ValueError(f"round_offset_ms must be within [{MIN_ROUND_OFFSET_MS}, {MAX_ROUND_OFFSET_MS}], got {offset}")
    return offset


def round_share(budget_per_5min: int) -> int:
    """``m``: request starts per window reserved for rounds and seeds."""
    return _budget(budget_per_5min) // 2


def history_catchup_share(budget_per_5min: int) -> int:
    """Request starts per window reserved for catch-up and history: ``b - m``."""
    budget = _budget(budget_per_5min)
    return budget - budget // 2


def groups_k(round_offset_ms: int) -> int:
    """``K``: whole limiter windows that fit between the first group and the last allowed start."""
    return (ROUND_LAST_START_BUDGET_MS - _offset(round_offset_ms)) // LIMITER_WINDOW_MS


def full_groups_per_round(round_offset_ms: int) -> int:
    """``Q = min(K + 1, 11)``: full groups of ``m`` requests a round can place and still leave the window empty."""
    return min(groups_k(round_offset_ms) + 1, MAX_FULL_GROUPS)


def isolated_round_capacity(budget_per_5min: int, round_offset_ms: int) -> int:
    """``(K + 1) * m - 1``: the capacity of ONE round started with an empty window. Not sustainable; not used to validate."""
    return (groups_k(round_offset_ms) + 1) * round_share(budget_per_5min) - 1


def sustained_capacity(budget_per_5min: int, round_offset_ms: int) -> int:
    """``S_sustained = Q * m - 1``: a lower bound on the symbol count whose hourly rounds start every series.

    It holds for an ideal run (ADR-0004 P-10): not a guarantee under errors or slow requests, and not a hard
    maximum (a greedy policy sustained about 7 % more in the simulation).
    """
    return full_groups_per_round(round_offset_ms) * round_share(budget_per_5min) - 1


def warning_threshold(sustained: int) -> int:
    """``floor(0.9 * sustained)``: above it less than 10 % of the capacity is left for retries and slow requests."""
    value = _ensure_int(sustained, "sustained")
    if value < 0:
        raise ValueError(f"sustained must be >= 0, got {value}")
    return value * 9 // 10


@dataclass(frozen=True)
class CapacityAssessment:
    sustained: int
    warn_above: int
    warn: bool


def assess_capacity(symbol_count: int, budget_per_5min: int, round_offset_ms: int) -> CapacityAssessment:
    """Refuse (``CapacityError``) above ``S_sustained``; ``warn`` is set above ``floor(0.9 * S_sustained)``."""
    count = _ensure_int(symbol_count, "symbol_count")
    if count < 1:
        raise ValueError(f"symbol_count must be >= 1, got {count}")
    sustained = sustained_capacity(budget_per_5min, round_offset_ms)
    if count > sustained:
        raise CapacityError(
            f"symbol_count {count} exceeds the sustainable capacity S_sustained {sustained} at "
            f"budget_per_5min {budget_per_5min} and round_offset_ms {round_offset_ms}"
        )
    threshold = warning_threshold(sustained)
    return CapacityAssessment(sustained=sustained, warn_above=threshold, warn=count > threshold)


def planned_start_offset_ms(index: int, budget_per_5min: int) -> int:
    """Planned start of request ``index`` after the wake of an isolated round (index 0 is the server-time call).

    The planned latency from a settlement to the request is ``round_offset_ms`` plus this value. It is the
    plan of an ideal run (no errors, no ``429``, no yielding): no latency is guaranteed (ADR-0004 P-10).
    """
    position = _ensure_int(index, "index")
    if position < 0:
        raise ValueError(f"index must be >= 0, got {position}")
    return (position // round_share(budget_per_5min)) * LIMITER_WINDOW_MS
