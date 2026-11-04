import math
import random
from dataclasses import replace

import pytest

from quasar_market_data.domain.funding_rate_capacity import RequestClass, history_catchup_share
from quasar_market_data.domain.funding_rate_scheduler import (
    ClassTurn,
    HistoryChunk,
    Plan,
    Selection,
    SeriesStatus,
    select_next,
)
from quasar_market_data.domain.funding_rate_span import DAY_MS, HISTORY_CHUNK_MS, Interval

HOUR = 3_600_000
B_NOW = 1_793_836_800_000
RECHECK = 86_400_000
DAYS = 365
NOW = 1_000_000


def status(symbol: str, **changes: object) -> SeriesStatus:
    base = SeriesStatus(
        symbol=symbol,
        span=Interval(B_NOW - 10 * HOUR, B_NOW - HOUR),
        verified_at_ms=B_NOW - HOUR,
        busy=False,
        unavailable_until_ms=0,
        continuing_from_ms=None,
        history_chunk=None,
        never_served=False,
        last_served=0,
        asked_in_round=False,
        needs_reread=False,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def pick(series: list[SeriesStatus], **overrides: object) -> Selection | None:
    arguments: dict[str, object] = {
        "now_ms": NOW,
        "b_now_ms": B_NOW,
        "round_active": True,
        "r_room": True,
        "ch_room": True,
        "turn": "C",
        "recheck_ms": RECHECK,
        "backfill_days": DAYS,
    }
    arguments.update(overrides)
    return select_next(series, **arguments)  # type: ignore[arg-type]


def unseeded(symbol: str, **changes: object) -> SeriesStatus:
    return status(symbol, span=None, verified_at_ms=None, **changes)


def finished(symbol: str, **changes: object) -> SeriesStatus:
    """A series whose history is as wide as the configured depth: no H candidate."""
    return status(symbol, **{"span": Interval(B_NOW - DAYS * DAY_MS - HOUR, B_NOW - HOUR), **changes})


def test_unseeded_series_come_first_and_are_class_s_in_symbol_order() -> None:
    series = [status("A"), unseeded("C"), unseeded("B")]
    selection = pick(series)
    assert selection is not None
    plan = selection.plan
    assert (plan.symbol, plan.kind, plan.request_class) == ("B", "S", RequestClass.R)
    assert (plan.start_ms, plan.end_ms) == (B_NOW - RECHECK, B_NOW)


def test_an_unseeded_series_beats_an_active_one_even_with_the_oldest_verified_at_and_an_earlier_symbol() -> None:
    selection = pick([status("A", verified_at_ms=0), unseeded("Z")])
    assert selection is not None and (selection.plan.symbol, selection.plan.kind) == ("Z", "S")


def test_r_follows_ascending_verified_at_then_symbol_and_uses_the_recheck_range() -> None:
    series = [
        status("B", verified_at_ms=500),
        status("C", verified_at_ms=100),
        status("A", verified_at_ms=100),
        status("D", verified_at_ms=900),
    ]
    order = []
    while series:
        selection = pick(series, ch_room=False)
        assert selection is not None
        order.append(selection.plan.symbol)
        series = [replace(s, asked_in_round=True) if s.symbol == selection.plan.symbol else s for s in series]
        series = [s for s in series if not s.asked_in_round]
    assert order == ["A", "C", "B", "D"]
    selection = pick([status("A", span=Interval(B_NOW - 5 * DAY_MS, B_NOW - HOUR))])
    assert selection is not None and selection.plan.kind == "R"
    assert (selection.plan.start_ms, selection.plan.end_ms) == (B_NOW - HOUR - RECHECK, B_NOW)
    young = status("A", span=Interval(B_NOW - 10 * HOUR, B_NOW - HOUR))  # from_ms is above through - recheck
    selection = pick([young])
    assert selection is not None and selection.plan.start_ms == B_NOW - 10 * HOUR


@pytest.mark.parametrize(
    "change",
    [
        {"busy": True},
        {"unavailable_until_ms": NOW + 1},
        {"asked_in_round": True},
        {"needs_reread": True},
    ],
    ids=["busy", "unavailable", "asked", "needs_reread"],
)
def test_ineligible_series_are_skipped_in_every_class(change: dict[str, object]) -> None:
    only_asked = change == {"asked_in_round": True}  # "asked in this round" matters for S and R only
    assert pick([finished("A", **change)]) is None  # R: no
    c_series = status("A", continuing_from_ms=B_NOW - 8 * HOUR, history_chunk=None, **change)
    c_selection = pick([replace(c_series, span=Interval(B_NOW - DAYS * DAY_MS - HOUR, B_NOW - HOUR))])
    assert (c_selection is not None) == only_asked and (c_selection is None or c_selection.plan.kind == "C")
    h_selection = pick([status("A", **change)], round_active=False)  # H: a young span has history pending
    assert (h_selection is not None) == only_asked and (h_selection is None or h_selection.plan.kind == "H")
    assert pick([finished("A", unavailable_until_ms=NOW)]) is not None  # available exactly at the instant


def test_no_round_means_no_s_or_r_and_no_r_room_falls_through() -> None:
    series = [unseeded("A"), finished("B")]
    assert pick(series, round_active=False) is None
    assert pick(series, r_room=False) is None
    history = pick([status("H1")], r_room=False)
    assert history is not None and history.plan.kind == "H"


def test_without_b_now_only_history_is_planned() -> None:
    assert (
        pick([unseeded("A"), finished("B"), finished("C", continuing_from_ms=B_NOW - 3 * HOUR)], b_now_ms=None) is None
    )
    selection = pick([status("H1")], b_now_ms=None)
    assert selection is not None and selection.plan.kind == "H"


def test_a_range_that_would_start_after_b_now_is_not_planned() -> None:
    caught_up = finished("A", span=Interval(B_NOW + 5, B_NOW + 10))  # the whole span is above b_now
    assert pick([caught_up], ch_room=False) is None  # R: nothing to ask
    assert pick([finished("A", continuing_from_ms=B_NOW + 1)]) is None


def test_a_behind_series_without_the_flag_is_an_r_candidate_not_c() -> None:
    behind = finished("A", span=Interval(B_NOW - DAYS * DAY_MS - 5 * HOUR, B_NOW - 5 * HOUR))
    selection = pick([behind])
    assert selection is not None and selection.plan.kind == "R"
    assert pick([behind], round_active=False) is None  # no C without the continuing flag


def test_a_skipped_series_is_first_in_the_next_round() -> None:
    skipped = status("Z", verified_at_ms=B_NOW - 2 * HOUR)  # not verified last round
    fresh = [status(f"A{i}", verified_at_ms=B_NOW - HOUR + i) for i in range(5)]
    selection = pick([*fresh, skipped])
    assert selection is not None and selection.plan.symbol == "Z"


def test_class_c_continues_at_the_flag_and_orders_by_verified_at() -> None:
    series = [
        finished("B", continuing_from_ms=B_NOW - 9 * HOUR, verified_at_ms=200),
        finished("A", continuing_from_ms=B_NOW - 8 * HOUR, verified_at_ms=300),
        finished("C", continuing_from_ms=B_NOW - 7 * HOUR, verified_at_ms=100),
    ]
    selection = pick(series, round_active=False)
    assert selection is not None
    plan = selection.plan
    assert (plan.symbol, plan.kind, plan.request_class) == ("C", "C", RequestClass.C)
    assert (plan.start_ms, plan.end_ms) == (B_NOW - 7 * HOUR, B_NOW)
    assert selection.turn == "H"


def test_a_continuing_series_is_not_an_r_candidate() -> None:
    assert pick([finished("A", continuing_from_ms=B_NOW - 8 * HOUR)], ch_room=False) is None


def test_class_h_plans_the_exact_ninety_day_chunk_and_the_last_chunk_ends_at_the_target() -> None:
    span = Interval(B_NOW - 5 * DAY_MS, B_NOW - HOUR)
    selection = pick([status("A", span=span)], round_active=False)
    assert selection is not None
    plan = selection.plan
    assert plan.kind == "H" and plan.request_class is RequestClass.H
    assert (plan.start_ms, plan.end_ms) == (span.from_ms - HISTORY_CHUNK_MS, span.from_ms - 1)
    assert plan.chunk == HistoryChunk(plan.start_ms, plan.end_ms, plan.start_ms)
    target = span.through_ms - DAYS * DAY_MS
    near = Interval(target + 10 * DAY_MS, span.through_ms)
    last = pick([status("A", span=near)], round_active=False)
    assert last is not None and last.plan.start_ms == target and last.plan.end_ms == near.from_ms - 1


def test_a_chunk_in_progress_continues_with_its_next_page() -> None:
    chunk = HistoryChunk(B_NOW - 100 * DAY_MS, B_NOW - 10 * DAY_MS, B_NOW - 50 * DAY_MS)
    selection = pick([status("A", history_chunk=chunk)], round_active=False)
    assert selection is not None
    assert (selection.plan.start_ms, selection.plan.end_ms, selection.plan.chunk) == (
        chunk.next_start_ms,
        chunk.through_ms,
        chunk,
    )


def test_history_order_chunk_in_progress_then_never_served_then_last_served_then_symbol() -> None:
    chunk = HistoryChunk(B_NOW - 100 * DAY_MS, B_NOW - 10 * DAY_MS, B_NOW - 50 * DAY_MS)
    series = [
        status("A", last_served=1),
        status("B", last_served=5, never_served=True),
        status("C", last_served=0, never_served=True),
        status("D", last_served=9, history_chunk=chunk),
        status("E", last_served=2),
    ]
    order = []
    remaining = list(series)
    while remaining:
        selection = pick(remaining, round_active=False, turn="H")
        assert selection is not None
        order.append(selection.plan.symbol)
        remaining = [s for s in remaining if s.symbol != selection.plan.symbol]
    assert order == ["D", "C", "B", "A", "E"]


def test_turn_alternates_between_c_and_h_while_both_have_work_and_starts_at_c() -> None:
    series = [
        finished("C1", continuing_from_ms=B_NOW - 8 * HOUR, verified_at_ms=1),
        finished("C2", continuing_from_ms=B_NOW - 8 * HOUR, verified_at_ms=2),
        status("H1"),
        status("H2"),
    ]
    turn: ClassTurn = "C"
    granted = []
    for _ in range(4):
        selection = pick(series, round_active=False, turn=turn)
        assert selection is not None
        granted.append(selection.plan.kind)
        turn = selection.turn
    assert granted == ["C", "H", "C", "H"]


def test_an_empty_class_is_skipped_and_the_turn_names_the_other() -> None:
    only_h = pick([status("H1")], round_active=False, turn="C")
    assert only_h is not None and only_h.plan.kind == "H" and only_h.turn == "C"
    only_c = pick([finished("C1", continuing_from_ms=B_NOW - 8 * HOUR)], round_active=False, turn="H")
    assert only_c is not None and only_c.plan.kind == "C" and only_c.turn == "H"
    assert pick([finished("X")], round_active=False) is None


def test_no_ch_room_means_no_catch_up_or_history() -> None:
    assert pick([status("H1")], round_active=False, ch_room=False) is None


def test_the_round_keeps_the_turn_it_was_given() -> None:
    selection = pick([unseeded("A")], turn="H")
    assert selection is not None and selection.turn == "H"


def test_near_the_minimum_timestamp_history_is_not_planned() -> None:
    assert pick([status("A", span=Interval(946_684_800_000 + 10, B_NOW))], round_active=False) is None


def test_determinism_independent_of_the_input_order() -> None:
    rng = random.Random(7)
    series = [
        status(f"S{i:02d}", verified_at_ms=B_NOW - rng.randint(1, 5) * HOUR, last_served=rng.randint(0, 9))
        for i in range(30)
    ]
    expected = pick(series)
    for _ in range(20):
        shuffled = list(series)
        rng.shuffle(shuffled)
        assert pick(shuffled) == expected


def reference(
    series: list[SeriesStatus], round_active: bool, r_room: bool, ch_room: bool, turn: str
) -> tuple[str, str] | None:
    """A second, naive implementation: (symbol, kind) of the next request."""
    ok = [s for s in series if not s.busy and s.unavailable_until_ms <= NOW and not s.needs_reread]
    if round_active and r_room:
        pool = [s for s in ok if not s.asked_in_round and (s.span is None or s.continuing_from_ms is None)]
        pool = [s for s in pool if s.span is None or max(s.span.from_ms, s.span.through_ms - RECHECK) <= B_NOW]
        if pool:
            best = min(pool, key=lambda s: (s.span is not None, s.verified_at_ms or 0, s.symbol))
            return best.symbol, "S" if best.span is None else "R"
    if not ch_room:
        return None
    c = [s for s in ok if s.continuing_from_ms is not None and s.continuing_from_ms <= B_NOW]
    h = [
        s
        for s in ok
        if s.span is not None and (s.history_chunk is not None or s.span.from_ms > s.span.through_ms - DAYS * DAY_MS)
    ]
    best_c = min(c, key=lambda s: (s.verified_at_ms or 0, s.symbol), default=None)
    best_h = min(h, key=lambda s: (s.history_chunk is None, not s.never_served, s.last_served, s.symbol), default=None)
    for cls in ("C", "H") if turn == "C" else ("H", "C"):
        if cls == "C" and best_c is not None:
            return best_c.symbol, "C"
        if cls == "H" and best_h is not None:
            return best_h.symbol, "H"
    return None


@pytest.mark.parametrize("seed", range(6))
def test_against_a_naive_reference_model(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(200):
        series = []
        for i in range(rng.randint(0, 12)):
            width = rng.choice([HOUR, 10 * DAY_MS, DAYS * DAY_MS + HOUR])
            through = B_NOW - rng.randint(1, 4) * HOUR
            span = None if rng.random() < 0.2 else Interval(through - width, through)
            chunk = None
            if span is not None and rng.random() < 0.2 and width > 5 * DAY_MS:
                chunk = HistoryChunk(span.from_ms - 30 * DAY_MS, span.from_ms - 1, span.from_ms - 20 * DAY_MS)
            series.append(
                SeriesStatus(
                    symbol=f"S{i:02d}",
                    span=span,
                    verified_at_ms=None if span is None else B_NOW - rng.randint(1, 9) * HOUR,
                    busy=rng.random() < 0.15,
                    unavailable_until_ms=rng.choice([0, 0, 0, NOW + 5]),
                    continuing_from_ms=None
                    if span is None or rng.random() < 0.7
                    else through - rng.randint(0, 5) * HOUR,
                    history_chunk=chunk,
                    never_served=rng.random() < 0.3,
                    last_served=rng.randint(0, 5),
                    asked_in_round=rng.random() < 0.3,
                    needs_reread=rng.random() < 0.1,
                )
            )
        flags = (rng.random() < 0.6, rng.random() < 0.7, rng.random() < 0.8)
        turn: ClassTurn = rng.choice(["C", "H"])
        got = pick(series, round_active=flags[0], r_room=flags[1], ch_room=flags[2], turn=turn)
        want = reference(series, flags[0], flags[1], flags[2], turn)
        assert (None if got is None else (got.plan.symbol, got.plan.kind)) == want


@pytest.mark.parametrize("b", [50, 150, 250])
def test_history_cannot_starve_behind_catch_up_and_the_reverse(b: int) -> None:
    per_window = history_catchup_share(b)  # C+H selections per 305 s window at saturation
    h_per_window = per_window // 2
    c_series = [
        finished(f"C{i:03d}", continuing_from_ms=B_NOW - 8 * HOUR, verified_at_ms=i, last_served=0) for i in range(200)
    ]
    h_series = [status(f"H{i:03d}", never_served=True, last_served=0) for i in range(300)]
    state = {s.symbol: s for s in c_series + h_series}
    turn: ClassTurn = "C"
    sequence: list[str] = []
    first_chunk_window: dict[str, int] = {}
    clock = 0
    counts_per_window: list[tuple[int, int]] = []
    for window in range(50):
        c_count = h_count = 0
        for _ in range(per_window):
            selection = pick(list(state.values()), round_active=False, turn=turn)
            assert selection is not None
            turn = selection.turn
            plan = selection.plan
            clock += 1
            current = state[plan.symbol]
            if plan.kind == "H":
                h_count += 1
                first_chunk_window.setdefault(plan.symbol, window)
                state[plan.symbol] = replace(current, never_served=False, last_served=clock)
            else:
                c_count += 1
                state[plan.symbol] = replace(current, verified_at_ms=B_NOW + clock)  # served: now the newest
            sequence.append(plan.kind)
        counts_per_window.append((c_count, h_count))
    for n in range(1, len(sequence) + 1):  # every prefix: H gets at least floor(n / 2)
        assert sequence[:n].count("H") >= n // 2
    assert all(h >= h_per_window and c >= h_per_window for c, h in counts_per_window)
    bound = math.ceil(len(h_series) / h_per_window)
    served = [first_chunk_window[s.symbol] for s in h_series if s.symbol in first_chunk_window]
    assert len(served) == min(len(h_series), 50 * h_per_window) and max(served) < bound


@pytest.mark.parametrize("b", [50, 150])
def test_with_c_empty_h_uses_the_whole_share_and_with_h_empty_c_does(b: int) -> None:
    per_window = history_catchup_share(b)
    only_h = [status(f"H{i:03d}", never_served=True) for i in range(per_window + 5)]
    seen = []
    state = {s.symbol: s for s in only_h}
    turn: ClassTurn = "C"
    for step in range(per_window):
        selection = pick(list(state.values()), round_active=False, turn=turn)
        assert selection is not None and selection.plan.kind == "H"
        turn = selection.turn
        seen.append(selection.plan.symbol)
        state[selection.plan.symbol] = replace(state[selection.plan.symbol], never_served=False, last_served=step + 1)
    assert len(set(seen)) == per_window
    only_c = [finished(f"C{i:03d}", continuing_from_ms=B_NOW - 8 * HOUR, verified_at_ms=i) for i in range(5)]
    assert all(pick(only_c, round_active=False, turn=t) is not None for t in ("C", "H"))


def test_a_new_symbol_is_selected_in_h_before_any_already_served_series_second_chunk() -> None:
    served = [status(f"A{i}", never_served=False, last_served=i + 1) for i in range(5)]
    new = status("Z", never_served=True, last_served=0)
    selection = pick([*served, new], round_active=False, turn="H")
    assert selection is not None and selection.plan.symbol == "Z"


def test_plan_is_a_frozen_value() -> None:
    plan = Plan("A", "R", RequestClass.R, 1, 2, None)
    assert plan == Plan("A", "R", RequestClass.R, 1, 2, None)
    with pytest.raises(AttributeError):
        plan.symbol = "B"  # type: ignore[misc]
    with pytest.raises(ValueError):
        HistoryChunk(10, 20, 21)
