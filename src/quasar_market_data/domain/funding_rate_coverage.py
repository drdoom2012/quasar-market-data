"""Window coverage and series health of the funding rate API (contract api-v1 6.7, ADR-0004 P-7, P-13).

Pure and integer-only. The Collector grows a per-series verified span ``[from_ms, through_ms]``
(``funding_rate_span``); this module only reads it:

* ``window_state``: whether a requested closed window ``[start, end]`` lies wholly inside the span
  (``VERIFIED``), partly (``PARTLY_VERIFIED``) or not at all (``UNVERIFIED``). It says what the Collector
  verified, never that a settlement is missing or that none occurred.
* ``is_complete``: ``VERIFIED`` and no conflicted event in the window.
* ``funding_series_health``: the three per-series flags of ``/health/ingest`` (``behind``, ``catching_up``,
  ``quiet``). ``behind`` and ``catching_up`` are the Collector's own functions (``is_behind``,
  ``history_pending``), so the two processes cannot drift apart.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from quasar_market_data.domain.funding_rate_span import (
    BEHIND_THRESHOLD_MS,
    DAY_MS,
    MAX_BACKFILL_DAYS,
    MIN_BACKFILL_DAYS,
    Interval,
    history_pending,
    is_behind,
)
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, ensure_timestamp_ms

MAX_FUNDING_WINDOW_DAYS: Final = 365  # contract 6.7.2 (decision FA2, set to 365 days by the user on 2026-11-04)
MAX_FUNDING_WINDOW_MS: Final = MAX_FUNDING_WINDOW_DAYS * DAY_MS
QUIET_AFTER_MS: Final = 2 * DAY_MS  # contract 6.7.8: a series with no event for more than 48 h is quiet


@dataclass(frozen=True)
class FundingRateRule:
    """What the API needs of the ``[funding_rate]`` table: the publication delay and the history depth."""

    publication_delay_ms: int
    backfill_days: int

    def __post_init__(self) -> None:
        behind_after_ms(self.publication_delay_ms)  # validates the type and the sign
        if type(self.backfill_days) is not int:
            raise TypeError(f"backfill_days must be int, got {type(self.backfill_days).__name__}")
        if not MIN_BACKFILL_DAYS <= self.backfill_days <= MAX_BACKFILL_DAYS:
            raise ValueError(f"backfill_days must be within [{MIN_BACKFILL_DAYS}, {MAX_BACKFILL_DAYS}]")


class WindowState(StrEnum):
    VERIFIED = "verified"
    PARTLY_VERIFIED = "partly_verified"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class FundingSeriesHealth:
    behind: bool
    catching_up: bool
    quiet: bool


def _window(start: int, end: int) -> tuple[int, int]:
    first = ensure_timestamp_ms(start, "start")
    last = ensure_timestamp_ms(end, "end")
    if last < first:
        raise ValueError(f"end {last} is before start {first}")
    return first, last


def window_state(span: Interval | None, start: int, end: int) -> WindowState:
    """The state of the closed window ``[start, end]`` against the verified span (contract 6.7.5)."""
    first, last = _window(start, end)
    if span is None or last < span.from_ms or first > span.through_ms:
        return WindowState.UNVERIFIED
    if span.from_ms <= first and last <= span.through_ms:
        return WindowState.VERIFIED
    return WindowState.PARTLY_VERIFIED


def is_complete(state: WindowState, conflicted_events: int) -> bool:
    """``True`` only for a verified window with no conflicted event in it (ADR-0004 P-13)."""
    if type(conflicted_events) is not int:
        raise TypeError(f"conflicted_events must be int, got {type(conflicted_events).__name__}")
    if conflicted_events < 0:
        raise ValueError(f"conflicted_events must be >= 0, got {conflicted_events}")
    return state is WindowState.VERIFIED and conflicted_events == 0


def defaulted_window_end(start: int, as_of_ms: int) -> int:
    """The ``end`` of a request without one: ``max(as_of, start)``, capped at ``start + 365 days`` (contract 6.7.2).

    The cap is clamped to the last valid time, so the result is always a valid ``end``.
    """
    first = ensure_timestamp_ms(start, "start")
    now = ensure_timestamp_ms(as_of_ms, "as_of_ms")
    return min(max(now, first), first + MAX_FUNDING_WINDOW_MS, MAX_TIMESTAMP_MS)


def behind_after_ms(publication_delay_ms: int) -> int:
    """The ``rule.behind_after_ms`` of the health object: ``as_of - through >= this`` means behind (inclusive)."""
    if type(publication_delay_ms) is not int:
        raise TypeError(f"publication_delay_ms must be int, got {type(publication_delay_ms).__name__}")
    if publication_delay_ms < 0:
        raise ValueError(f"publication_delay_ms must be >= 0, got {publication_delay_ms}")
    return publication_delay_ms + BEHIND_THRESHOLD_MS


def funding_series_health(
    span: Interval,
    newest_row_ms: int | None,
    as_of_ms: int,
    publication_delay_ms: int,
    backfill_days: int,
) -> FundingSeriesHealth:
    """The flags of one series (contract 6.7.8).

    ``behind``: ``is_behind(as_of - delay, span)``. ``catching_up``: ``history_pending(span, backfill_days)``, which
    depends on the span and the configuration only. ``quiet``: no stored row, or ``as_of - newest`` strictly above 48 h.
    A host clock too close to the minimum time (``as_of - delay`` out of range) raises ``ValueError``: fail closed.
    """
    now = ensure_timestamp_ms(as_of_ms, "as_of_ms")
    delay = behind_after_ms(publication_delay_ms) - BEHIND_THRESHOLD_MS  # validates the type and sign
    newest = None if newest_row_ms is None else ensure_timestamp_ms(newest_row_ms, "newest_row_ms")
    return FundingSeriesHealth(
        behind=is_behind(now - delay, span),
        catching_up=history_pending(span, backfill_days),
        quiet=newest is None or now - newest > QUIET_AFTER_MS,
    )
