import itertools
from collections.abc import Sequence

import pytest

from quasar_market_data.domain.indicator_series import (
    IndicatorPoint,
    IndicatorSeries,
    evaluate,
    read_start_ms,
)
from quasar_market_data.domain.timed_coverage import SlotState
from quasar_market_data.domain.timestamps import MIN_TIMESTAMP_MS

TF = 300_000
S = 1792643400000  # aligned, far above T_min
W = 140
END = S + 400 * TF
T_MIN = MIN_TIMESTAMP_MS


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[float]] = []

    def __call__(self, closes: Sequence[float]) -> Sequence[Sequence[float]]:
        self.calls.append(list(closes))
        return (list(itertools.accumulate(closes)),)


def slot(n: int, start: int = S) -> int:
    return start + n * TF


def close(open_time: int) -> float:
    return float((open_time // TF) % 97 + 1)  # small integers: running sums are exact


def direct(anchor: int, open_time: int) -> float:
    """The running sum computed directly over ``[anchor, open_time]``."""
    return sum(close(t) for t in range(anchor, open_time + TF, TF))


def build(
    start: int = S, end: int = END, non_present: dict[int, SlotState] | None = None, warmup: int = W
) -> tuple[list[tuple[int, SlotState]], dict[int, float]]:
    non_present = non_present or {}
    states = [(t, non_present.get(t, SlotState.PRESENT)) for t in range(read_start_ms(start, warmup, TF), end, TF)]
    closes = {t: close(t) for t, state in states if state is SlotState.PRESENT}
    return states, closes


def run(
    start: int = S, end: int = END, non_present: dict[int, SlotState] | None = None
) -> tuple[IndicatorSeries, Recorder]:
    states, closes = build(start, end, non_present)
    recorder = Recorder()
    series = evaluate(
        start=start,
        end=end,
        timeframe_ms=TF,
        warmup_slots=W,
        states=states,
        closes=closes,
        compute=recorder,
        output_count=1,
    )
    return series, recorder


def point(series: IndicatorSeries, open_time: int) -> IndicatorPoint:
    (found,) = [p for p in series.points if p.open_time_ms == open_time]
    return found


def first_value(series: IndicatorSeries) -> int:
    return next(p.open_time_ms for p in series.points if p.values is not None)


def assert_window(series: IndicatorSeries, start: int = S, end: int = END) -> None:
    assert [p.open_time_ms for p in series.points] == list(range(start, end, TF))
    for p in series.points:
        assert (p.values is None) != (p.reason is None)


def assert_values_from(series: IndicatorSeries, anchor: int, first: int, end: int = END) -> None:
    for t in range(first, end, TF):
        assert point(series, t) == IndicatorPoint(t, (direct(anchor, t),), None)


def assert_reason(series: IndicatorSeries, first: int, last_exclusive: int, reason: str) -> None:
    for t in range(first, last_exclusive, TF):
        assert point(series, t) == IndicatorPoint(t, None, reason)  # type: ignore[arg-type]


def mid_gap(state: SlotState) -> dict[int, SlotState]:
    return {slot(200): state}


def test_a1_full_history_anchors_w_before_start() -> None:
    series, recorder = run()
    assert_window(series)
    assert series.anchor_open_time_ms == slot(-140)
    assert all(p.values is not None for p in series.points)
    assert_values_from(series, slot(-140), S)
    assert recorder.calls == [[close(t) for t in range(slot(-140), END, TF)]]


def test_a2_missing_slot_before_the_read_range_does_not_matter() -> None:
    # the missing slot at s - 300 is before read_start_ms, so the caller never passes it
    assert read_start_ms(S, 140, TF) == S - 140 * TF
    assert slot(-300) < read_start_ms(S, 140, TF)
    a1, _ = run()
    a2, _ = run(non_present={slot(-300): SlotState.MISSING})
    assert a2 == a1


def test_a3_missing_slot_inside_the_warmup() -> None:
    series, _ = run(non_present={slot(-50): SlotState.MISSING})
    assert_window(series)
    assert series.anchor_open_time_ms == slot(-49)
    assert_reason(series, S, slot(91), "warmup")
    assert first_value(series) == slot(91)
    assert_values_from(series, slot(-49), slot(91))


def _gap_inside_the_window(state: SlotState, reason: str) -> None:
    series, _ = run(non_present=mid_gap(state))
    assert_window(series)
    assert series.anchor_open_time_ms == slot(-140)
    assert_values_from(series, slot(-140), S, slot(200))
    assert point(series, slot(200)) == IndicatorPoint(slot(200), None, reason)  # type: ignore[arg-type]
    assert_reason(series, slot(201), slot(341), "warmup")
    assert_values_from(series, slot(201), slot(341))
    # byte-identical to the same request on a database where everything before s + 201 is deleted
    deleted, _ = run(non_present={t: SlotState.MISSING for t in range(slot(-140), slot(201), TF)})
    assert deleted.points[201:] == series.points[201:]
    assert deleted.anchor_open_time_ms == slot(201)


def test_a4_missing_slot_inside_the_window_restarts_the_warmup() -> None:
    _gap_inside_the_window(SlotState.MISSING, "missing")


def test_a5_conflicted_slot_inside_the_window_restarts_the_warmup() -> None:
    _gap_inside_the_window(SlotState.CONFLICTED, "conflicted")


def test_a6_missing_start_starts_a_new_segment_after_it() -> None:
    series, _ = run(non_present={S: SlotState.MISSING})
    assert_window(series)
    assert point(series, S) == IndicatorPoint(S, None, "missing")
    assert series.anchor_open_time_ms == slot(1)
    assert_reason(series, slot(1), slot(141), "warmup")
    assert first_value(series) == slot(141)
    assert_values_from(series, slot(1), slot(141))


def test_a7_oldest_candle_inside_the_warmup() -> None:
    series, _ = run(non_present={t: SlotState.MISSING for t in range(slot(-140), slot(-20), TF)})
    assert_window(series)
    assert series.anchor_open_time_ms == slot(-20)
    assert_reason(series, S, slot(120), "warmup")
    assert first_value(series) == slot(120)
    assert_values_from(series, slot(-20), slot(120))


def test_a8_exactly_w_present_slots_before_a_point_give_a_value_and_w_minus_1_do_not() -> None:
    # exactly W present slots [s - 140, s) before the point s
    exact, _ = run()
    assert point(exact, S) == IndicatorPoint(S, (direct(slot(-140), S),), None)
    # W - 1 present slots [s - 139, s) before the point s
    short, _ = run(non_present={slot(-140): SlotState.MISSING})
    assert point(short, S) == IndicatorPoint(S, None, "warmup")
    assert point(short, slot(1)) == IndicatorPoint(slot(1), (direct(slot(-139), slot(1)),), None)
    # the same boundary inside the window, after a gap at s + 10
    gap, _ = run(non_present={slot(10): SlotState.MISSING})
    assert point(gap, slot(150)) == IndicatorPoint(slot(150), None, "warmup")
    assert point(gap, slot(151)) == IndicatorPoint(slot(151), (direct(slot(11), slot(151)),), None)


def test_a9_a_segment_shorter_than_period_plus_one_has_no_values() -> None:
    end = slot(20)
    present = set(range(S, slot(14), TF))  # one segment of 14 slots
    states, closes = build(
        end=end, non_present={t: SlotState.MISSING for t in range(slot(-140), end, TF) if t not in present}
    )
    calls: list[int] = []

    def nan_compute(values: Sequence[float]) -> Sequence[Sequence[float]]:
        calls.append(len(values))
        return ([float("nan")] * len(values),)

    series = evaluate(
        start=S,
        end=end,
        timeframe_ms=TF,
        warmup_slots=W,
        states=states,
        closes=closes,
        compute=nan_compute,
        output_count=1,
    )
    assert calls == [14]
    assert_window(series, end=end)
    assert all(p.values is None for p in series.points)
    assert_reason(series, S, slot(14), "warmup")
    assert_reason(series, slot(14), end, "missing")
    assert series.anchor_open_time_ms is None


def test_a10_indicators_are_evaluated_independently() -> None:
    # each evaluate call takes its own read range; a longer warm-up read for another indicator never reaches it
    states, closes = build(warmup=1000)
    own_start = read_start_ms(S, W, TF)
    own_states = [(t, state) for t, state in states if t >= own_start]
    own_closes = {t: c for t, c in closes.items() if t >= own_start}
    alone, _ = run()
    together = evaluate(
        start=S,
        end=END,
        timeframe_ms=TF,
        warmup_slots=W,
        states=own_states,
        closes=own_closes,
        compute=Recorder(),
        output_count=1,
    )
    assert together == alone


A12_START = T_MIN + 50 * TF
A12_END = T_MIN + 300 * TF

A11_CASES = {
    "a3": (S, END, {slot(-50): SlotState.MISSING}, [(slot(-140), slot(-50)), (slot(-49), END)]),
    "a4": (S, END, mid_gap(SlotState.MISSING), [(slot(-140), slot(200)), (slot(201), END)]),
    "a5": (S, END, mid_gap(SlotState.CONFLICTED), [(slot(-140), slot(200)), (slot(201), END)]),
    "a6": (S, END, {S: SlotState.MISSING}, [(slot(-140), S), (slot(1), END)]),
    "a12": (A12_START, A12_END, {}, [(T_MIN, A12_END)]),
    "a12_t_min": (T_MIN, A12_END, {}, [(T_MIN, A12_END)]),
}


@pytest.mark.parametrize("case", sorted(A11_CASES))
def test_a11_every_compute_call_is_exactly_one_segment(case: str) -> None:
    start, end, non_present, segments = A11_CASES[case]
    _, recorder = run(start, end, non_present)
    read_start = read_start_ms(start, W, TF)
    assert all(first >= read_start for first, _ in segments)
    assert recorder.calls == [[close(t) for t in range(first, last, TF)] for first, last in segments]


def test_a12_read_range_clamps_at_t_min() -> None:
    assert read_start_ms(A12_START, W, TF) == T_MIN
    series, _ = run(A12_START, A12_END)
    assert_window(series, A12_START, A12_END)
    assert series.anchor_open_time_ms == T_MIN
    assert_reason(series, A12_START, A12_START + 90 * TF, "warmup")
    assert first_value(series) == A12_START + 90 * TF
    assert_values_from(series, T_MIN, A12_START + 90 * TF, A12_END)

    assert read_start_ms(T_MIN, W, TF) == T_MIN
    at_t_min, _ = run(T_MIN, A12_END)
    assert at_t_min.anchor_open_time_ms == T_MIN
    assert_reason(at_t_min, T_MIN, T_MIN + 140 * TF, "warmup")
    assert first_value(at_t_min) == T_MIN + 140 * TF
    assert_values_from(at_t_min, T_MIN, T_MIN + 140 * TF, A12_END)


def test_read_start_ms() -> None:
    assert read_start_ms(S, 0, TF) == S
    assert read_start_ms(S, 3, TF) == S - 3 * TF
    assert read_start_ms(T_MIN + TF, 2, TF) == T_MIN


@pytest.mark.parametrize("state", [s for s in SlotState if s is not SlotState.PRESENT])
def test_reasons_follow_slot_states(state: SlotState) -> None:
    series, _ = run(non_present={slot(5): state})
    assert point(series, slot(5)) == IndicatorPoint(slot(5), None, state.value)  # type: ignore[arg-type]


def test_warmup_zero_gives_a_value_at_every_present_slot() -> None:
    states = [(slot(0), SlotState.PRESENT), (slot(1), SlotState.DUE), (slot(2), SlotState.PRESENT)]
    series = evaluate(
        start=S,
        end=slot(3),
        timeframe_ms=TF,
        warmup_slots=0,
        states=states,
        closes={slot(0): 2.0, slot(2): 5.0},
        compute=Recorder(),
        output_count=1,
    )
    assert series == IndicatorSeries(
        anchor_open_time_ms=S,
        points=(
            IndicatorPoint(slot(0), (2.0,), None),
            IndicatorPoint(slot(1), None, "due"),
            IndicatorPoint(slot(2), (5.0,), None),
        ),
    )


def test_multiple_outputs_are_carried_in_order() -> None:
    states = [(slot(0), SlotState.PRESENT), (slot(1), SlotState.PRESENT)]

    def two(values: Sequence[float]) -> Sequence[Sequence[float]]:
        return ([v + 1 for v in values], [v * 10 for v in values])

    series = evaluate(
        start=S,
        end=slot(2),
        timeframe_ms=TF,
        warmup_slots=0,
        states=states,
        closes={slot(0): 1.0, slot(1): 2.0},
        compute=two,
        output_count=2,
    )
    assert [p.values for p in series.points] == [(2.0, 10.0), (3.0, 20.0)]


def bad_compute_wrong_length(values: Sequence[float]) -> Sequence[Sequence[float]]:
    return (list(values)[:-1],)


def bad_compute_wrong_outputs(values: Sequence[float]) -> Sequence[Sequence[float]]:
    return (list(values), list(values))


def inconsistent_cases() -> dict[str, dict[str, object]]:
    states, closes = build(end=slot(5))
    return {
        "states_not_at_read_start": {"states": states[1:]},
        "states_shifted_by_one_slot": {"states": [(t + TF, state) for t, state in states]},
        "states_before_read_start": {"states": [(slot(-141), SlotState.MISSING), *states]},
        "states_short_of_end": {"states": states[:-1]},
        "states_past_end": {"states": [*states, (slot(5), SlotState.PRESENT)]},
        "states_out_of_order": {"states": [states[1], states[0], *states[2:]]},
        "close_for_a_non_present_slot": {
            "states": [*states[:-1], (slot(4), SlotState.MISSING)],
        },
        "missing_close_for_a_present_slot": {"closes": {t: c for t, c in closes.items() if t != S}},
        "close_outside_the_states": {"closes": {**closes, slot(9): 1.0}},
        "compute_wrong_length": {"compute": bad_compute_wrong_length},
        "compute_wrong_output_count": {"compute": bad_compute_wrong_outputs},
        "empty_window": {"end": S, "states": states[:140]},
        "misaligned_start": {"start": S + 1},
        "negative_warmup": {"warmup_slots": -1},
        "zero_timeframe": {"timeframe_ms": 0},
        "zero_output_count": {"output_count": 0},
    }


@pytest.mark.parametrize("case", sorted(inconsistent_cases()))
def test_inconsistent_inputs_raise(case: str) -> None:
    states, closes = build(end=slot(5))
    kwargs: dict[str, object] = {
        "start": S,
        "end": slot(5),
        "timeframe_ms": TF,
        "warmup_slots": W,
        "states": states,
        "closes": closes,
        "compute": Recorder(),
        "output_count": 1,
    }
    kwargs.update(inconsistent_cases()[case])
    with pytest.raises(ValueError):
        evaluate(**kwargs)  # type: ignore[arg-type]
