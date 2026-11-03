"""Open interest runs and counts from one slot sequence (contract api-v1 6.6; design 5, formal model P1 to P8)."""

import random
from dataclasses import dataclass

import pytest

from quasar_market_data.domain.open_interest_coverage import (
    OpenInterestCoverage,
    OpenInterestRule,
    OpenInterestRun,
    OpenInterestSlotState,
    classify_open_interest_runs,
    classify_open_interest_span,
)
from quasar_market_data.domain.open_interest_request import RETRIEVAL_HORIZON_MS
from quasar_market_data.domain.timeframe import Timeframe

P5 = Timeframe("5m")
P = 300_000
E = 500_000  # c = 1, round_offset_ms = 200 000
L = 60_000
H = RETRIEVAL_HORIZON_MS
RULE = OpenInterestRule(cadence=1, expected_after_ms=E, lag_threshold_ms=L, period=P5)
T0 = 1_792_672_500_000  # the first slot of the contract example (6.5.4)
AS_OF = T0 + 7 * P

S = OpenInterestSlotState
_ORDER = {S.NO_LONGER_RECOVERABLE: 0, S.MISSING: 1, S.STALE: 2, S.DUE: 3, S.NOT_YET_EXPECTED: 4}


def runs_of(
    start: int,
    end: int,
    *,
    stored: tuple[int, ...] = (),
    conflicts: tuple[int, ...] = (),
    newest: int,
    as_of: int = AS_OF,
    rule: OpenInterestRule = RULE,
) -> tuple[OpenInterestRun, ...]:
    return classify_open_interest_runs(
        start=start,
        end=end,
        stored=set(stored),
        conflicts=set(conflicts),
        newest_time_ms=newest,
        as_of_ms=as_of,
        rule=rule,
    ).runs


def test_the_contract_example_runs() -> None:
    result = classify_open_interest_runs(
        start=T0,
        end=T0 + 7 * P,
        stored={T0, T0 + 3 * P},
        conflicts={T0 + P},
        newest_time_ms=T0 + 3 * P,
        as_of_ms=1_792_674_530_000,
        rule=RULE,
    )

    assert result.runs == (
        OpenInterestRun(S.CONFLICTED, T0 + P, T0 + 2 * P),
        OpenInterestRun(S.MISSING, T0 + 2 * P, T0 + 3 * P),
        OpenInterestRun(S.STALE, T0 + 4 * P, T0 + 5 * P),
        OpenInterestRun(S.DUE, T0 + 5 * P, T0 + 6 * P),
        OpenInterestRun(S.NOT_YET_EXPECTED, T0 + 6 * P, T0 + 7 * P),
    )
    assert result.coverage.present == 2
    assert result.coverage.slots == 7


def test_touching_runs_of_different_states_stay_separate() -> None:
    # slot 0 conflicted, slot 1 missing (the row at slot 2 is newer), slot 2 present
    runs = runs_of(T0, T0 + 3 * P, stored=(T0 + 2 * P,), conflicts=(T0,), newest=T0 + 2 * P)

    assert runs == (OpenInterestRun(S.CONFLICTED, T0, T0 + P), OpenInterestRun(S.MISSING, T0 + P, T0 + 2 * P))


def test_slots_of_one_state_without_a_gap_form_one_run() -> None:
    runs = runs_of(T0, T0 + 5 * P, stored=(T0, T0 + 4 * P), newest=T0 + 4 * P)

    assert len(runs) == 1
    assert runs[0].state is S.MISSING
    assert runs[0].end_ms - runs[0].start_ms == 3 * P


def test_two_runs_of_one_state_separated_by_a_present_slot_stay_two_runs() -> None:
    runs = runs_of(T0, T0 + 5 * P, stored=(T0 + 2 * P, T0 + 4 * P), newest=T0 + 4 * P)

    assert runs == (OpenInterestRun(S.MISSING, T0, T0 + 2 * P), OpenInterestRun(S.MISSING, T0 + 3 * P, T0 + 4 * P))


@dataclass(frozen=True)
class Case:
    start: int
    end: int
    stored: frozenset[int]
    conflicts: frozenset[int]
    newest: int
    oldest: int
    as_of: int
    rule: OpenInterestRule


def random_case(rng: random.Random) -> Case:
    """A series with rows between slot ``o`` and ``n`` (both rows), a window that may reach before and after them."""
    base = T0 + 40 * P  # keeps every index positive
    o = rng.randint(0, 30)
    n = o + rng.randint(0, 30)
    rows = {o, n} | {i for i in range(o, n + 1) if rng.random() < 0.5}
    first = rng.randint(-5, 35)
    last = first + rng.randint(1, 40)
    inside = range(first, last)
    stored = {i for i in rows if first <= i < last}
    conflicts = {i for i in inside if rng.random() < 0.15}
    as_of = base + rng.randint(0, 70) * P + rng.randint(0, P - 1) - 20 * P
    horizon = rng.choice([H, 5 * P, 12 * P, 30 * P])
    rule = OpenInterestRule(cadence=1, expected_after_ms=E, lag_threshold_ms=L, period=P5, retrieval_horizon_ms=horizon)
    return Case(
        start=base + first * P,
        end=base + last * P,
        stored=frozenset(base + i * P for i in stored),
        conflicts=frozenset(base + i * P for i in conflicts),
        newest=base + n * P,
        oldest=base + o * P,
        as_of=as_of,
        rule=rule,
    )


