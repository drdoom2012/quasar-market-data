from typing import Any

import pytest

from quasar_market_data.domain.reconcile import (
    REACH_MARGIN_SLOTS,
    HistoryFacts,
    HistoryStatus,
    SeriesFacts,
    SeriesReconcile,
    WindowStatus,
    attempt_limit,
    clock_lead_slots,
    max_target_distance_slots,
    reconcile_series,
    reconcile_window,
    steady_state_limit_needed,
)
from quasar_market_data.domain.schedule import newest_expected_open_ms
from quasar_market_data.domain.timed_coverage import ExpectationRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import MIN_TIMESTAMP_MS

TF = 300_000
F0 = 1792643400000  # aligned to 5m
assert F0 % TF == 0
RULE = ExpectationRule(15_000)
DEFAULT = 100
MAX = 1000
AS_OF = F0 + 20_000
T = F0 - TF
WS = T - 996 * TF


def t(n: int) -> int:
    """T + n slots."""
    return T + n * TF


def facts(
    oldest: int,
    newest: int,
    *,
    holes: tuple[int, ...] = (),
    conflicts: tuple[int, ...] = (),
    unavailable: tuple[int, ...] = (),
) -> SeriesFacts:
    start = max(oldest, WS)
    candles = frozenset(s for s in range(start, min(newest, T) + TF, TF) if s not in holes)
    return SeriesFacts(oldest, newest, candles, frozenset(conflicts), frozenset(unavailable))


def run(
    f: SeriesFacts | None,
    h: HistoryFacts | None = None,
    *,
    as_of: int = AS_OF,
    target: int | None = T,
    rule: ExpectationRule = RULE,
    default: int = DEFAULT,
    max_limit: int = MAX,
) -> SeriesReconcile:
    return reconcile_series(
        f,
        h,
        as_of_ms=as_of,
        target_open_ms=target,
        timeframe_ms=TF,
        rule=rule,
        default_limit=default,
        max_limit=max_limit,
    )


def no_holes() -> HistoryFacts:
    return HistoryFacts(0, 0, 0, (), 0, 0, 0)


# arithmetic


