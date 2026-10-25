import random
from typing import Any

import pytest

from quasar_market_data.domain.span_coverage import classify_span
from quasar_market_data.domain.timed_coverage import (
    DEFAULT_LAG_THRESHOLD_MS,
    ExpectationRule,
    GapRun,
    SlotState,
    TimedCoverage,
    classify_span_at,
)
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, MIN_TIMESTAMP_MS

TF = 300_000
S = 1792643400000  # aligned
DAY = 86_400_000
RULE = ExpectationRule(15_000, 60_000)


def slot(n: int) -> int:
    return S + n * TF


def classify(n_slots: int, *, newest: int, as_of: int, rule: ExpectationRule = RULE, **sets: set[int]) -> TimedCoverage:
    return classify_span_at(
        start=S,
        end=slot(n_slots),
        timeframe_ms=TF,
        candles=sets.get("candles", set()),
        conflicts=sets.get("conflicts", set()),
        unavailable=sets.get("unavailable", set()),
        newest_open_time_ms=newest,
        as_of_ms=as_of,
        rule=rule,
    )


def counts(c: TimedCoverage) -> tuple[int, ...]:
    return (c.present, c.conflicted, c.unavailable, c.missing, c.not_yet_expected, c.due, c.stale)


def test_default_lag_threshold() -> None:
    assert DEFAULT_LAG_THRESHOLD_MS == 60_000
    assert ExpectationRule(0).lag_threshold_ms == 60_000


EXPECTED_AT = slot(2) + 15_000  # the end of slot(1), the slot after the newest candle at slot(0), plus the delay


@pytest.mark.parametrize(
    ("as_of", "state"),
    [
        (EXPECTED_AT - 1, "not_yet_expected"),
        (EXPECTED_AT, "due"),
        (EXPECTED_AT + 60_000, "due"),
        (EXPECTED_AT + 60_001, "stale"),
    ],
)
def test_clock_boundaries_of_a_slot_after_the_newest_candle(as_of: int, state: str) -> None:
    # the span is slot(1) only; slot(0) is the newest candle, stored just before it
    c = classify_span_at(
        start=slot(1),
        end=slot(2),
        timeframe_ms=TF,
        candles=set(),
        conflicts=set(),
        unavailable=set(),
        newest_open_time_ms=slot(0),
        as_of_ms=as_of,
        rule=RULE,
    )
    assert c.slots == 1
    assert getattr(c, state) == 1
    assert sum(counts(c)) == 1


def test_a_series_that_stopped_long_ago_is_stale_after_its_newest_candle() -> None:
    c = classify(4, newest=slot(0), as_of=slot(0) + DAY, candles={slot(0)})
    assert (c.present, c.stale, c.not_yet_expected, c.due) == (1, 3, 0, 0)


def test_a_window_in_the_future_of_as_of_is_all_not_yet_expected() -> None:
    c = classify(4, newest=S - TF, as_of=S + TF + 15_000 - 1)
    assert c.not_yet_expected == 4
    assert c.slots == 4


def test_unavailable_after_the_newest_candle_is_not_stale() -> None:
    c = classify(3, newest=slot(0), as_of=slot(0) + DAY, candles={slot(0)}, unavailable={slot(1)})
    assert (c.present, c.unavailable, c.stale) == (1, 1, 1)


def test_conflict_after_the_newest_candle_is_not_stale() -> None:
    c = classify(3, newest=slot(0), as_of=slot(0) + DAY, candles={slot(0)}, conflicts={slot(1)})
    assert (c.present, c.conflicted, c.stale) == (1, 1, 1)


def test_missing_before_the_newest_candle_ignores_the_clock() -> None:
    results = [counts(classify(3, newest=slot(2), as_of=as_of, candles={slot(2)})) for as_of in (S, S + DAY)]
    assert results[0] == results[1]
    assert results[0][3] == 2


def test_a_candle_is_present_even_when_as_of_is_before_its_eligibility() -> None:
    c = classify(1, newest=slot(0), as_of=S, candles={slot(0)})
    assert c.present == 1
    assert c.stored_ahead_of_as_of is True


