"""Restart reconcile of one series from stored facts (S15): reach arithmetic and window classification.

Pure: every function takes its instants as epoch-millisecond ``int`` arguments, reads no clock and does no I/O.
The facts are what the database holds; this module never creates a candle and never decides that history exists.

Assumption A1 (an operating assumption, not a property the exchange guarantees): the server's forming slot is at
most ``REACH_MARGIN_SLOTS`` slots ahead of the local one. A request returns the newest ``limit`` rows, so a slot
``g`` is reached when ``limit >= (F(now) - g) / tf + 1 + REACH_MARGIN_SLOTS``.

Statuses never say "complete": ``WINDOW_NO_GAPS`` means only that the classified span has no missing and no stale
slot, and ``CONTIGUOUS_FROM_OLDEST`` means no unaccounted hole above the oldest stored candle.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from quasar_market_data.domain.timed_coverage import ExpectationRule, GapRun, SlotState, classify_span_at
from quasar_market_data.domain.timestamps import MIN_TIMESTAMP_MS, ensure_timestamp_ms

REACH_MARGIN_SLOTS: Final = 2
_MAX_LARGEST_RUNS: Final = 5
_ABSENT: Final = frozenset({SlotState.MISSING, SlotState.DUE, SlotState.STALE})


class WindowStatus(StrEnum):
    NO_BASELINE = "no_baseline"
    NOTHING_EXPECTED = "nothing_expected"
    TARGET_OUT_OF_REACH = "target_out_of_reach"
    WINDOW_NOT_ASSESSED = "window_not_assessed"
    WINDOW_GAPS = "window_gaps"
    WINDOW_NO_GAPS = "window_no_gaps"


class HistoryStatus(StrEnum):
    UNKNOWN = "unknown"
    HOLES = "holes"
    CONTIGUOUS_FROM_OLDEST = "contiguous_from_oldest"


@dataclass(frozen=True)
class SeriesFacts:
    """Stored facts of one series; the three sets are limited to the window."""

    oldest_open_ms: int
    newest_open_ms: int
    candles: frozenset[int]
    conflicts: frozenset[int]
    unavailable: frozenset[int]


@dataclass(frozen=True)
class HistoryFacts:
    """Series-wide hole aggregate; ``largest_runs`` are raw ``[start, end)`` in ms, at most five."""

    raw_hole_slots: int
    raw_hole_slots_older_than_window: int
    hole_runs: int
    largest_runs: tuple[tuple[int, int], ...]
    unavailable_in_holes: int
    unavailable_in_holes_older_than_window: int
    conflicted_slots: int


@dataclass(frozen=True)
class SeriesReconcile:
    window: WindowStatus
    all_present: bool
    clock_behind_stored: bool
    present: int
    slots_classified: int
    missing: int
    due: int
    stale: int
    conflicted: int
    unavailable: int
    absent_runs: tuple[GapRun, ...]
    oldest_wanted_ms: int | None
    catch_up_limit_at_read: int
    unreachable_tail_slots: int
    history: HistoryStatus
    history_unaccounted_holes: int
    history_holes_older_than_window: int
    hole_runs: int
    largest_hole_runs: tuple[tuple[int, int], ...]
    history_conflicted_slots: int
    unknown_before_ms: int | None
    needs_s16_slots: int


def _int(value: int, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value).__name__}")


def _positive(value: int, name: str) -> None:
    _int(value, name)
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")


def _non_negative(value: int, name: str) -> None:
    _int(value, name)
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")


def _aligned(value: int, timeframe_ms: int, name: str) -> None:
    ensure_timestamp_ms(value, name)
    if value % timeframe_ms != 0:
        raise ValueError(f"{name} {value} is not aligned to the timeframe")


def _check_max_limit(max_limit: int) -> None:
    _int(max_limit, "max_limit")
    if max_limit <= REACH_MARGIN_SLOTS + 1:
        raise ValueError(f"max_limit must be > {REACH_MARGIN_SLOTS + 1}, got {max_limit}")


def max_target_distance_slots(publication_delay_ms: int, timeframe_ms: int) -> int:
    """The largest ``(F(w) - T) / tf`` over a cycle: ``ceil(delay / tf) + 1``."""
    _non_negative(publication_delay_ms, "publication_delay_ms")
    _positive(timeframe_ms, "timeframe_ms")
    return -(-publication_delay_ms // timeframe_ms) + 1


def steady_state_limit_needed(publication_delay_ms: int, timeframe_ms: int) -> int:
    """The smallest request limit with which a round can always reach its target."""
    return max_target_distance_slots(publication_delay_ms, timeframe_ms) + 1 + REACH_MARGIN_SLOTS


def attempt_limit(
    *, now_ms: int, oldest_wanted_ms: int | None, timeframe_ms: int, default_limit: int, max_limit: int
) -> int:
    """``min(max_limit, max(default_limit, (F(now) - g) / tf + 1 + margin))``; ``default_limit`` for no ``g``."""
    ensure_timestamp_ms(now_ms, "now_ms")
    _positive(timeframe_ms, "timeframe_ms")
    _positive(default_limit, "default_limit")
    _check_max_limit(max_limit)
    if oldest_wanted_ms is None:
        return default_limit
    _aligned(oldest_wanted_ms, timeframe_ms, "oldest_wanted_ms")
    floor_now = now_ms - now_ms % timeframe_ms
    if oldest_wanted_ms > floor_now:
        raise ValueError(f"oldest_wanted_ms {oldest_wanted_ms} is newer than the current slot {floor_now}")
    needed = (floor_now - oldest_wanted_ms) // timeframe_ms + 1 + REACH_MARGIN_SLOTS
    return min(max_limit, max(default_limit, needed))


def clock_lead_slots(server_forming_open_ms: int, local_wall_ms: int, timeframe_ms: int) -> int:
    """How many slots the server's forming slot is ahead of the local one (negative: behind)."""
    _positive(timeframe_ms, "timeframe_ms")
    _aligned(server_forming_open_ms, timeframe_ms, "server_forming_open_ms")
    ensure_timestamp_ms(local_wall_ms, "local_wall_ms")
    return (server_forming_open_ms - (local_wall_ms - local_wall_ms % timeframe_ms)) // timeframe_ms