def test_max_target_distance_matches_the_measured_offsets() -> None:
    tf = Timeframe("5m")
    cases = {0: 1, 15_000: 2, 299_999: 2, 300_000: 2, 400_000: 3, 700_000: 4, 30_000_000: 101, 30_000_001: 102}
    for delay, expected in cases.items():
        assert max_target_distance_slots(delay, TF) == expected
        seen = 0
        for step in range(0, TF + delay + TF, 1000):
            now = F0 + step
            newest = newest_expected_open_ms(now, tf, delay)
            assert newest is not None
            seen = max(seen, (now - now % TF - newest) // TF)
        assert seen == expected


def test_steady_state_limit_needed() -> None:
    assert steady_state_limit_needed(15_000, TF) == 5
    assert steady_state_limit_needed(700_000, TF) == 4 + 1 + 2
    assert steady_state_limit_needed(30_000_000, TF) == 101 + 3
    assert steady_state_limit_needed(30_000_001, TF) == 102 + 3
    assert REACH_MARGIN_SLOTS == 2


def al(now: int, g: int | None, **kw: int) -> int:
    return attempt_limit(
        now_ms=now, oldest_wanted_ms=g, timeframe_ms=TF, default_limit=kw.get("d", 100), max_limit=kw.get("m", 1000)
    )


def test_attempt_limit_examples() -> None:
    assert al(AS_OF, WS) == 1000
    assert al(AS_OF, F0 - 990 * TF) == 993
    assert al(AS_OF, t(-59), d=10) == 63
    assert al(AS_OF, t(-59)) == 100
    assert al(F0 + 3 * TF, WS) == 1000
    assert al(F0 + 3 * TF, F0 - 990 * TF) == 996
    assert al(AS_OF, None) == 100
    assert al(AS_OF, t(-5)) == 100


def test_attempt_limit_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        al(AS_OF, WS + 1)
    with pytest.raises(ValueError):
        al(AS_OF, F0 + TF)
    with pytest.raises(TypeError):
        al(AS_OF, float(WS))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        al(float(AS_OF), WS)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        al(AS_OF, WS, d=0)
    with pytest.raises(ValueError):
        al(AS_OF, WS, m=3)


# window


def test_window() -> None:
    assert reconcile_window(AS_OF, T, TF, MAX) == (WS, T + TF)


def test_window_is_clamped_at_the_first_valid_slot() -> None:
    first = MIN_TIMESTAMP_MS + 10 * TF
    assert reconcile_window(first + 20_000, first - TF, TF, MAX) == (MIN_TIMESTAMP_MS, first)


def test_window_is_empty_when_the_target_is_out_of_reach() -> None:
    assert reconcile_window(AS_OF, F0 - 101 * TF, TF, 100) is None


def test_window_for_a_700_000_delay() -> None:
    assert reconcile_window(AS_OF, F0 - 4 * TF, TF, MAX) == (WS, F0 - 3 * TF)


def test_window_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        reconcile_window(AS_OF, T + 1, TF, MAX)
    with pytest.raises(ValueError):
        reconcile_window(AS_OF, F0, TF, MAX)
    with pytest.raises(ValueError):
        reconcile_window(AS_OF, T, TF, REACH_MARGIN_SLOTS + 1)


def test_clock_lead_slots() -> None:
    assert clock_lead_slots(F0 + 3 * TF, F0 + 40_000, TF) == 3
    assert clock_lead_slots(F0, F0 + 40_000, TF) == 0
    assert clock_lead_slots(F0 - TF, F0 + 40_000, TF) == -1
    assert clock_lead_slots(F0 + TF, F0 + TF - 1, TF) == 1
    with pytest.raises(ValueError):
        clock_lead_slots(F0 + 1, F0, TF)


# series


def test_no_stored_candle_is_no_baseline_and_history_unknown() -> None:
    r = run(None, None)
    assert r.window is WindowStatus.NO_BASELINE
    assert r.history is HistoryStatus.UNKNOWN
    assert r.all_present is False
    assert r.unknown_before_ms is None
    assert r.catch_up_limit_at_read == 100
    r = run(None, no_holes())
    assert r.history is HistoryStatus.UNKNOWN


def test_nothing_expected_when_the_target_is_none() -> None:
    r = run(facts(t(-10), T), None, target=None)
    assert r.window is WindowStatus.NOTHING_EXPECTED
    assert r.slots_classified == 0
    assert r.all_present is False


def test_series_younger_than_the_window_has_no_false_gaps() -> None:
    r = run(facts(t(-10), T))
    assert r.window is WindowStatus.WINDOW_NO_GAPS
    assert r.missing == 0
    assert r.catch_up_limit_at_read == 100
    assert r.unknown_before_ms == t(-10)
    assert r.slots_classified == 11
    assert r.all_present is True


def test_hole_after_the_oldest_candle_is_missing() -> None:
    r = run(facts(t(-10), T, holes=(t(-5),)))
    assert r.missing == 1
    assert r.window is WindowStatus.WINDOW_GAPS
    assert r.all_present is False


def test_tail_gap_sets_the_wanted_slot() -> None:
    r = run(facts(WS, t(-49)))
    assert r.due + r.stale == 49
    assert r.oldest_wanted_ms == t(-48)
    assert r.catch_up_limit_at_read == 100
    r = run(facts(WS, t(-200)))
    assert r.oldest_wanted_ms == t(-199)
    assert (F0 - t(-199)) // TF == 200
    assert r.catch_up_limit_at_read == 203
    assert r.window is WindowStatus.WINDOW_GAPS
    assert r.absent_runs[0].start_ms == t(-199)


def test_interior_hole() -> None:
    r = run(facts(WS, T, holes=(t(-300),)))
    assert r.oldest_wanted_ms == t(-300)
    assert r.catch_up_limit_at_read == 304


def test_gap_beyond_the_reach() -> None:
    r = run(facts(WS - 2000 * TF, t(-2000)))
    assert r.unreachable_tail_slots == 1003
    assert r.catch_up_limit_at_read == 1000
    assert r.slots_classified == 997
    assert r.present == 0
    assert r.needs_s16_slots == 1003
    assert r.window is WindowStatus.WINDOW_GAPS


def test_unavailable_and_conflicted_slots_do_not_enlarge_the_request_but_a_missing_one_does() -> None:
    r = run(facts(WS, T, holes=(t(-400),), conflicts=(t(-300),), unavailable=(t(-400),)))
    assert r.window is WindowStatus.WINDOW_NO_GAPS
    assert r.catch_up_limit_at_read == 100
    assert r.unavailable == 1
    assert r.conflicted == 1
    r = run(facts(WS, T, holes=(t(-400), t(-200)), conflicts=(t(-300),), unavailable=(t(-400),)))
    assert r.oldest_wanted_ms == t(-200)
    assert r.catch_up_limit_at_read == 204
    assert r.missing == 1


def test_stored_and_unavailable_slot_is_conflicted() -> None:
    r = run(facts(WS, T, unavailable=(t(-7),)))
    assert r.conflicted == 1
    assert r.unavailable == 0


def test_due_slots_are_not_gaps_but_the_window_is_not_all_present() -> None:
    r = run(facts(WS, t(-1)))
    assert r.due == 1
    assert r.window is WindowStatus.WINDOW_NO_GAPS
    assert r.all_present is False


def test_conflicted_or_unavailable_slots_keep_all_present_false() -> None:
    for kw in ({"conflicts": (t(-3),)}, {"unavailable": (t(-3),), "holes": (t(-3),)}):
        r = run(facts(WS, T, **kw))
        assert r.window is WindowStatus.WINDOW_NO_GAPS
        assert r.all_present is False


def test_all_present_only_when_every_classified_slot_is_present() -> None:
    r = run(facts(WS, T))
    assert r.present == r.slots_classified == 997
    assert r.all_present is True
    assert (r.missing, r.due, r.stale, r.conflicted, r.unavailable) == (0, 0, 0, 0, 0)


def test_clock_behind_stored_data_sets_the_flag_and_still_classifies() -> None:
    f = facts(WS, t(2), holes=(t(-4),))
    f = SeriesFacts(f.oldest_open_ms, t(2), f.candles, f.conflicts, f.unavailable)
    r = run(f, no_holes(), as_of=F0 + 10_000)
    assert r.clock_behind_stored is True
    assert r.missing == 1
    assert r.window is WindowStatus.WINDOW_GAPS
    assert r.history is HistoryStatus.CONTIGUOUS_FROM_OLDEST
    assert run(facts(WS, T)).clock_behind_stored is False


def test_window_not_assessed_when_stored_data_starts_at_or_after_the_window_end() -> None:
    r = run(SeriesFacts(t(1), t(3), frozenset(), frozenset(), frozenset()), no_holes())
    assert r.window is WindowStatus.WINDOW_NOT_ASSESSED
    assert (r.slots_classified, r.present, r.missing, r.due, r.stale) == (0, 0, 0, 0, 0)
    assert r.all_present is False
    assert r.history is HistoryStatus.CONTIGUOUS_FROM_OLDEST


def test_target_out_of_reach_status() -> None:
    far = F0 - 101 * TF
    f = SeriesFacts(far - 5 * TF, far, frozenset(), frozenset(), frozenset())
    r = run(f, None, target=far, max_limit=100)
    assert r.window is WindowStatus.TARGET_OUT_OF_REACH
    assert r.slots_classified == 0


# history


def test_window_no_gaps_does_not_mean_history_complete() -> None:
    h = HistoryFacts(7, 7, 1, ((WS - 20 * TF, WS - 13 * TF),), 0, 0, 0)
    r = run(facts(WS, T), h)
    assert r.window is WindowStatus.WINDOW_NO_GAPS
    assert r.history is HistoryStatus.HOLES
    assert r.history_unaccounted_holes == 7
    assert r.needs_s16_slots == 7
    assert r.hole_runs == 1
    assert r.largest_hole_runs == h.largest_runs


def test_contiguous_history_still_has_an_unknown_start() -> None:
    r = run(facts(t(-10), T), no_holes())
    assert r.history is HistoryStatus.CONTIGUOUS_FROM_OLDEST
    assert r.unknown_before_ms == t(-10)


def test_history_enum_has_no_complete_member() -> None:
    assert "COMPLETE" not in HistoryStatus.__members__
    assert not any("COMPLETE" in name for name in WindowStatus.__members__)


def test_unavailable_slots_in_holes_are_accounted() -> None:
    r = run(facts(WS, T), HistoryFacts(7, 0, 1, (), 7, 0, 2))
    assert r.history is HistoryStatus.CONTIGUOUS_FROM_OLDEST
    assert r.history_unaccounted_holes == 0
    assert r.history_conflicted_slots == 2
    r = run(facts(WS, T), HistoryFacts(7, 7, 1, (), 3, 3, 0))
    assert r.history is HistoryStatus.HOLES
    assert r.history_unaccounted_holes == 4
    assert r.history_holes_older_than_window == 4


def test_needs_s16_adds_the_tail_and_the_old_holes() -> None:
    r = run(facts(WS - 2000 * TF, t(-2000)), HistoryFacts(7, 7, 1, (), 3, 3, 0))
    assert r.needs_s16_slots == 1003 + 4


def test_inconsistent_history_raises() -> None:
    bad = [
        HistoryFacts(3, 0, 1, (), 4, 0, 0),
        HistoryFacts(3, 4, 1, (), 0, 0, 0),
        HistoryFacts(3, 3, 1, (), 1, 2, 0),
        HistoryFacts(-1, 0, 0, (), 0, 0, 0),
        HistoryFacts(3, 0, 1, (), 0, 0, -1),
        HistoryFacts(0, 0, 0, ((1, 2),) * 6, 0, 0, 0),
    ]
    for h in bad:
        with pytest.raises(ValueError):
            run(facts(WS, T), h)


def test_bad_inputs_raise_on_every_path() -> None:
    f: Any = facts(WS, T)
    for fx, tg in ((None, T), (f, None), (f, T)):
        with pytest.raises(TypeError):
            run(fx, None, as_of=float(AS_OF), target=tg)  # type: ignore[arg-type]
    for fx in (None, f):
        with pytest.raises(ValueError):
            run(fx, None, target=T + 1)
        with pytest.raises(ValueError):
            run(fx, None, max_limit=REACH_MARGIN_SLOTS + 1)
    with pytest.raises(ValueError):
        run(f, None, target=None, max_limit=REACH_MARGIN_SLOTS + 1)
    out = SeriesFacts(WS, T, frozenset({T + TF}), frozenset(), frozenset())
    with pytest.raises(ValueError):
        run(out, None)
    out = SeriesFacts(WS, T, frozenset({WS - TF}), frozenset(), frozenset())
    with pytest.raises(ValueError):
        run(out, None)
    with pytest.raises(ValueError):
        run(f, None, target=F0)
