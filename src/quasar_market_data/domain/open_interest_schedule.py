"""When open interest rounds run and how the idle time between them is protected (ADR-0003 P-10, P-12).

A round for boundary ``B`` (a period end, aligned) wakes at ``wake(B) = B + round_offset_ms`` and must finish by
``deadline(B) = wake(B) + period - 15 s``, 15 s before the next round is due. ``next_round`` picks the boundary to run
next from the clock and the last boundary run: a round whose wake has passed but whose deadline has not runs late; one
whose deadline has passed is skipped and counted. Catch-up work is allowed only in the idle window that ends
``ROUND_DEADLINE_RESERVE_MS`` before the next wake, and only while the next round keeps its budget
(``backfill_may_start``).

All times are epoch milliseconds, integers only; wrong types raise ``TypeError``.
"""

from dataclasses import dataclass
from typing import Final

from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import ensure_timestamp_ms

ROUND_DEADLINE_RESERVE_MS: Final = 15_000


def _int(value: int, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    return value


def _offset(offset_ms: int, period: Timeframe) -> int:
    offset = _int(offset_ms, "round_offset_ms")
    if not 0 <= offset < period.milliseconds:
        raise ValueError(f"round_offset_ms must be within [0, {period.milliseconds}), got {offset}")
    return offset


def _boundary(boundary_ms: int, period: Timeframe) -> int:
    boundary = ensure_timestamp_ms(boundary_ms, "boundary_ms")
    if not period.is_aligned(boundary):
        raise ValueError(f"boundary_ms {boundary} is not aligned to {period.label}")
    return boundary


def round_wake_ms(boundary_ms: int, offset_ms: int, period: Timeframe) -> int:
    return _boundary(boundary_ms, period) + _offset(offset_ms, period)


def round_deadline_ms(boundary_ms: int, offset_ms: int, period: Timeframe) -> int:
    """``B + period + offset - 15 s``: the round is cancelled here (ADR-0003 P-10)."""
    return round_wake_ms(boundary_ms, offset_ms, period) + period.milliseconds - ROUND_DEADLINE_RESERVE_MS


def idle_deadline_ms(next_wake_ms: int) -> int:
    """The idle window before a round ends 15 s before that round's wake."""
    return ensure_timestamp_ms(next_wake_ms, "next_wake_ms") - ROUND_DEADLINE_RESERVE_MS


def round_range(boundary_ms: int, cadence: int, period: Timeframe) -> tuple[int, int]:
    """``[B - 2c * period, B]``: ``2c + 1`` period ends (ADR-0003 P-10, overlapping range)."""
    boundary = _boundary(boundary_ms, period)
    c = _int(cadence, "cadence")
    if c < 1:
        raise ValueError(f"cadence must be >= 1, got {c}")
    return boundary - 2 * c * period.milliseconds, boundary


def expected_after_ms(cadence: int, offset_ms: int, period: Timeframe) -> int:
    """``c * period + round_offset``: after this a slot is no longer ``not_yet_expected`` (ADR-0003 P-10)."""
    c = _int(cadence, "cadence")
    if c < 1:
        raise ValueError(f"cadence must be >= 1, got {c}")
    return c * period.milliseconds + _offset(offset_ms, period)


@dataclass(frozen=True)
class NextRound:
    boundary_ms: int
    wake_ms: int
    skipped: int  # boundaries between the last run and this one that are never run


def next_round(now_ms: int, last_boundary_ms: int | None, offset_ms: int, period: Timeframe) -> NextRound:
    now = ensure_timestamp_ms(now_ms, "now_ms")
    offset = _offset(offset_ms, period)
    step = period.milliseconds
    last = None if last_boundary_ms is None else _boundary(last_boundary_ms, period)
    newest_woken = (now - offset) // step * step  # the newest boundary whose wake is at or before now
    if last is not None and newest_woken <= last:
        chosen = last + step  # its wake is in the future, or it is late and still inside its deadline
    elif now < round_deadline_ms(newest_woken, offset, period):
        chosen = newest_woken
    else:
        chosen = newest_woken + step
    skipped = 0 if last is None else (chosen - last) // step - 1
    return NextRound(chosen, round_wake_ms(chosen, offset, period), skipped)


def backfill_may_start(committed_now: int, committed_at_next_wake: int, budget_per_5min: int, round_cost: int) -> bool:
    """A catch-up request may start only if it is grantable at once and the next round keeps its whole cost.

    ``committed_now`` counts the budget starts in the window now, ``committed_at_next_wake`` those still counted at the
    next round's wake instant. The first test keeps the request from sleeping in the budget in front of the round; the
    second keeps ``round_cost`` starts free for the round (ADR-0003 P-15, rounds pre-empt backfill).
    """
    for name, value in (
        ("committed_now", committed_now),
        ("committed_at_next_wake", committed_at_next_wake),
        ("budget_per_5min", budget_per_5min),
        ("round_cost", round_cost),
    ):
        _int(value, name)
    return committed_now + 1 <= budget_per_5min and committed_at_next_wake + 1 + round_cost <= budget_per_5min