def test_stored_ahead_of_as_of_boundary() -> None:
    boundary = slot(0) + TF + 15_000
    assert classify(1, newest=slot(0), as_of=boundary, candles={slot(0)}).stored_ahead_of_as_of is False
    assert classify(1, newest=slot(0), as_of=boundary - 1, candles={slot(0)}).stored_ahead_of_as_of is True


def test_stored_ahead_of_as_of_does_not_change_the_classification() -> None:
    # 4 slots, a candle at slot(0), slots 1..3 after it; as_of = slot(1) + 5_000.
    # Decision 1 by hand: expected_at = slot end + delay, i.e. slot(2)+d, slot(3)+d, slot(4)+d for slots 1..3.
    # d = 0: all three expected_at are >= slot(2) > as_of  -> 3 not_yet_expected.
    # d = 15_000: later still                              -> 3 not_yet_expected.
    # The flag is newest end + d > as_of: slot(1) + d > slot(1) + 5_000, False for d = 0, True for d = 15_000.
    as_of = slot(1) + 5_000
    zero = classify(4, newest=slot(0), as_of=as_of, rule=ExpectationRule(0, 60_000), candles={slot(0)})
    delayed = classify(4, newest=slot(0), as_of=as_of, rule=ExpectationRule(15_000, 60_000), candles={slot(0)})
    assert zero.stored_ahead_of_as_of is False
    assert delayed.stored_ahead_of_as_of is True
    assert counts(zero) == (1, 0, 0, 0, 3, 0, 0)
    assert counts(delayed) == (1, 0, 0, 0, 3, 0, 0)


def test_stored_ahead_of_as_of_with_a_historical_as_of() -> None:
    # as_of before the candle's own eligibility: flag True, the candle is still present, slots after it
    # (expected_at = slot(2) + 15_000 > as_of and later) are not_yet_expected.
    c = classify(4, newest=slot(0), as_of=S, candles={slot(0)})
    assert c.stored_ahead_of_as_of is True
    assert counts(c) == (1, 0, 0, 0, 3, 0, 0)


def test_zero_lag_threshold() -> None:
    rule = ExpectationRule(15_000, 0)

    def at(as_of: int) -> TimedCoverage:
        return classify_span_at(
            start=slot(1),
            end=slot(2),
            timeframe_ms=TF,
            candles=set(),
            conflicts=set(),
            unavailable=set(),
            newest_open_time_ms=slot(0),
            as_of_ms=as_of,
            rule=rule,
        )

    assert at(EXPECTED_AT - 1).not_yet_expected == 1
    assert at(EXPECTED_AT).due == 1
    assert at(EXPECTED_AT + 1).stale == 1


def test_counts_always_sum_to_slots_and_refine_classify_span() -> None:
    rng = random.Random(12)
    for _ in range(300):
        n = rng.randint(1, 12)
        slots = [slot(i) for i in range(n)]
        candles = {s for s in slots if rng.random() < 0.4}
        conflicts = {s for s in slots if rng.random() < 0.15}
        unavailable = {s for s in slots if rng.random() < 0.2}
        newest = max(candles) if candles else S - TF
        # candles may not be newer than newest: guaranteed by construction
        as_of = S + rng.randint(-2 * TF, (n + 3) * TF)
        rule = ExpectationRule(rng.choice([0, 15_000, 120_000]), rng.choice([0, 60_000, 400_000]))
        kwargs: dict[str, Any] = {
            "start": S,
            "end": slot(n),
            "timeframe_ms": TF,
            "candles": candles,
            "conflicts": conflicts,
            "unavailable": unavailable,
            "newest_open_time_ms": newest,
        }
        timed = classify_span_at(**kwargs, as_of_ms=as_of, rule=rule)
        base = classify_span(**kwargs)
        assert timed.slots == sum(counts(timed))
        assert (timed.present, timed.conflicted, timed.unavailable, timed.missing) == (
            base.present,
            base.conflicted,
            base.unavailable,
            base.missing,
        )
        assert timed.not_yet_expected + timed.due + timed.stale == base.beyond_stored
        # independent oracle from the input sets
        conflicted = conflicts | (candles & unavailable)
        in_span = set(slots)
        assert timed.present == len(candles - conflicted)
        assert timed.conflicted == len(conflicted)
        assert timed.unavailable == len(unavailable - candles - conflicts)
        assert timed.missing == len({s for s in in_span if s <= newest} - candles - conflicts - unavailable)