def reconcile_window(as_of_ms: int, target_open_ms: int, timeframe_ms: int, max_limit: int) -> tuple[int, int] | None:
    """The reachable window ``[ws, T + tf)``, or ``None`` when it is empty (``ws > T``)."""
    ensure_timestamp_ms(as_of_ms, "as_of_ms")
    _positive(timeframe_ms, "timeframe_ms")
    _check_max_limit(max_limit)
    _aligned(target_open_ms, timeframe_ms, "target_open_ms")
    floor_as_of = as_of_ms - as_of_ms % timeframe_ms
    if target_open_ms > floor_as_of - timeframe_ms:
        raise ValueError(f"target_open_ms {target_open_ms} is newer than the last closed slot of as_of_ms")
    start = max(MIN_TIMESTAMP_MS, floor_as_of - (max_limit - 1 - REACH_MARGIN_SLOTS) * timeframe_ms)
    if start > target_open_ms:
        return None
    return start, target_open_ms + timeframe_ms


def _check_history(h: HistoryFacts) -> None:
    for name in (
        "raw_hole_slots",
        "raw_hole_slots_older_than_window",
        "hole_runs",
        "unavailable_in_holes",
        "unavailable_in_holes_older_than_window",
        "conflicted_slots",
    ):
        _non_negative(getattr(h, name), name)
    if h.unavailable_in_holes > h.raw_hole_slots:
        raise ValueError("unavailable_in_holes exceeds raw_hole_slots")
    if h.raw_hole_slots_older_than_window > h.raw_hole_slots:
        raise ValueError("raw_hole_slots_older_than_window exceeds raw_hole_slots")
    if h.unavailable_in_holes_older_than_window > h.unavailable_in_holes:
        raise ValueError("unavailable_in_holes_older_than_window exceeds unavailable_in_holes")
    if h.unavailable_in_holes_older_than_window > h.raw_hole_slots_older_than_window:
        raise ValueError("unavailable_in_holes_older_than_window exceeds raw_hole_slots_older_than_window")
    if h.hole_runs > h.raw_hole_slots:
        raise ValueError("hole_runs exceeds raw_hole_slots")
    if len(h.largest_runs) > min(_MAX_LARGEST_RUNS, h.hole_runs):
        raise ValueError("largest_runs has more entries than allowed")
    for start, end in h.largest_runs:
        _int(start, "run start")
        _int(end, "run end")
        if start >= end:
            raise ValueError(f"hole run [{start}, {end}) is empty")


def _check_facts(f: SeriesFacts, timeframe_ms: int, window: tuple[int, int] | None) -> None:
    _aligned(f.oldest_open_ms, timeframe_ms, "oldest_open_ms")
    _aligned(f.newest_open_ms, timeframe_ms, "newest_open_ms")
    if f.oldest_open_ms > f.newest_open_ms:
        raise ValueError("oldest_open_ms is newer than newest_open_ms")
    for name, members in (("candles", f.candles), ("conflicts", f.conflicts), ("unavailable", f.unavailable)):
        for member in members:
            _aligned(member, timeframe_ms, f"{name} member")
            if window is not None and not window[0] <= member < window[1]:
                raise ValueError(f"{name} member {member} lies outside the window {window}")


