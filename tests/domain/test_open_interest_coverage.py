"""Open interest slot states, counts and freshness (ADR-0003 P-7, P-10; contract api-v1 sections 6.5.3, 6.5.8)."""

import random

import pytest

from quasar_market_data.domain.freshness import Freshness, SeriesFreshness
from quasar_market_data.domain.open_interest_cadence import CadenceError
from quasar_market_data.domain.open_interest_coverage import (
    OpenInterestCoverage,
    OpenInterestRule,
    OpenInterestSlotState,
    classify_open_interest_span,
    open_interest_freshness,
    open_interest_rule,
    open_interest_slot_states,
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
AS_OF = T0 + 7 * P  # aligned: 1 792 674 600 000

S = OpenInterestSlotState


def state_of(
    t: int,
    as_of: int,
    *,
    stored: tuple[int, ...] = (),
    conflicts: tuple[int, ...] = (),
    newest: int,
    rule: OpenInterestRule = RULE,
) -> OpenInterestSlotState:
    """The state of the single slot ``t`` (a one-slot span)."""
    states = open_interest_slot_states(
        start=t,
        end=t + P,
        stored=set(stored),
        conflicts=set(conflicts),
        newest_time_ms=newest,
        as_of_ms=as_of,
        rule=rule,
    )
    assert [time for time, _ in states] == [t]
    return states[0][1]


# --- precedence (contract 6.5.3): the first match wins ---------------------------------------------------------


def test_conflicted_wins_over_present() -> None:
    assert state_of(T0, AS_OF, stored=(T0,), conflicts=(T0,), newest=T0) is S.CONFLICTED


def test_a_conflict_without_a_stored_row_is_conflicted() -> None:
    assert state_of(T0, AS_OF, conflicts=(T0,), newest=T0 - P) is S.CONFLICTED


def test_present_wins_over_not_yet_expected() -> None:
    assert state_of(T0, T0 + E - 1, stored=(T0,), newest=T0) is S.PRESENT


def test_a_stored_row_after_as_of_is_present() -> None:
    assert state_of(T0, T0 - P, stored=(T0,), newest=T0) is S.PRESENT


def test_not_yet_expected_wins_over_no_longer_recoverable() -> None:
    tiny = OpenInterestRule(1, E, L, P5, retrieval_horizon_ms=0)  # every slot before as_of is older than the horizon
    assert state_of(AS_OF - P, AS_OF, newest=AS_OF - 2 * P, rule=tiny) is S.NOT_YET_EXPECTED
    assert state_of(AS_OF - 2 * P, AS_OF, newest=AS_OF - 3 * P, rule=tiny) is S.NO_LONGER_RECOVERABLE


def test_no_longer_recoverable_wins_over_stale_and_missing() -> None:
    old = AS_OF - H - P
    assert state_of(old, AS_OF, newest=old - P) is S.NO_LONGER_RECOVERABLE  # after the newest row, would be stale
    assert state_of(old, AS_OF, newest=old + P) is S.NO_LONGER_RECOVERABLE  # before the newest row, would be missing


# --- boundaries, in exact milliseconds ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("as_of_minus_slot", "expected"),
    [
        (E - 1, S.NOT_YET_EXPECTED),
        (E, S.DUE),
        (E + L, S.DUE),
        (E + L + 1, S.STALE),
    ],
)
def test_the_clock_states_after_the_newest_row(as_of_minus_slot: int, expected: OpenInterestSlotState) -> None:
    assert state_of(T0, T0 + as_of_minus_slot, newest=T0 - P) is expected


def test_the_retrieval_horizon_edge_is_exact() -> None:
    assert state_of(AS_OF - H, AS_OF, newest=AS_OF - H - P) is S.STALE  # exactly H old is still requestable
    assert state_of(AS_OF - H - P, AS_OF, newest=AS_OF - H - 2 * P) is S.NO_LONGER_RECOVERABLE


def test_a_row_absent_after_the_newest_walks_not_yet_expected_due_stale_and_is_never_present() -> None:
    """The issue's "row absent for 3 rounds": the whole series stopped at ``newest``."""
    t = T0 + P
    walk = [state_of(t, t + E + offset, newest=T0) for offset in (-1, 0, L, L + 1, 3 * P)]
    assert walk == [S.NOT_YET_EXPECTED, S.DUE, S.DUE, S.STALE, S.STALE]
    assert S.PRESENT not in walk


def test_an_absent_row_with_newer_rows_is_not_yet_expected_then_missing_never_due_or_stale() -> None:
    t = T0
    newest = T0 + P
    walk = [state_of(t, t + E + offset, newest=newest) for offset in (-1, 0, L, L + 1, 3 * P)]
    assert walk == [S.NOT_YET_EXPECTED, S.MISSING, S.MISSING, S.MISSING, S.MISSING]


