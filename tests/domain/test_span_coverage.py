import random

import pytest

from quasar_market_data.domain.span_coverage import SpanCoverage, classify_span, span_slot_states

TF = 300_000
S = 1792643400000  # aligned


def slot(n: int) -> int:
    return S + n * TF


def classify(
    n_slots: int,
    *,
    candles: set[int] | None = None,
    conflicts: set[int] | None = None,
    unavailable: set[int] | None = None,
    newest: int,
) -> SpanCoverage:
    return classify_span(
        start=S,
        end=slot(n_slots),
        timeframe_ms=TF,
        candles=candles or set(),
        conflicts=conflicts or set(),
        unavailable=unavailable or set(),
        newest_open_time_ms=newest,
    )


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"candles": {slot(0)}, "newest": slot(0)}, "present"),
        ({"conflicts": {slot(0)}, "newest": slot(0)}, "conflicted"),
        ({"unavailable": {slot(0)}, "newest": slot(0)}, "unavailable"),
        ({"newest": slot(5)}, "missing"),  # no data, before the newest candle
        ({"newest": slot(0)}, "missing"),  # no data, slot equal to newest
        ({"newest": slot(-1)}, "beyond_stored"),  # slot at newest + tf
        ({"newest": slot(-10)}, "beyond_stored"),
    ],
)
def test_each_state_in_isolation(kwargs: dict[str, set[int] | int], field: str) -> None:
    result = classify(1, **kwargs)  # type: ignore[arg-type]

    assert result.slots == 1
    assert getattr(result, field) == 1
    others = {"present", "conflicted", "unavailable", "missing", "beyond_stored"} - {field}
    assert all(getattr(result, name) == 0 for name in others)


def test_slot_before_the_oldest_candle_is_missing() -> None:
    result = classify(3, candles={slot(2)}, newest=slot(2))

    assert (result.missing, result.present) == (2, 1)


def test_candle_with_a_conflict_record_is_conflicted_once() -> None:
    result = classify(1, candles={slot(0)}, conflicts={slot(0)}, newest=slot(0))

    assert (result.conflicted, result.present, result.slots) == (1, 0, 1)
    assert result.conflicted_open_times == frozenset({slot(0)})


def test_candle_and_unavailable_record_is_conflicted_not_unavailable() -> None:
    result = classify(1, candles={slot(0)}, unavailable={slot(0)}, newest=slot(0))

    assert (result.conflicted, result.unavailable, result.present) == (1, 0, 0)
    assert result.conflicted_open_times == frozenset({slot(0)})


def test_unavailable_only_is_unavailable_and_never_present() -> None:
    result = classify(1, unavailable={slot(0)}, newest=slot(0))

    assert (result.unavailable, result.present) == (1, 0)


def test_conflict_without_candle_is_counted_conflicted() -> None:
    result = classify(2, conflicts={slot(1)}, newest=slot(1))

    assert result.conflicted == 1
    assert result.missing == 1


def test_unavailable_or_conflict_beyond_the_newest_candle_keeps_its_state() -> None:
    result = classify(2, unavailable={slot(0)}, conflicts={slot(1)}, newest=slot(-1))

    assert (result.unavailable, result.conflicted, result.beyond_stored) == (1, 1, 0)


def test_counts_always_sum_to_slots() -> None:
    rng = random.Random(0)
    for _ in range(200):
        n = rng.randint(1, 40)
        indexes = range(n)
        candles = {slot(i) for i in indexes if rng.random() < 0.5}
        conflicts = {slot(i) for i in indexes if rng.random() < 0.2}
        unavailable = {slot(i) for i in indexes if rng.random() < 0.2}
        newest = slot(rng.randint(-5, n + 5))

        result = classify(n, candles=candles, conflicts=conflicts, unavailable=unavailable, newest=newest)

        assert result.slots == n
        assert result.present + result.conflicted + result.unavailable + result.missing + result.beyond_stored == n
        assert result.conflicted == len(result.conflicted_open_times)


def test_gap_free_is_true_only_for_a_fully_present_span() -> None:
    full = {slot(i) for i in range(4)}
    assert classify(4, candles=full, newest=slot(3)).gap_free is True
    assert classify(4, candles=full - {slot(1)}, newest=slot(3)).gap_free is False  # missing
    assert classify(4, candles=full - {slot(1)}, unavailable={slot(1)}, newest=slot(3)).gap_free is False
    assert classify(4, candles=full, conflicts={slot(1)}, newest=slot(3)).gap_free is False
    assert classify(4, candles=full - {slot(3)}, newest=slot(2)).gap_free is False  # beyond stored


@pytest.mark.parametrize(
    "case",
    ["empty_span", "start_unaligned", "end_unaligned", "element_outside", "element_unaligned", "bad_timeframe"],
)
def test_invalid_input_raises(case: str) -> None:
    args: dict[str, object] = {
        "start": S,
        "end": slot(3),
        "timeframe_ms": TF,
        "candles": set(),
        "conflicts": set(),
        "unavailable": set(),
        "newest_open_time_ms": slot(3),
    }
    if case == "empty_span":
        args["end"] = S
    elif case == "start_unaligned":
        args["start"] = S + 1
    elif case == "end_unaligned":
        args["end"] = slot(3) + 1
    elif case == "element_outside":
        args["candles"] = {slot(3)}
    elif case == "element_unaligned":
        args["conflicts"] = {slot(1) + 1}
    else:
        args["timeframe_ms"] = 0

    with pytest.raises(ValueError):
        classify_span(**args)  # type: ignore[arg-type]


def test_span_slot_states_yields_every_slot_in_order() -> None:
    states = span_slot_states(
        start=S,
        end=slot(6),
        timeframe_ms=TF,
        candles={slot(0), slot(4)},
        conflicts={slot(1)},
        unavailable={slot(2)},
        newest_open_time_ms=slot(4),
    )

    assert list(states) == [
        (slot(0), "present"),
        (slot(1), "conflicted"),
        (slot(2), "unavailable"),
        (slot(3), "missing"),
        (slot(4), "present"),
        (slot(5), "beyond_stored"),
    ]


@pytest.mark.parametrize("case", ["start_unaligned", "empty_span", "element_outside"])
def test_span_slot_states_validates_before_the_first_item(case: str) -> None:
    args = {
        "start": S,
        "end": slot(3),
        "timeframe_ms": TF,
        "candles": set[int](),
        "conflicts": set[int](),
        "unavailable": set[int](),
        "newest_open_time_ms": slot(3),
    }
    if case == "start_unaligned":
        args["start"] = S + 1
    elif case == "empty_span":
        args["end"] = S
    else:
        args["candles"] = {slot(3)}

    with pytest.raises(ValueError):
        span_slot_states(**args)  # type: ignore[arg-type]