def classify(case: Case) -> tuple[OpenInterestCoverage, tuple[OpenInterestRun, ...]]:
    result = classify_open_interest_runs(
        start=case.start,
        end=case.end,
        stored=case.stored,
        conflicts=case.conflicts,
        newest_time_ms=case.newest,
        as_of_ms=case.as_of,
        rule=case.rule,
    )
    return result.coverage, result.runs


def test_counts_equal_classify_open_interest_span_and_the_run_sums() -> None:
    rng = random.Random(20261103)
    for _ in range(500):
        case = random_case(rng)
        coverage, runs = classify(case)

        assert coverage == classify_open_interest_span(
            start=case.start,
            end=case.end,
            stored=case.stored,
            conflicts=case.conflicts,
            newest_time_ms=case.newest,
            as_of_ms=case.as_of,
            rule=case.rule,
        )
        run_slots = {state: 0 for state in S}
        for run in runs:
            assert run.state is not S.PRESENT
            assert run.start_ms < run.end_ms
            assert (run.end_ms - run.start_ms) % P == 0
            run_slots[run.state] += (run.end_ms - run.start_ms) // P
        for state in S:
            if state is not S.PRESENT:
                assert run_slots[state] == getattr(coverage, state.value)
        assert coverage.present == coverage.slots - sum(run_slots.values())


def run_containing(runs: tuple[OpenInterestRun, ...], time_ms: int) -> OpenInterestRun | None:
    for run in runs:
        if run.start_ms <= time_ms < run.end_ms:
            return run
    return None


def test_the_slot_model_properties_hold_for_random_windows() -> None:
    rng = random.Random(20261104)
    for _ in range(2000):
        case = random_case(rng)
        _, runs = classify(case)
        o, n = case.oldest, case.newest

        for edge in (o, n):  # P1
            run = run_containing(runs, edge)
            if run is not None:
                assert run.state is S.CONFLICTED
        for time_ms in range(case.start, min(o, case.end), P):  # P2
            run = run_containing(runs, time_ms)
            assert run is not None
            assert run.state not in (S.DUE, S.STALE)
        for time_ms in range(max(n + P, case.start), case.end, P):  # P3
            run = run_containing(runs, time_ms)
            assert run is not None
            assert run.state is not S.MISSING
        for run in runs:  # P4
            if run.state is S.MISSING:
                assert run.end_ms <= n
            if run.state in (S.DUE, S.STALE):
                assert run.start_ms > n
        ordered = [_ORDER[run.state] for run in runs if run.state is not S.CONFLICTED]  # P8
        assert ordered == sorted(ordered)


def test_a_conflict_without_a_row_is_conflicted_before_o_between_and_after_n() -> None:
    o, n = T0 + 2 * P, T0 + 5 * P
    conflicts = (o - P, o + P, n + P)  # none has a row

    result = classify_open_interest_runs(
        start=T0,
        end=T0 + 8 * P,
        stored={o, n},
        conflicts=set(conflicts),
        newest_time_ms=n,
        as_of_ms=AS_OF,
        rule=RULE,
    )

    for time_ms in conflicts:
        assert OpenInterestRun(S.CONFLICTED, time_ms, time_ms + P) in result.runs
    assert result.coverage.conflicted == 3
    assert result.coverage.conflicted_times == frozenset(conflicts)
    for time_ms in (o, n):  # the bounds of the series do not move: the rows at o and n stay present
        assert run_containing(result.runs, time_ms) is None
    assert result.coverage.present == 2


def test_the_horizon_edge_splits_a_no_longer_recoverable_run_from_a_missing_run() -> None:
    edge = AS_OF - H  # aligned: AS_OF is, and H is 8 352 periods
    runs = runs_of(edge - 2 * P, edge + 2 * P, stored=(edge + P,), newest=edge + P)

    assert runs == (
        OpenInterestRun(S.NO_LONGER_RECOVERABLE, edge - 2 * P, edge),
        OpenInterestRun(S.MISSING, edge, edge + P),
    )


def test_a_ten_thousand_slot_window_has_5000_and_10000_runs() -> None:
    start = T0
    end = T0 + 10_000 * P
    as_of = end + 10 * P
    newest = end + P  # a newer row after the window

    alternating = classify_open_interest_runs(
        start=start,
        end=end,
        stored={start + i * P for i in range(0, 10_000, 2)},
        conflicts=set(),
        newest_time_ms=newest,
        as_of_ms=as_of,
        rule=RULE,
    )
    assert len(alternating.runs) == 5_000
    assert all(run.end_ms - run.start_ms == P for run in alternating.runs)
    assert alternating.coverage.present == 5_000

    conflicted = classify_open_interest_runs(
        start=start,
        end=end,
        stored=set(),
        conflicts={start + i * P for i in range(0, 10_000, 2)},
        newest_time_ms=newest,
        as_of_ms=as_of,
        rule=RULE,
    )
    assert len(conflicted.runs) == 10_000
    for index, run in enumerate(conflicted.runs):
        assert run.end_ms - run.start_ms == P
        assert (run.state is S.CONFLICTED) == (index % 2 == 0)
    assert conflicted.coverage.conflicted == 5_000


def test_the_function_refuses_what_the_slot_function_refuses() -> None:
    with pytest.raises(ValueError):  # an unaligned member
        runs_of(T0, T0 + 3 * P, stored=(T0 + 1,), newest=T0 + 1)
    with pytest.raises(ValueError):  # a member outside the span
        runs_of(T0, T0 + 3 * P, stored=(T0 + 3 * P,), newest=T0 + 3 * P)