def test_present_slots_are_not_listed() -> None:
    c = classify(3, newest=slot(2), as_of=slot(10), candles={slot(0), slot(2)})
    for run in c.runs:
        assert run.state is not SlotState.PRESENT
        assert not any(run.start_ms <= t < run.end_ms for t in (slot(0), slot(2)))


def test_a_fully_present_span_has_no_runs() -> None:
    c = classify(3, newest=slot(2), as_of=slot(10), candles={slot(0), slot(1), slot(2)})
    assert c.runs == ()


def test_adjacent_slots_of_one_state_merge_into_one_run() -> None:
    c = classify(5, newest=slot(4), as_of=slot(10), candles={slot(0), slot(4)})
    assert c.runs == (GapRun(SlotState.MISSING, slot(1), slot(4)),)


def test_a_state_change_starts_a_new_run() -> None:
    c = classify(5, newest=slot(4), as_of=slot(10), candles={slot(0), slot(4)}, unavailable={slot(2), slot(3)})
    assert c.runs == (
        GapRun(SlotState.MISSING, slot(1), slot(2)),
        GapRun(SlotState.UNAVAILABLE, slot(2), slot(4)),
    )


def test_runs_cover_exactly_the_non_present_slots() -> None:
    rng = random.Random(13)
    for _ in range(200):
        n = rng.randint(1, 12)
        slots = [slot(i) for i in range(n)]
        candles = {s for s in slots if rng.random() < 0.4}
        conflicts = {s for s in slots if rng.random() < 0.15}
        unavailable = {s for s in slots if rng.random() < 0.2}
        newest = max(candles) if candles else S - TF
        rule = ExpectationRule(rng.choice([0, 15_000, 120_000]), rng.choice([0, 60_000, 400_000]))
        c = classify_span_at(
            start=S,
            end=slot(n),
            timeframe_ms=TF,
            candles=candles,
            conflicts=conflicts,
            unavailable=unavailable,
            newest_open_time_ms=newest,
            as_of_ms=S + rng.randint(-2 * TF, (n + 3) * TF),
            rule=rule,
        )
        conflicted = conflicts | (candles & unavailable)
        non_present = {s for s in slots if s not in candles - conflicted}
        covered = {t for run in c.runs for t in range(run.start_ms, run.end_ms, TF)}
        assert covered == non_present
        per_state = dict.fromkeys(SlotState, 0)
        for run in c.runs:
            assert run.state is not SlotState.PRESENT
            assert run.start_ms < run.end_ms
            per_state[run.state] += (run.end_ms - run.start_ms) // TF
        assert per_state == {
            SlotState.PRESENT: 0,
            SlotState.CONFLICTED: c.conflicted,
            SlotState.UNAVAILABLE: c.unavailable,
            SlotState.MISSING: c.missing,
            SlotState.NOT_YET_EXPECTED: c.not_yet_expected,
            SlotState.DUE: c.due,
            SlotState.STALE: c.stale,
        }
        for a, b in zip(c.runs, c.runs[1:], strict=False):
            assert a.end_ms <= b.start_ms
            assert not (a.end_ms == b.start_ms and a.state is b.state)


def valid(**override: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "start": S,
        "end": slot(3),
        "timeframe_ms": TF,
        "candles": {slot(0)},
        "conflicts": set(),
        "unavailable": set(),
        "newest_open_time_ms": slot(0),
        "as_of_ms": slot(5),
        "rule": RULE,
    }
    kwargs.update(override)
    return kwargs


def test_valid_baseline() -> None:
    assert classify_span_at(**valid()).slots == 3


@pytest.mark.parametrize("bad", [True, 1.5])
@pytest.mark.parametrize("field", ["as_of_ms", "start", "end", "newest_open_time_ms", "timeframe_ms"])
def test_scalar_type_errors(field: str, bad: object) -> None:
    with pytest.raises(TypeError):
        classify_span_at(**valid(**{field: bad}))