def test_slots_before_the_oldest_row_inside_the_horizon_are_missing() -> None:
    assert state_of(AS_OF - 100 * P, AS_OF, newest=AS_OF - 50 * P) is S.MISSING


# --- the contract example (6.5.4) and the 40-day outage -----------------------------------------------------------


def test_the_contract_example_counts() -> None:
    t = [T0 + k * P for k in range(7)]
    as_of = t[5] + E + 30_000  # 1 792 674 530 000
    coverage = classify_open_interest_span(
        start=t[0],
        end=t[6] + P,
        stored={t[0], t[1], t[3]},
        conflicts={t[1]},
        newest_time_ms=t[3],
        as_of_ms=as_of,
        rule=RULE,
    )

    assert as_of == 1_792_674_530_000
    assert coverage == OpenInterestCoverage(
        slots=7,
        present=2,
        conflicted=1,
        not_yet_expected=1,
        no_longer_recoverable=0,
        due=1,
        stale=1,
        missing=1,
        conflicted_times=frozenset({t[1]}),
    )
    assert not coverage.gap_free
    states = dict(
        open_interest_slot_states(
            start=t[0],
            end=t[6] + P,
            stored={t[0], t[1], t[3]},
            conflicts={t[1]},
            newest_time_ms=t[3],
            as_of_ms=as_of,
            rule=RULE,
        )
    )
    assert [states[x] for x in t] == [S.PRESENT, S.CONFLICTED, S.MISSING, S.PRESENT, S.STALE, S.DUE, S.NOT_YET_EXPECTED]


def test_history_ending_forty_days_before_as_of_in_a_window_of_ten_thousand_slots() -> None:
    """The issue's "history ending 40 days before ``as_of``", inside the API's 10 000-slot cap."""
    newest = AS_OF - 40 * 86_400_000
    coverage = classify_open_interest_span(
        start=AS_OF - 10_000 * P,
        end=AS_OF,
        stored=set(),
        conflicts=set(),
        newest_time_ms=newest,
        as_of_ms=AS_OF,
        rule=RULE,
    )

    assert coverage.slots == 10_000
    assert coverage.no_longer_recoverable == 1_648  # slots older than as_of - 29 days
    assert coverage.not_yet_expected == 1  # as_of - P: T + E is after as_of
    assert coverage.due == 0  # L is shorter than a period: as_of - 2P is already stale
    assert coverage.stale == 8_351
    assert coverage.present == coverage.conflicted == coverage.missing == 0


# --- invariants -------------------------------------------------------------------------------------------------


def test_the_counts_always_sum_to_slots_and_gap_free_means_all_present() -> None:
    rng = random.Random(20261103)
    for _ in range(200):
        start = T0 + rng.randrange(0, 20) * P
        slots = rng.randrange(1, 60)
        end = start + slots * P
        grid = [start + k * P for k in range(slots)]
        stored = {t for t in grid if rng.random() < 0.6}
        conflicts = {t for t in grid if rng.random() < 0.1}
        newest = max(stored) if stored else start - P
        if rng.random() < 0.3:
            newest = max(newest, start + rng.randrange(0, slots) * P)
            stored = {t for t in stored if t <= newest}
            if start <= newest < end:
                stored.add(newest)
        as_of = start + rng.randrange(-5, 3 * slots + 5000) * P + rng.choice((0, 1, 17_000, 299_999))
        coverage = classify_open_interest_span(
            start=start,
            end=end,
            stored=stored,
            conflicts=conflicts,
            newest_time_ms=newest,
            as_of_ms=as_of,
            rule=RULE,
        )
        total = (
            coverage.present
            + coverage.conflicted
            + coverage.not_yet_expected
            + coverage.no_longer_recoverable
            + coverage.due
            + coverage.stale
            + coverage.missing
        )
        assert total == coverage.slots == slots
        assert coverage.gap_free == (coverage.present == coverage.slots)
        assert coverage.conflicted_times == frozenset(conflicts)


# --- validation: a caller bug never becomes a wrong count --------------------------------------------------------


