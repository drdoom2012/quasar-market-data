"""Slot states of a span at a given ``as_of``: the clock-free states plus not_yet_expected, due and stale."""

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from quasar_market_data.domain.span_coverage import span_slot_states
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, ensure_timestamp_ms

DEFAULT_LAG_THRESHOLD_MS: Final = 60_000


class SlotState(StrEnum):
    PRESENT = "present"
    CONFLICTED = "conflicted"
    UNAVAILABLE = "unavailable"
    MISSING = "missing"
    NOT_YET_EXPECTED = "not_yet_expected"
    DUE = "due"
    STALE = "stale"


@dataclass(frozen=True)
class ExpectationRule:
    """When a slot after the newest stored candle is expected, and how long a late one is tolerated.

    ``lag_threshold_ms`` is only required to be a non-negative ``int``; a very large value keeps every slot
    ``due`` and never ``stale``, so S12/S13 must bound the configured value.
    """

    publication_delay_ms: int
    lag_threshold_ms: int = DEFAULT_LAG_THRESHOLD_MS

    def __post_init__(self) -> None:
        for name, value in (
            ("publication_delay_ms", self.publication_delay_ms),
            ("lag_threshold_ms", self.lag_threshold_ms),
        ):
            if type(value) is not int:
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
            if value < 0:
                raise ValueError(f"{name} must be >= 0, got {value}")


@dataclass(frozen=True)
class GapRun:
    state: SlotState
    start_ms: int
    end_ms: int  # exclusive


@dataclass(frozen=True)
class TimedCoverage:
    """Counts and runs for a span at ``as_of_ms``.

    ``stored_ahead_of_as_of`` is ``newest_open_time_ms + timeframe_ms + publication_delay_ms > as_of_ms``: a
    statement about the inputs, not a diagnosis. The cause may be a reader clock behind the Collector's, a
    deliberately historical ``as_of`` or a different publication delay. It never changes a classification.
    When it is true every ``beyond_stored`` slot is necessarily ``not_yet_expected`` (its ``expected_at`` is at
    least the newest end plus the delay, which is after ``as_of``), so ``due == stale == 0``.
    """

    as_of_ms: int
    rule: ExpectationRule
    slots: int
    present: int
    conflicted: int
    unavailable: int
    missing: int
    not_yet_expected: int
    due: int
    stale: int
    stored_ahead_of_as_of: bool
    runs: tuple[GapRun, ...]

    @property
    def fully_present(self) -> bool:
        return self.present == self.slots


def _ensure_int(value: int, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value).__name__}")


def classify_span_at(
    *,
    start: int,
    end: int,
    timeframe_ms: int,
    candles: AbstractSet[int],
    conflicts: AbstractSet[int],
    unavailable: AbstractSet[int],
    newest_open_time_ms: int,
    as_of_ms: int,
    rule: ExpectationRule,
) -> TimedCoverage:
    """Count the states of the slots of ``[start, end)`` at ``as_of_ms``.

    Stored facts win over the clock: only a slot after the newest stored candle with no record is refined, by
    decision 1 of the S11 plan: ``as_of < expected_at`` is ``not_yet_expected``, up to ``expected_at + lag``
    ``due``, later ``stale``, where ``expected_at = slot end + publication delay``.
    Raises ``TypeError`` or ``ValueError`` for wrong types, out-of-range or inconsistent inputs (see
    ``span_slot_states`` for the span rules).
    """
    if not isinstance(rule, ExpectationRule):
        raise TypeError(f"rule must be ExpectationRule, got {type(rule).__name__}")
    ensure_timestamp_ms(as_of_ms, "as_of_ms")
    ensure_timestamp_ms(start, "start")
    ensure_timestamp_ms(newest_open_time_ms, "newest_open_time_ms")
    _ensure_int(end, "end")
    _ensure_int(timeframe_ms, "timeframe_ms")
    # Members are validated here, before span_slot_states: an aligned float such as 1792643400000.0 passes its
    # ``%`` alignment test, so only this check rejects it.
    for name, members in (("candles", candles), ("conflicts", conflicts), ("unavailable", unavailable)):
        for open_time in members:
            ensure_timestamp_ms(open_time, f"{name} member")
    # MAX_TIMESTAMP_MS + timeframe_ms is the exclusive end of the last valid slot (settles follow-up F-1 for the
    # domain).
    if timeframe_ms > 0 and end > MAX_TIMESTAMP_MS + timeframe_ms:
        raise ValueError(f"end must be at most {MAX_TIMESTAMP_MS + timeframe_ms}, got {end}")
    if timeframe_ms > 0 and newest_open_time_ms % timeframe_ms != 0:
        raise ValueError(f"newest_open_time_ms {newest_open_time_ms} is not aligned to the timeframe")
    if start <= newest_open_time_ms < end and newest_open_time_ms not in candles:
        raise ValueError(f"newest_open_time_ms {newest_open_time_ms} is inside the span but not in candles")
    # conflicts and unavailable records may lie after the newest candle; a candle may not.
    for open_time in candles:
        if open_time > newest_open_time_ms:
            raise ValueError(f"candles contains {open_time}, newer than newest_open_time_ms {newest_open_time_ms}")

    counts: dict[SlotState, int] = dict.fromkeys(SlotState, 0)
    runs: list[GapRun] = []
    for open_time, stored in span_slot_states(
        start=start,
        end=end,
        timeframe_ms=timeframe_ms,
        candles=candles,
        conflicts=conflicts,
        unavailable=unavailable,
        newest_open_time_ms=newest_open_time_ms,
    ):
        if stored != "beyond_stored":
            state = SlotState(stored)
        else:
            expected_at = open_time + timeframe_ms + rule.publication_delay_ms
            if as_of_ms < expected_at:
                state = SlotState.NOT_YET_EXPECTED
            elif as_of_ms <= expected_at + rule.lag_threshold_ms:
                state = SlotState.DUE
            else:
                state = SlotState.STALE
        counts[state] += 1
        if state is SlotState.PRESENT:
            continue
        if runs and runs[-1].state is state and runs[-1].end_ms == open_time:
            runs[-1] = GapRun(state, runs[-1].start_ms, open_time + timeframe_ms)
        else:
            runs.append(GapRun(state, open_time, open_time + timeframe_ms))

    return TimedCoverage(
        as_of_ms=as_of_ms,
        rule=rule,
        slots=(end - start) // timeframe_ms,
        present=counts[SlotState.PRESENT],
        conflicted=counts[SlotState.CONFLICTED],
        unavailable=counts[SlotState.UNAVAILABLE],
        missing=counts[SlotState.MISSING],
        not_yet_expected=counts[SlotState.NOT_YET_EXPECTED],
        due=counts[SlotState.DUE],
        stale=counts[SlotState.STALE],
        stored_ahead_of_as_of=newest_open_time_ms + timeframe_ms + rule.publication_delay_ms > as_of_ms,
        runs=tuple(runs),
    )
