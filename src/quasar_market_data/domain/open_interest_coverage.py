"""Open interest slot states, page counts and series freshness (ADR-0003 P-7, P-10; contract api-v1 6.5).

A slot is one ``time_ms``, the **end** of a period (ADR-0003 P-3). For a slot ``T`` at ``as_of_ms`` the first
matching state wins:

1. ``conflicted``: a conflict record exists for ``T``;
2. ``present``: a row is stored for ``T`` (stored facts win over the clock);
3. ``not_yet_expected``: ``T + expected_after_ms > as_of_ms``;
4. ``no_longer_recoverable``: ``T < as_of_ms - retrieval_horizon_ms`` and no row;
5. ``due`` / ``stale``: no row, ``T`` is after the newest stored ``time_ms``; ``due`` while
   ``as_of_ms <= T + expected_after_ms + lag_threshold_ms``, ``stale`` after;
6. ``missing``: none of the above (no row, ``T`` before the newest stored ``time_ms``).

Unlike candle coverage, the clock also refines a slot *before* the newest row (state 3). The domain reads no clock;
``as_of_ms`` is an input. Integer arithmetic only.
"""

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import StrEnum

from quasar_market_data.domain.freshness import Freshness, SeriesFreshness
from quasar_market_data.domain.open_interest_cadence import MAX_CADENCE, open_interest_cadence
from quasar_market_data.domain.open_interest_request import RETRIEVAL_HORIZON_MS
from quasar_market_data.domain.open_interest_schedule import expected_after_ms
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, ensure_timestamp_ms


class OpenInterestSlotState(StrEnum):
    PRESENT = "present"
    CONFLICTED = "conflicted"
    NOT_YET_EXPECTED = "not_yet_expected"
    NO_LONGER_RECOVERABLE = "no_longer_recoverable"
    DUE = "due"
    STALE = "stale"
    MISSING = "missing"


