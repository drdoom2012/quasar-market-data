"""When funding rate rounds run (ADR-0004 P-10).

The round of hour ``B`` (a whole hour) wakes at ``B + round_offset_ms`` and has the deadline
``B + 3 600 000 - 15 000``, 15 s before the next hour, so two rounds never overlap (the next wake is at least
``B + 3 600 000 + 60 000``). A round whose wake has passed but whose deadline has not runs late; one whose deadline
has passed is skipped and counted. ``next_round`` picks the boundary to run next from the clock and the last
boundary run.

All times are epoch milliseconds, integers only; wrong types raise ``TypeError``.
"""

from dataclasses import dataclass
from typing import Final

from quasar_market_data.domain.funding_rate_capacity import MAX_ROUND_OFFSET_MS, MIN_ROUND_OFFSET_MS
from quasar_market_data.domain.timestamps import ensure_timestamp_ms

ROUND_PERIOD_MS: Final = 3_600_000
ROUND_DEADLINE_MARGIN_MS: Final = 15_000


def _offset(offset_ms: int) -> int:
    if type(offset_ms) is not int:
        raise TypeError(f"round_offset_ms must be int, got {type(offset_ms).__name__}")
    if not MIN_ROUND_OFFSET_MS <= offset_ms <= MAX_ROUND_OFFSET_MS:
        raise ValueError(
            f"round_offset_ms must be within [{MIN_ROUND_OFFSET_MS}, {MAX_ROUND_OFFSET_MS}], got {offset_ms}"
        )
    return offset_ms


def _boundary(boundary_ms: int) -> int:
    boundary = ensure_timestamp_ms(boundary_ms, "boundary_ms")
    if boundary % ROUND_PERIOD_MS != 0:
        raise ValueError(f"boundary_ms {boundary} is not a whole hour")
    return boundary


def round_wake_ms(boundary_ms: int, offset_ms: int) -> int:
    return _boundary(boundary_ms) + _offset(offset_ms)


def round_deadline_ms(boundary_ms: int) -> int:
    """``B + 3 600 000 - 15 000``: started round requests are cancelled here."""
    return _boundary(boundary_ms) + ROUND_PERIOD_MS - ROUND_DEADLINE_MARGIN_MS


@dataclass(frozen=True)
class RoundPlan:
    boundary_ms: int
    wake_ms: int
    skipped: int  # boundaries between the last run and this one that are never run


def next_round(now_ms: int, last_boundary_ms: int | None, offset_ms: int) -> RoundPlan:
    """The boundary to run next: the oldest one after ``last_boundary_ms`` whose deadline is still ahead.

    Its wake may already have passed (the round runs late) or lie in the future (wait for it). Boundaries between
    the last run and the chosen one are counted as skipped.
    """
    now = ensure_timestamp_ms(now_ms, "now_ms")
    offset = _offset(offset_ms)
    last = None if last_boundary_ms is None else _boundary(last_boundary_ms)
    # smallest whole hour b with b + 1 h - 15 s > now
    chosen = (now - ROUND_PERIOD_MS + ROUND_DEADLINE_MARGIN_MS) // ROUND_PERIOD_MS * ROUND_PERIOD_MS + ROUND_PERIOD_MS
    if last is not None and chosen <= last:
        chosen = last + ROUND_PERIOD_MS
    skipped = 0 if last is None else (chosen - last) // ROUND_PERIOD_MS - 1
    return RoundPlan(chosen, chosen + offset, skipped)