def _states(**overrides: object) -> object:
    args: dict[str, object] = {
        "start": T0,
        "end": T0 + 3 * P,
        "stored": {T0},
        "conflicts": set(),
        "newest_time_ms": T0,
        "as_of_ms": AS_OF,
        "rule": RULE,
    }
    args.update(overrides)
    return open_interest_slot_states(**args)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"start": float(T0)}, TypeError),
        ({"start": True}, TypeError),
        ({"end": T0 + 3 * P + 1}, ValueError),  # unaligned end
        ({"start": T0 + 1, "end": T0 + 3 * P}, ValueError),  # unaligned start
        ({"end": T0}, ValueError),  # empty span
        ({"as_of_ms": float(AS_OF)}, TypeError),
        ({"as_of_ms": 0}, ValueError),  # outside the valid time range
        ({"stored": {T0 + 1}}, ValueError),  # unaligned member
        ({"stored": {T0 - P}, "newest_time_ms": T0}, ValueError),  # member outside the span
        ({"conflicts": {T0 + 5 * P}}, ValueError),  # member outside the span
        ({"newest_time_ms": T0 + 1}, ValueError),  # unaligned newest
        ({"stored": {T0 + P}, "newest_time_ms": T0}, ValueError),  # a stored row newer than newest
        ({"stored": set(), "newest_time_ms": T0}, ValueError),  # newest inside the span but not stored
        ({"rule": None}, TypeError),
    ],
)
def test_wrong_input_raises(overrides: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        _states(**overrides)


# --- the rule -------------------------------------------------------------------------------------------------


def test_rule_defaults_the_retrieval_horizon_to_twenty_nine_days() -> None:
    assert RULE.retrieval_horizon_ms == 29 * 86_400_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cadence": 0},
        {"cadence": 13},
        {"cadence": True},
        {"cadence": 1.0},
        {"expected_after_ms": -1},
        {"lag_threshold_ms": -1},
        {"lag_threshold_ms": 1.5},
        {"retrieval_horizon_ms": -1},
        {"period": "5m"},
    ],
)
def test_a_wrong_rule_is_refused(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {"cadence": 1, "expected_after_ms": E, "lag_threshold_ms": L, "period": P5}
    base.update(kwargs)
    with pytest.raises((TypeError, ValueError)):
        OpenInterestRule(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("symbols", "budget", "cadence", "expected_after"),
    [
        (10, 400, 1, 500_000),
        (149, 400, 1, 500_000),
        (150, 400, 2, 800_000),
        (300, 400, 3, 1_100_000),
        (600, 400, 5, 1_700_000),
        (600, 200, 9, 2_900_000),
        (10, 100, 1, 500_000),
        (432, 100, 12, 3_800_000),
    ],
)
def test_the_rule_derives_cadence_and_expectation_from_the_collectors_functions(
    symbols: int, budget: int, cadence: int, expected_after: int
) -> None:
    rule = open_interest_rule(symbols, budget, 200_000, L, P5)

    assert rule == OpenInterestRule(cadence, expected_after, L, P5)


def test_a_symbol_count_that_needs_a_cadence_above_twelve_is_refused() -> None:
    with pytest.raises(CadenceError):
        open_interest_rule(433, 100, 200_000, L, P5)


# --- freshness of a series (contract 6.5.8) ---------------------------------------------------------------------


def fresh(newest: int, as_of: int, rule: OpenInterestRule = RULE) -> SeriesFreshness:
    return open_interest_freshness(newest, as_of, rule)


def test_freshness_boundaries_for_cadence_one() -> None:
    x = T0 + P + E  # the instant the slot after the newest row is expected

    assert fresh(T0, x - 1) == SeriesFreshness(Freshness.CURRENT, 0)
    assert fresh(T0, x) == SeriesFreshness(Freshness.DUE, 1)
    assert fresh(T0, x + L) == SeriesFreshness(Freshness.DUE, 1)
    assert fresh(T0, x + L + 1) == SeriesFreshness(Freshness.STALE, 1)
    assert fresh(T0, x + P) == SeriesFreshness(Freshness.STALE, 2)
    assert fresh(T0, x + P - 1) == SeriesFreshness(Freshness.STALE, 1)


def test_freshness_with_a_larger_cadence_uses_its_expectation() -> None:
    rule = OpenInterestRule(5, 1_700_000, L, P5)
    x = T0 + P + 1_700_000

    assert fresh(T0, x - 1, rule) == SeriesFreshness(Freshness.CURRENT, 0)
    assert fresh(T0, x, rule) == SeriesFreshness(Freshness.DUE, 1)
    assert fresh(T0, x + L + 1, rule) == SeriesFreshness(Freshness.STALE, 1)


def test_a_clock_behind_the_stored_data_is_current() -> None:
    assert fresh(T0, T0 - 10 * P) == SeriesFreshness(Freshness.CURRENT, 0)


def test_freshness_refuses_an_unaligned_or_mistyped_input() -> None:
    with pytest.raises(ValueError):
        fresh(T0 + 1, AS_OF)
    with pytest.raises(TypeError):
        fresh(float(T0), AS_OF)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        fresh(T0, 0)