def _ensure_int(value: int, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class OpenInterestRule:
    """What the states are computed with: the cadence, when a slot is expected, the lag tolerance and the horizon.

    ``expected_after_ms`` is *not* re-derived from ``cadence`` here: ``open_interest_rule`` derives both from the
    configuration with the Collector's own functions.
    """

    cadence: int
    expected_after_ms: int
    lag_threshold_ms: int
    period: Timeframe
    retrieval_horizon_ms: int = RETRIEVAL_HORIZON_MS

    def __post_init__(self) -> None:
        cadence = _ensure_int(self.cadence, "cadence")
        if not 1 <= cadence <= MAX_CADENCE:
            raise ValueError(f"cadence must be within [1, {MAX_CADENCE}], got {cadence}")
        for name in ("expected_after_ms", "lag_threshold_ms", "retrieval_horizon_ms"):
            value = _ensure_int(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} must be >= 0, got {value}")
        if type(self.period) is not Timeframe:
            raise TypeError(f"period must be Timeframe, got {type(self.period).__name__}")


def open_interest_rule(
    symbol_count: int, budget_per_5min: int, round_offset_ms: int, lag_threshold_ms: int, period: Timeframe
) -> OpenInterestRule:
    """The rule for a configuration; ``CadenceError`` when the symbol count needs a cadence above 12."""
    cadence = open_interest_cadence(symbol_count, budget_per_5min)
    return OpenInterestRule(cadence, expected_after_ms(cadence, round_offset_ms, period), lag_threshold_ms, period)


@dataclass(frozen=True)
class OpenInterestCoverage:
    slots: int
    present: int
    conflicted: int
    not_yet_expected: int
    no_longer_recoverable: int
    due: int
    stale: int
    missing: int
    conflicted_times: frozenset[int]

    @property
    def gap_free(self) -> bool:
        return self.present == self.slots


def _check_rule(rule: OpenInterestRule) -> None:
    if type(rule) is not OpenInterestRule:
        raise TypeError(f"rule must be OpenInterestRule, got {type(rule).__name__}")


def open_interest_slot_states(
    *,
    start: int,
    end: int,
    stored: AbstractSet[int],
    conflicts: AbstractSet[int],
    newest_time_ms: int,
    as_of_ms: int,
    rule: OpenInterestRule,
) -> tuple[tuple[int, OpenInterestSlotState], ...]:
    """Return ``(time_ms, state)`` for every slot of ``[start, end)`` at ``as_of_ms``, ascending.

    ``stored`` and ``conflicts`` hold the ``time_ms`` of the rows and of the conflict records inside the span;
    ``newest_time_ms`` is the newest stored ``time_ms`` of the whole series (it may lie outside the span, and
    must be in ``stored`` when inside it). Raises ``TypeError`` or ``ValueError`` for wrong types, misaligned or
    out-of-range inputs and inconsistent facts, so a caller bug never becomes a wrong count.
    """
    _check_rule(rule)
    step = rule.period.milliseconds
    as_of = ensure_timestamp_ms(as_of_ms, "as_of_ms")
    first = ensure_timestamp_ms(start, "start")
    last = _ensure_int(end, "end")
    newest = ensure_timestamp_ms(newest_time_ms, "newest_time_ms")
    for name, value in (("start", first), ("end", last), ("newest_time_ms", newest)):
        if value % step:
            raise ValueError(f"{name} {value} is not aligned to {rule.period.label}")
    if last <= first:
        raise ValueError(f"the span must not be empty, got [{first}, {last})")
    if last > MAX_TIMESTAMP_MS + step:
        raise ValueError(f"end must be at most {MAX_TIMESTAMP_MS + step}, got {last}")
    for name, members in (("stored", stored), ("conflicts", conflicts)):
        for time_ms in members:
            ensure_timestamp_ms(time_ms, f"{name} member")
            if time_ms % step:
                raise ValueError(f"{name} contains the unaligned time {time_ms}")
            if not first <= time_ms < last:
                raise ValueError(f"{name} contains {time_ms}, outside [{first}, {last})")
    for time_ms in stored:
        if time_ms > newest:
            raise ValueError(f"stored contains {time_ms}, newer than newest_time_ms {newest}")
    if first <= newest < last and newest not in stored:
        raise ValueError(f"newest_time_ms {newest} is inside the span but not in stored")

    horizon_start = as_of - rule.retrieval_horizon_ms
    states: list[tuple[int, OpenInterestSlotState]] = []
    for time_ms in range(first, last, step):
        expected_at = time_ms + rule.expected_after_ms
        if time_ms in conflicts:
            state = OpenInterestSlotState.CONFLICTED
        elif time_ms in stored:
            state = OpenInterestSlotState.PRESENT
        elif expected_at > as_of:
            state = OpenInterestSlotState.NOT_YET_EXPECTED
        elif time_ms < horizon_start:
            state = OpenInterestSlotState.NO_LONGER_RECOVERABLE
        elif time_ms > newest:
            late = as_of <= expected_at + rule.lag_threshold_ms
            state = OpenInterestSlotState.DUE if late else OpenInterestSlotState.STALE
        else:
            state = OpenInterestSlotState.MISSING
        states.append((time_ms, state))
    return tuple(states)


def _count(states: tuple[tuple[int, OpenInterestSlotState], ...], slots: int) -> OpenInterestCoverage:
    counts: dict[OpenInterestSlotState, int] = dict.fromkeys(OpenInterestSlotState, 0)
    conflicted_times: set[int] = set()
    for time_ms, state in states:
        counts[state] += 1
        if state is OpenInterestSlotState.CONFLICTED:
            conflicted_times.add(time_ms)
    return OpenInterestCoverage(
        slots=slots,
        present=counts[OpenInterestSlotState.PRESENT],
        conflicted=counts[OpenInterestSlotState.CONFLICTED],
        not_yet_expected=counts[OpenInterestSlotState.NOT_YET_EXPECTED],
        no_longer_recoverable=counts[OpenInterestSlotState.NO_LONGER_RECOVERABLE],
        due=counts[OpenInterestSlotState.DUE],
        stale=counts[OpenInterestSlotState.STALE],
        missing=counts[OpenInterestSlotState.MISSING],
        conflicted_times=frozenset(conflicted_times),
    )


def classify_open_interest_span(
    *,
    start: int,
    end: int,
    stored: AbstractSet[int],
    conflicts: AbstractSet[int],
    newest_time_ms: int,
    as_of_ms: int,
    rule: OpenInterestRule,
) -> OpenInterestCoverage:
    """Count the states of the slots of ``[start, end)`` (see ``open_interest_slot_states``)."""
    states = open_interest_slot_states(
        start=start,
        end=end,
        stored=stored,
        conflicts=conflicts,
        newest_time_ms=newest_time_ms,
        as_of_ms=as_of_ms,
        rule=rule,
    )
    return _count(states, (end - start) // rule.period.milliseconds)


@dataclass(frozen=True)
class OpenInterestRun:
    """A maximal run of consecutive slots in one state other than ``present``; ``end_ms`` is exclusive."""

    state: OpenInterestSlotState
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class OpenInterestRunCoverage:
    """The counts and the runs of one slot sequence: they describe the same slots, so they always agree."""

    coverage: OpenInterestCoverage
    runs: tuple[OpenInterestRun, ...]


def _group_runs(states: tuple[tuple[int, OpenInterestSlotState], ...], step: int) -> tuple[OpenInterestRun, ...]:
    runs: list[OpenInterestRun] = []
    for time_ms, state in states:
        if state is OpenInterestSlotState.PRESENT:
            continue
        if runs and runs[-1].state is state and runs[-1].end_ms == time_ms:
            runs[-1] = OpenInterestRun(state, runs[-1].start_ms, time_ms + step)
        else:
            runs.append(OpenInterestRun(state, time_ms, time_ms + step))
    return tuple(runs)


def classify_open_interest_runs(
    *,
    start: int,
    end: int,
    stored: AbstractSet[int],
    conflicts: AbstractSet[int],
    newest_time_ms: int,
    as_of_ms: int,
    rule: OpenInterestRule,
) -> OpenInterestRunCoverage:
    """Counts and runs of ``[start, end)`` from one call of ``open_interest_slot_states`` (contract 6.6)."""
    states = open_interest_slot_states(
        start=start,
        end=end,
        stored=stored,
        conflicts=conflicts,
        newest_time_ms=newest_time_ms,
        as_of_ms=as_of_ms,
        rule=rule,
    )
    step = rule.period.milliseconds
    return OpenInterestRunCoverage(_count(states, (end - start) // step), _group_runs(states, step))


def open_interest_freshness(newest_time_ms: int, as_of_ms: int, rule: OpenInterestRule) -> SeriesFreshness:
    """Judge a series whose newest stored row has ``time_ms = newest_time_ms`` at ``as_of_ms`` (contract 6.5.8).

    ``T_e`` is the newest slot expected at ``as_of_ms``. The series is ``current`` when it holds ``T_e`` or a newer
    row; otherwise ``x = newest + period + expected_after_ms`` is when the slot after the newest row was expected:
    ``due`` while ``as_of_ms <= x + lag_threshold_ms``, ``stale`` after. ``lag_slots = (T_e - newest) / period``.
    """
    _check_rule(rule)
    step = rule.period.milliseconds
    newest = ensure_timestamp_ms(newest_time_ms, "newest_time_ms")
    as_of = ensure_timestamp_ms(as_of_ms, "as_of_ms")
    if newest % step:
        raise ValueError(f"newest_time_ms {newest} is not aligned to {rule.period.label}")
    newest_expected = (as_of - rule.expected_after_ms) // step * step
    if newest >= newest_expected:
        return SeriesFreshness(Freshness.CURRENT, 0)
    expected_at = newest + step + rule.expected_after_ms
    state = Freshness.DUE if as_of <= expected_at + rule.lag_threshold_ms else Freshness.STALE
    return SeriesFreshness(state, (newest_expected - newest) // step)