def reconcile_series(
    facts: SeriesFacts | None,
    history: HistoryFacts | None,
    *,
    as_of_ms: int,
    target_open_ms: int | None,
    timeframe_ms: int,
    rule: ExpectationRule,
    default_limit: int,
    max_limit: int,
) -> SeriesReconcile:
    """Classify one series' window and history from stored facts. ``None`` facts: no stored candle."""
    ensure_timestamp_ms(as_of_ms, "as_of_ms")
    _positive(timeframe_ms, "timeframe_ms")
    _positive(default_limit, "default_limit")
    _check_max_limit(max_limit)
    if not isinstance(rule, ExpectationRule):
        raise TypeError(f"rule must be ExpectationRule, got {type(rule).__name__}")
    window = None
    if target_open_ms is not None:
        window = reconcile_window(as_of_ms, target_open_ms, timeframe_ms, max_limit)
    if history is not None:
        _check_history(history)
    if facts is not None:
        _check_facts(facts, timeframe_ms, window)

    # History part, independent of the window.
    h_status = HistoryStatus.UNKNOWN
    unaccounted = old_unaccounted = h_runs = h_conflicted = 0
    largest: tuple[tuple[int, int], ...] = ()
    if facts is not None and history is not None:
        unaccounted = history.raw_hole_slots - history.unavailable_in_holes
        old_unaccounted = history.raw_hole_slots_older_than_window - history.unavailable_in_holes_older_than_window
        h_runs = history.hole_runs
        largest = history.largest_runs
        h_conflicted = history.conflicted_slots
        h_status = HistoryStatus.HOLES if unaccounted > 0 else HistoryStatus.CONTIGUOUS_FROM_OLDEST

    def build(
        status: WindowStatus,
        *,
        cov: tuple[int, int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0, 0),
        runs: tuple[GapRun, ...] = (),
        wanted: int | None = None,
        tail: int = 0,
        behind: bool = False,
    ) -> SeriesReconcile:
        slots, present, missing, due, stale, conflicted, unavailable = cov
        return SeriesReconcile(
            window=status,
            all_present=slots > 0 and present == slots,
            clock_behind_stored=behind,
            present=present,
            slots_classified=slots,
            missing=missing,
            due=due,
            stale=stale,
            conflicted=conflicted,
            unavailable=unavailable,
            absent_runs=runs,
            oldest_wanted_ms=wanted,
            catch_up_limit_at_read=attempt_limit(
                now_ms=as_of_ms,
                oldest_wanted_ms=wanted,
                timeframe_ms=timeframe_ms,
                default_limit=default_limit,
                max_limit=max_limit,
            ),
            unreachable_tail_slots=tail,
            history=h_status,
            history_unaccounted_holes=unaccounted,
            history_holes_older_than_window=old_unaccounted,
            hole_runs=h_runs,
            largest_hole_runs=largest,
            history_conflicted_slots=h_conflicted,
            unknown_before_ms=None if facts is None else facts.oldest_open_ms,
            needs_s16_slots=tail + old_unaccounted,
        )

    if target_open_ms is None:
        return build(WindowStatus.NOTHING_EXPECTED, behind=_behind(facts, timeframe_ms, rule, as_of_ms))
    if facts is None:
        return build(WindowStatus.NO_BASELINE)
    behind = _behind(facts, timeframe_ms, rule, as_of_ms)
    if window is None:
        return build(WindowStatus.TARGET_OUT_OF_REACH, behind=behind)
    ws, end = window
    start = max(ws, facts.oldest_open_ms)
    if start >= end:
        return build(WindowStatus.WINDOW_NOT_ASSESSED, behind=behind)

    cov = classify_span_at(
        start=start,
        end=end,
        timeframe_ms=timeframe_ms,
        candles=facts.candles,
        conflicts=facts.conflicts,
        unavailable=facts.unavailable,
        newest_open_time_ms=facts.newest_open_ms,
        as_of_ms=as_of_ms,
        rule=rule,
    )
    runs = tuple(r for r in cov.runs if r.state in _ABSENT)
    tail = (ws - facts.newest_open_ms) // timeframe_ms - 1 if facts.newest_open_ms < ws else 0
    status = WindowStatus.WINDOW_GAPS if cov.missing + cov.stale > 0 else WindowStatus.WINDOW_NO_GAPS
    return build(
        status,
        cov=(cov.slots, cov.present, cov.missing, cov.due, cov.stale, cov.conflicted, cov.unavailable),
        runs=runs,
        wanted=runs[0].start_ms if runs else None,
        tail=tail,
        behind=behind,
    )


def _behind(facts: SeriesFacts | None, timeframe_ms: int, rule: ExpectationRule, as_of_ms: int) -> bool:
    if facts is None:
        return False
    return facts.newest_open_ms + timeframe_ms + rule.publication_delay_ms > as_of_ms
