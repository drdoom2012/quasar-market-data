import random

import pytest

from quasar_market_data.domain.open_interest import period_end_count
from quasar_market_data.domain.open_interest_holes import (
    Page,
    holes_in,
    newest_expected_ms,
    oldest_allowed_ms,
    plan_pages,
    slots_at_risk,
)
from quasar_market_data.domain.open_interest_request import MAX_PERIOD_ENDS, RETRIEVAL_HORIZON_MS
from quasar_market_data.domain.timeframe import Timeframe

P = Timeframe("5m")
STEP = P.milliseconds
BASE = 1_792_677_000_000 // STEP * STEP
AS_OF = BASE + 123_456  # deliberately unaligned


def test_oldest_and_newest_follow_the_adr() -> None:
    oldest = oldest_allowed_ms(AS_OF, P)
    assert oldest % STEP == 0 and AS_OF - RETRIEVAL_HORIZON_MS <= oldest < AS_OF - RETRIEVAL_HORIZON_MS + STEP
    newest = newest_expected_ms(AS_OF, 1, 200_000, P)
    assert newest % STEP == 0 and AS_OF - 500_000 - STEP < newest <= AS_OF - 500_000
    assert period_end_count(oldest, newest, P) > 8000  # about 29 days of slots


def test_holes_exclude_stored_and_conflicted_slots_and_include_both_bounds() -> None:
    slots = [1_000 * STEP + i * STEP for i in range(10)]
    assert holes_in(slots[1:3], slots[5:6], slots[0], slots[-1], P) == (slots[0], slots[3], slots[4], *slots[6:])
    assert holes_in([], [], slots[0], slots[0], P) == (slots[0],)
    assert holes_in(slots, [], slots[0], slots[-1], P) == ()
    assert holes_in([], [], slots[3], slots[2], P) == ()


def test_holes_reject_unaligned_bounds() -> None:
    with pytest.raises(ValueError):
        holes_in([], [], STEP + 1, 5 * STEP, P)
    with pytest.raises(ValueError):
        holes_in([], [], STEP, 5 * STEP + 1, P)


def test_a_fresh_install_has_about_8352_holes_in_17_pages() -> None:
    oldest = oldest_allowed_ms(AS_OF, P)
    newest = oldest + 8351 * STEP
    holes = holes_in([], [], oldest, newest, P)
    assert len(holes) == 8352
    pages = plan_pages({"BTC/USDT:USDT": holes}, P)
    assert len(pages) == 17
    assert all(page.period_ends(P) <= MAX_PERIOD_ENDS for page in pages)
    assert sorted(slot for page in pages for slot in page.slots) == list(holes)  # each hole exactly once


def test_a_single_hole_is_one_page_of_one_period_end() -> None:
    (page,) = plan_pages({"S/USDT:USDT": [BASE + 10 * STEP]}, P)
    assert (page.start_ms, page.end_ms, page.period_ends(P)) == (BASE + 10 * STEP, BASE + 10 * STEP, 1)


def test_holes_600_slots_apart_make_two_pages_not_one() -> None:
    pages = plan_pages({"S/USDT:USDT": [STEP, 601 * STEP]}, P)
    assert [(p.start_ms, p.end_ms) for p in pages] == [(STEP, STEP), (601 * STEP, 601 * STEP)]


def test_a_page_spans_stored_slots_between_holes_but_not_more_than_500_period_ends() -> None:
    (page,) = plan_pages({"S/USDT:USDT": [BASE + STEP, BASE + 500 * STEP]}, P)
    assert page.period_ends(P) == 500 and page.slots == (BASE + STEP, BASE + 500 * STEP)
    two = plan_pages({"S/USDT:USDT": [BASE + STEP, BASE + 501 * STEP]}, P)
    assert len(two) == 2


def test_pages_are_ordered_by_start_then_symbol_whatever_the_input_order() -> None:
    available = {"B/USDT:USDT": [5 * STEP], "A/USDT:USDT": [5 * STEP, 900 * STEP], "C/USDT:USDT": [2 * STEP]}
    expected = [
        ("C/USDT:USDT", 2 * STEP),
        ("A/USDT:USDT", 5 * STEP),
        ("B/USDT:USDT", 5 * STEP),
        ("A/USDT:USDT", 900 * STEP),
    ]
    keys = list(available)
    for _ in range(6):
        random.Random(len(keys)).shuffle(keys)
        shuffled = {k: available[k] for k in keys}
        assert [(p.symbol, p.start_ms) for p in plan_pages(shuffled, P)] == expected
        keys = keys[1:] + keys[:1]


def test_the_plan_is_deterministic() -> None:
    rng = random.Random(7)
    available = {f"S{i}/USDT:USDT": sorted(rng.sample(range(1000, 6000), 300)) for i in range(5)}
    shuffled = {k: available[k] for k in reversed(list(available))}
    first = plan_pages({k: [s * STEP for s in v] for k, v in available.items()}, P)
    second = plan_pages({k: [s * STEP for s in v] for k, v in shuffled.items()}, P)
    assert first == second


def test_slots_must_ascend_and_be_aligned() -> None:
    with pytest.raises(ValueError):
        plan_pages({"S/USDT:USDT": [2 * STEP, STEP]}, P)
    with pytest.raises(ValueError):
        plan_pages({"S/USDT:USDT": [STEP, STEP]}, P)
    with pytest.raises(ValueError):
        plan_pages({"S/USDT:USDT": [STEP + 1]}, P)


def test_a_long_outage_requests_nothing_older_than_the_horizon_and_oldest_first() -> None:
    oldest = oldest_allowed_ms(AS_OF, P)
    newest = newest_expected_ms(AS_OF, 1, 200_000, P)
    last_stored = AS_OF - 40 * 86_400_000 // STEP * STEP
    stored = [last_stored - i * STEP for i in range(50)]
    holes = holes_in(stored, [], oldest, newest, P)
    pages = plan_pages({"S/USDT:USDT": holes}, P)
    assert min(p.start_ms for p in pages) >= oldest and holes[0] == oldest
    assert [p.start_ms for p in pages] == sorted(p.start_ms for p in pages)


def pages_of_one(count: int, slot: int) -> list[Page]:
    return list(plan_pages({f"S{i:02d}/USDT:USDT": [slot] for i in range(count)}, P))


def test_a_hole_that_cannot_be_reached_before_it_leaves_is_at_risk() -> None:
    as_of = 1_792_677_000_000 // STEP * STEP + RETRIEVAL_HORIZON_MS  # as_of - H is aligned
    horizon_start = as_of - RETRIEVAL_HORIZON_MS
    slot = horizon_start + 10 * STEP  # leaves in 10 cycles
    pages = pages_of_one(12, slot)
    one = slots_at_risk(pages, as_of, 1, P)
    assert set(one) == {"S11/USDT:USDT"}  # window 1 is now: 10 cycles left means 11 windows; position 12 is the 12th
    assert slots_at_risk(pages, as_of, 2, P) == {}


def test_slots_newer_than_the_margin_are_never_at_risk() -> None:
    as_of = 1_792_677_000_000 // STEP * STEP + RETRIEVAL_HORIZON_MS
    slot = as_of - RETRIEVAL_HORIZON_MS + 86_400_000  # exactly one day left: not judged
    assert slots_at_risk(pages_of_one(1000, slot), as_of, 1, P) == {}