def test_aligned_float_as_of_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        classify_span_at(**valid(as_of_ms=float(S)))


@pytest.mark.parametrize("bad", [True, 1.5, float(slot(1))])
@pytest.mark.parametrize("field", ["candles", "conflicts", "unavailable"])
def test_set_member_type_errors(field: str, bad: object) -> None:
    with pytest.raises(TypeError):
        classify_span_at(**valid(**{field: {slot(0), bad}}))


def test_rule_type_errors() -> None:
    with pytest.raises(TypeError):
        ExpectationRule(True)
    with pytest.raises(TypeError):
        ExpectationRule(0, True)


@pytest.mark.parametrize("as_of", [MIN_TIMESTAMP_MS - 1, MAX_TIMESTAMP_MS + 1, 0, -1])
def test_as_of_out_of_range(as_of: int) -> None:
    with pytest.raises(ValueError):
        classify_span_at(**valid(as_of_ms=as_of))


@pytest.mark.parametrize("field", ["start", "newest_open_time_ms"])
@pytest.mark.parametrize("bad", [MIN_TIMESTAMP_MS - 1, MAX_TIMESTAMP_MS + 1])
def test_scalar_out_of_range(field: str, bad: int) -> None:
    with pytest.raises(ValueError):
        classify_span_at(**valid(**{field: bad}))


@pytest.mark.parametrize("field", ["candles", "conflicts", "unavailable"])
@pytest.mark.parametrize("bad", [MIN_TIMESTAMP_MS - 1, MAX_TIMESTAMP_MS + 1])
def test_set_member_out_of_range(field: str, bad: int) -> None:
    with pytest.raises(ValueError):
        classify_span_at(**valid(**{field: {slot(0), bad}}))


def test_rule_negative_values() -> None:
    with pytest.raises(ValueError):
        ExpectationRule(-1)
    with pytest.raises(ValueError):
        ExpectationRule(0, -1)


def test_misaligned_span() -> None:
    with pytest.raises(ValueError):
        classify_span_at(**valid(end=slot(3) + 1))


@pytest.mark.parametrize("field", ["candles", "conflicts", "unavailable"])
@pytest.mark.parametrize("bad", [slot(3), S - TF, slot(1) + 1])
def test_member_outside_or_misaligned(field: str, bad: int) -> None:
    with pytest.raises(ValueError):
        classify_span_at(**valid(**{field: {slot(0), bad}}, newest_open_time_ms=slot(3)))


def test_end_above_the_last_valid_slot() -> None:
    last = (MAX_TIMESTAMP_MS // TF) * TF
    with pytest.raises(ValueError):
        classify_span_at(**valid(start=last, end=last + 2 * TF, candles=set(), newest_open_time_ms=last - TF))


def test_newest_inside_the_span_must_be_a_candle() -> None:
    with pytest.raises(ValueError):
        classify_span_at(**valid(candles=set(), newest_open_time_ms=slot(1)))


def test_candle_newer_than_newest() -> None:
    with pytest.raises(ValueError):
        classify_span_at(**valid(candles={slot(0), slot(2)}, newest_open_time_ms=slot(0)))


def test_end_exactly_at_the_last_valid_slot_end_is_accepted() -> None:
    c = classify_span_at(
        **valid(
            start=MAX_TIMESTAMP_MS,
            end=MAX_TIMESTAMP_MS + TF,
            candles=set(),
            newest_open_time_ms=MAX_TIMESTAMP_MS - TF,
            as_of_ms=MAX_TIMESTAMP_MS,
        )
    )
    assert c.slots == 1


def test_newest_open_time_must_be_aligned() -> None:
    with pytest.raises(ValueError):
        classify_span_at(**valid(newest_open_time_ms=slot(0) - 1))


@pytest.mark.parametrize("bad", [None, (15_000, 60_000), 15_000])
def test_rule_must_be_an_expectation_rule(bad: object) -> None:
    with pytest.raises(TypeError):
        classify_span_at(**valid(rule=bad))
