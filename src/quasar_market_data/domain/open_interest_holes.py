"""Holes of a stored open interest series and the pages that fetch them (ADR-0003 P-7, P-12).

A *hole* is a slot in ``[oldest, newest]`` that is neither stored nor conflicted (a conflict is a recorded fact,
not a hole). ``oldest`` is the retrieval horizon (``as_of - H``, rounded up to the period): slots older than that
are ``no_longer_recoverable`` and never requested. ``newest`` is the newest slot that is no longer
``not_yet_expected`` (``as_of - expected_after``, rounded down); newer slots belong to the rounds, whose overlapping
range covers them.

``plan_pages`` turns the available holes of every symbol into requests of at most ``MAX_PERIOD_ENDS`` period ends.
The plan is a pure function of its input: the same holes in any container order give the same pages, ordered by
``(start, symbol)`` so the slots closest to leaving the horizon come first.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from quasar_market_data.domain.open_interest import period_end_count
from quasar_market_data.domain.open_interest_request import MAX_PERIOD_ENDS, RETRIEVAL_HORIZON_MS
from quasar_market_data.domain.open_interest_schedule import expected_after_ms
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import ensure_timestamp_ms

DAY_MS: Final = 86_400_000


def _int(value: int, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    return value


def oldest_allowed_ms(as_of_ms: int, period: Timeframe, retrieval_horizon_ms: int = RETRIEVAL_HORIZON_MS) -> int:
    """The oldest slot that may be requested at ``as_of_ms``: ``as_of - H`` rounded up to the period (``H`` is the
    configured depth, ``backfill_days``)."""
    as_of = ensure_timestamp_ms(as_of_ms, "as_of_ms")
    step = period.milliseconds
    return -(-(as_of - retrieval_horizon_ms) // step) * step


def newest_expected_ms(as_of_ms: int, cadence: int, offset_ms: int, period: Timeframe) -> int:
    """The newest slot older than ``expected_after`` at ``as_of_ms``, rounded down to the period."""
    as_of = ensure_timestamp_ms(as_of_ms, "as_of_ms")
    step = period.milliseconds
    return (as_of - expected_after_ms(cadence, offset_ms, period)) // step * step


def _aligned(value: int, name: str, period: Timeframe) -> int:
    slot = _int(value, name)
    if slot % period.milliseconds:
        raise ValueError(f"{name} {slot} is not aligned to {period.label}")
    return slot


def holes_in(
    stored: Iterable[int], conflicted: Iterable[int], oldest_ms: int, newest_ms: int, period: Timeframe
) -> tuple[int, ...]:
    """The slots in ``[oldest_ms, newest_ms]`` that are neither stored nor conflicted, ascending."""
    oldest = _aligned(oldest_ms, "oldest_ms", period)
    newest = _aligned(newest_ms, "newest_ms", period)
    if newest < oldest:
        return ()
    present = set(stored) | set(conflicted)
    return tuple(slot for slot in range(oldest, newest + 1, period.milliseconds) if slot not in present)


@dataclass(frozen=True)
class Page:
    symbol: str
    start_ms: int
    end_ms: int  # the last planned slot; the request is [start_ms, end_ms]
    slots: tuple[int, ...]  # the available holes this page was planned for

    def period_ends(self, period: Timeframe) -> int:
        return period_end_count(self.start_ms, self.end_ms, period)


def _pages_of(symbol: str, slots: Sequence[int], period: Timeframe) -> list[Page]:
    step = period.milliseconds
    ordered = [_aligned(slot, "slot", period) for slot in slots]
    if any(ordered[i] <= ordered[i - 1] for i in range(1, len(ordered))):
        raise ValueError(f"slots of {symbol!r} must be strictly ascending")
    pages: list[Page] = []
    index = 0
    while index < len(ordered):
        start = ordered[index]
        limit = start + (MAX_PERIOD_ENDS - 1) * step
        stop = index
        while stop < len(ordered) and ordered[stop] <= limit:
            stop += 1
        chunk = tuple(ordered[index:stop])
        pages.append(Page(symbol, start, chunk[-1], chunk))
        index = stop
    return pages


def plan_pages(available: Mapping[str, Sequence[int]], period: Timeframe) -> tuple[Page, ...]:
    """Pages of at most 500 period ends per symbol, all symbols ordered by ``(start, symbol)``."""
    pages: list[Page] = []
    for symbol, slots in available.items():
        pages.extend(_pages_of(symbol, slots, period))
    return tuple(sorted(pages, key=lambda page: (page.start_ms, page.symbol)))


def slots_at_risk(
    pages: Sequence[Page],
    as_of_ms: int,
    share_per_cycle: int,
    period: Timeframe,
    margin_ms: int = DAY_MS,
    retrieval_horizon_ms: int = RETRIEVAL_HORIZON_MS,
) -> Mapping[str, tuple[int, ...]]:
    """Holes that will leave the horizon before the plan can reach them within the backfill share.

    Only slots within ``margin_ms`` of leaving are judged. The page at position ``p`` (1-based, the order of
    ``pages``) is reached in window ``ceil(p / share)``, window 1 being now and window ``k`` ``(k - 1)`` periods from now;
    a slot ``T`` can still be fetched in windows up to ``(T - (as_of - H)) // period + 1``. The share is optimistic
    (every window's whole share is used), so a slot named here is lost even then.
    """
    as_of = ensure_timestamp_ms(as_of_ms, "as_of_ms")
    share = _int(share_per_cycle, "share_per_cycle")
    if share < 1:
        raise ValueError(f"share_per_cycle must be >= 1, got {share}")
    margin = _int(margin_ms, "margin_ms")
    horizon_start = as_of - retrieval_horizon_ms
    lost: dict[str, list[int]] = {}
    for position, page in enumerate(pages, start=1):
        for slot in page.slots:
            if slot >= horizon_start + margin:
                continue
            cycles_left = (slot - horizon_start) // period.milliseconds
            if position > (cycles_left + 1) * share:
                lost.setdefault(page.symbol, []).append(slot)
    return {symbol: tuple(sorted(slots)) for symbol, slots in sorted(lost.items())}
