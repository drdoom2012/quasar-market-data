"""Which funding rate request comes next: the pure selection of ADR-0004 P-10.

``select_next`` is a function of explicit inputs only (the series statuses, the flags of the dispatcher, ``now`` and
``b_now``), so the same inputs give the same next request. Every ordering is total (ties by ``symbol``).

Kinds and classes (the limiter shares are in P-9): ``S`` seed of a series without a span, ``R`` the hourly check of
a series with a span (both class ``R``, first page only, only while a round is active); ``C`` the next page of a
truncated forward range; ``H`` the next chunk, or the next page of the current chunk, of backward history (both
class ``C``/``H`` share). ``behind`` is informational and never moves a series between kinds: only a series with
``continuing_from_ms`` set is a ``C`` candidate (the start-up reconcile sets it for a series that is behind after a
restart, P-11 step 2).

Selection rule 1: while a round is active and the ``R`` share has room, the next ``S`` or ``R`` series (series
without a span first, then ascending ``verified_at_ms``, then ``symbol``). Rule 2: otherwise, when the ``C``+``H``
share has room, ``turn`` names the class that was **not** granted last (initially ``C``); that class is granted if it
has an eligible series, otherwise the other one. Within ``C``: ascending ``verified_at_ms``, then ``symbol``. Within
``H``: a chunk in progress first, then a series never served in this process run, then the smallest ``last_served``,
then ``symbol``. Rule 3 (one request per series at a time) is the ``busy`` flag.

Times are integer milliseconds; ``now_ms`` and ``unavailable_until_ms`` are on the same (monotonic) clock, the other
times are epoch milliseconds.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from quasar_market_data.domain.funding_rate_capacity import RequestClass
from quasar_market_data.domain.funding_rate_span import Interval, next_history_chunk

ClassTurn = Literal["C", "H"]
PlanKind = Literal["S", "R", "C", "H"]

_FAR: Final = 2**62


@dataclass(frozen=True)
class HistoryChunk:
    """A backward chunk ``[from_ms, through_ms]`` whose next page starts at ``next_start_ms``."""

    from_ms: int
    through_ms: int
    next_start_ms: int

    def __post_init__(self) -> None:
        if not self.from_ms <= self.next_start_ms <= self.through_ms:
            raise ValueError(f"next_start_ms {self.next_start_ms} must lie within [{self.from_ms}, {self.through_ms}]")


@dataclass(frozen=True)
class SeriesStatus:
    symbol: str
    span: Interval | None
    verified_at_ms: int | None
    busy: bool
    unavailable_until_ms: int
    continuing_from_ms: int | None
    history_chunk: HistoryChunk | None
    never_served: bool
    last_served: int
    asked_in_round: bool
    needs_reread: bool


@dataclass(frozen=True)
class Plan:
    symbol: str
    kind: PlanKind
    request_class: RequestClass
    start_ms: int
    end_ms: int
    chunk: HistoryChunk | None  # the chunk an H page belongs to (``next_start_ms`` is this page's start)


@dataclass(frozen=True)
class Selection:
    plan: Plan
    turn: ClassTurn  # the class not granted last, to pass to the next call


def _available(series: SeriesStatus, now_ms: int) -> bool:
    return not series.busy and series.unavailable_until_ms <= now_ms and not series.needs_reread


def _round_plan(series: SeriesStatus, b_now_ms: int | None, recheck_ms: int) -> Plan | None:
    if b_now_ms is None or series.asked_in_round:
        return None
    if series.span is None:
        return Plan(series.symbol, "S", RequestClass.R, b_now_ms - recheck_ms, b_now_ms, None)
    if series.continuing_from_ms is not None:
        return None
    start = max(series.span.from_ms, series.span.through_ms - recheck_ms)
    if start > b_now_ms:
        return None
    return Plan(series.symbol, "R", RequestClass.R, start, b_now_ms, None)


def _round_key(series: SeriesStatus) -> tuple[int, int, str]:
    return (0 if series.span is None else 1, series.verified_at_ms or 0, series.symbol)


def _catch_up_plan(series: SeriesStatus, b_now_ms: int | None) -> Plan | None:
    start = series.continuing_from_ms
    if start is None or b_now_ms is None or start > b_now_ms:
        return None
    return Plan(series.symbol, "C", RequestClass.C, start, b_now_ms, None)


def _history_plan(series: SeriesStatus, backfill_days: int) -> Plan | None:
    chunk = series.history_chunk
    if chunk is None:
        if series.span is None:
            return None
        try:
            fresh = next_history_chunk(series.span, backfill_days)
        except ValueError:
            return None  # near the minimum timestamp: fail closed, request nothing
        if fresh is None:
            return None
        chunk = HistoryChunk(fresh.from_ms, fresh.through_ms, fresh.from_ms)
    return Plan(series.symbol, "H", RequestClass.H, chunk.next_start_ms, chunk.through_ms, chunk)


def select_next(
    series: Sequence[SeriesStatus],
    *,
    now_ms: int,
    b_now_ms: int | None,
    round_active: bool,
    r_room: bool,
    ch_room: bool,
    turn: ClassTurn,
    recheck_ms: int,
    backfill_days: int,
) -> Selection | None:
    candidates = [s for s in series if _available(s, now_ms)]
    if round_active and r_room:
        ranked = sorted(
            ((_round_key(s), plan) for s in candidates if (plan := _round_plan(s, b_now_ms, recheck_ms)) is not None),
            key=lambda pair: pair[0],
        )
        if ranked:
            return Selection(ranked[0][1], turn)
    if not ch_room:
        return None
    catch_up = sorted(
        ((s.verified_at_ms or 0, s.symbol, plan) for s in candidates if (plan := _catch_up_plan(s, b_now_ms))),
        key=lambda item: (item[0], item[1]),
    )
    history = sorted(
        (
            (
                (0 if s.history_chunk is not None else 1, 0 if s.never_served else 1, s.last_served, s.symbol),
                plan,
            )
            for s in candidates
            if (plan := _history_plan(s, backfill_days)) is not None
        ),
        key=lambda pair: pair[0],
    )
    for granted in ("C", "H") if turn == "C" else ("H", "C"):
        if granted == "C" and catch_up:
            return Selection(catch_up[0][2], "H")
        if granted == "H" and history:
            return Selection(history[0][1], "C")
    return None
