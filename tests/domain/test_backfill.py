import pytest

from quasar_market_data.domain.backfill import (
    Attribution,
    BackfillTarget,
    PageKind,
    PagePlan,
    PageViolation,
    RecoveryResult,
    TargetResult,
    attribute_recovery,
    judge_page,
    judge_target,
    last_open_ms,
    max_pages,
    plan_page,
    plan_targets,
    slot_count,
)
from quasar_market_data.domain.timestamps import MIN_TIMESTAMP_MS

TF = 300_000
S0 = MIN_TIMESTAMP_MS + 1000 * TF


def slot(i: int) -> int:
    return S0 + i * TF


def target(a: int, b: int) -> BackfillTarget:
    return BackfillTarget(slot(a), slot(b))


def test_targets_are_newest_first_and_clipped_to_the_horizon() -> None:
    runs = [(slot(80), slot(85)), (slot(40), slot(45)), (slot(0), slot(12))]
    got = plan_targets(
        horizon_start_ms=slot(10),
        forming_open_ms=slot(100),
        oldest_stored_ms=slot(30),
        interior_runs=runs,
        timeframe_ms=TF,
    )
    assert got == (target(80, 85), target(40, 45), target(10, 12), target(10, 30))


def test_a_symbol_with_no_candles_has_one_head_target_up_to_the_forming_slot() -> None:
    got = plan_targets(
        horizon_start_ms=slot(10), forming_open_ms=slot(100), oldest_stored_ms=None, interior_runs=[], timeframe_ms=TF
    )
    assert got == (target(10, 100),)


def test_an_empty_horizon_has_no_targets() -> None:
    got = plan_targets(
        horizon_start_ms=slot(100),
        forming_open_ms=slot(100),
        oldest_stored_ms=None,
        interior_runs=[(slot(1), slot(5))],
        timeframe_ms=TF,
    )
    assert got == ()


def test_plan_page_never_asks_beyond_the_target() -> None:
    t = target(0, 2500)
    e = slot(2499)
    assert plan_page(t, slot(0), TF, 1000) == PagePlan(slot(0), e, 1000)
    assert plan_page(t, slot(1000), TF, 1000) == PagePlan(slot(1000), e, 1000)
    assert plan_page(t, slot(2000), TF, 1000) == PagePlan(slot(2000), e, 500)
    assert last_open_ms(t, TF) == e
    assert slot_count(t, TF) == 2500


def test_a_full_page_continues_after_its_last_row_and_a_short_page_ends_the_target() -> None:
    plan = PagePlan(slot(0), slot(2499), 1000)
    full = judge_page(plan, [slot(i) for i in range(1000)], TF)
    assert (full.kind, full.rows, full.next_start_ms) == (PageKind.FULL, 1000, slot(1000))
    # a full page that ends exactly at the target end has nothing after it
    last = PagePlan(slot(2000), slot(2499), 500)
    assert judge_page(last, [slot(i) for i in range(2000, 2500)], TF).next_start_ms is None
    short = judge_page(plan, [slot(0), slot(1), slot(5)], TF)
    assert (short.kind, short.rows, short.next_start_ms) == (PageKind.SHORT, 3, None)


def test_an_empty_page_ends_the_target_without_progress() -> None:
    out = judge_page(PagePlan(slot(0), slot(9), 10), [], TF)
    assert (out.kind, out.rows, out.next_start_ms) == (PageKind.EMPTY, 0, None)


@pytest.mark.parametrize(
    "rows",
    [
        [slot(-1), slot(0)],
        [slot(0), slot(10)],
        [slot(1), slot(1)],
        [slot(2), slot(1)],
        [slot(0), slot(1) + 1],
        [slot(i) for i in range(6)],
        [slot(0), slot(1), slot(2) + 7, slot(3), slot(4)],
    ],
    ids=["before", "after", "duplicate", "descending", "misaligned", "too_many", "one_bad_of_five"],
)
def test_judge_page_rejects_the_whole_page(rows: list[int]) -> None:
    with pytest.raises(PageViolation):
        judge_page(PagePlan(slot(0), slot(9), 5), rows, TF)


def test_progress_is_strict() -> None:
    plan = PagePlan(slot(0), slot(9), 1)
    assert judge_page(plan, [slot(0)], TF).next_start_ms == slot(1) > plan.start_ms


def test_judge_target_served_all_some_none() -> None:
    t = target(0, 10)
    assert judge_target(t, TF, 10) is TargetResult.SERVED_ALL
    assert judge_target(t, TF, 4) is TargetResult.SERVED_SOME
    assert judge_target(t, TF, 0) is TargetResult.SERVED_NONE
    with pytest.raises(ValueError):
        judge_target(t, TF, 11)
    assert "storage" in (judge_target.__doc__ or "")


def test_max_pages_bounds_a_target() -> None:
    assert max_pages(target(0, 2500), TF, 1000) == 4
    assert max_pages(target(0, 1000), TF, 1000) == 2


def test_misaligned_bool_or_non_int_inputs_are_refused() -> None:
    with pytest.raises(ValueError):
        BackfillTarget(slot(3), slot(3))
    with pytest.raises(TypeError):
        BackfillTarget(True, slot(3))
    with pytest.raises(TypeError):
        BackfillTarget(slot(1), float(slot(3)))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        plan_page(target(0, 10), slot(0) + 1, TF, 1000)
    with pytest.raises(ValueError):
        plan_page(target(0, 10), slot(10), TF, 1000)
    with pytest.raises(TypeError):
        plan_page(target(0, 10), slot(0), TF, True)
    with pytest.raises(ValueError):
        plan_targets(
            horizon_start_ms=slot(0) + 1,
            forming_open_ms=slot(9),
            oldest_stored_ms=None,
            interior_runs=[],
            timeframe_ms=TF,
        )
    with pytest.raises(TypeError):
        judge_page(PagePlan(slot(0), slot(9), 5), [True], TF)


# --- recovery attribution -------------------------------------------------------------------------------------

T10 = [target(0, 10)]


def fs(*idx: int) -> frozenset[int]:
    return frozenset(slot(i) for i in idx)


def attribute(
    before: list[frozenset[int]] | None,
    after: list[frozenset[int]] | None,
    sent: frozenset[int],
    inserted: int | None,
    targets: list[BackfillTarget] = T10,
) -> RecoveryResult:
    return attribute_recovery(targets, before, after, sent, inserted, TF)


def test_a_fully_recovered_target_is_attributed() -> None:
    r = attribute([fs()], [fs(*range(10))], fs(*range(10)), 10)
    assert (r.new_in_targets, r.recovered, r.missing_in_targets, r.attribution) == (10, 10, 0, Attribution.OK)


def test_a_partly_recovered_target_reports_exact_numbers() -> None:
    r = attribute([fs()], [fs(*range(6))], fs(*range(6)), 6)
    assert (r.new_in_targets, r.recovered, r.missing_in_targets, r.attribution) == (6, 6, 4, Attribution.OK)


def test_slots_present_before_are_not_recovered() -> None:
    r = attribute([fs(2, 3)], [fs(2, 3, 4, 5)], fs(2, 3, 4, 5), 2)
    assert (r.new_in_targets, r.recovered, r.attribution) == (2, 2, Attribution.OK)


def test_a_foreign_write_inside_the_target_makes_recovery_unverified() -> None:
    r = attribute([fs()], [fs(1, 2, 7)], fs(1, 2), 2)
    assert (r.new_in_targets, r.recovered, r.missing_in_targets) == (3, None, 7)
    assert r.attribution is Attribution.FOREIGN_WRITES


def test_a_foreign_write_of_a_slot_this_slice_also_sent_is_caught_by_the_report() -> None:
    r = attribute([fs()], [fs(1, 2, 3, 4)], fs(1, 2, 3, 4), 3)
    assert (r.recovered, r.attribution) == (None, Attribution.REPORT_MISMATCH)


def test_a_lying_write_report_cannot_create_or_remove_recovery() -> None:
    r = attribute([fs()], [fs()], fs(1, 2, 3), 3)
    assert (r.new_in_targets, r.recovered, r.attribution) == (0, None, Attribution.REPORT_MISMATCH)
    r = attribute([fs()], [fs(1, 2, 3)], fs(1, 2, 3), 0)
    assert (r.recovered, r.attribution) == (None, Attribution.REPORT_MISMATCH)


def test_a_removed_candle_is_an_anomaly() -> None:
    r = attribute([fs(1, 2)], [fs(1)], fs(), 0)
    assert (r.recovered, r.attribution) == (None, Attribution.ANOMALY)


def test_an_unknown_write_outcome_wins_over_unattributable_new_slots() -> None:
    r = attribute([fs()], [fs(1, 2)], fs(), None)  # committed, then unknown: the slots were never reported as sent
    assert (r.attribution, r.recovered, r.new_in_targets) == (Attribution.WRITE_UNKNOWN, None, 2)


def test_an_unknown_write_outcome_is_not_attributable() -> None:
    r = attribute([fs()], [fs(1)], fs(1), None)
    assert (r.recovered, r.attribution, r.new_in_targets) == (None, Attribution.WRITE_UNKNOWN, 1)


@pytest.mark.parametrize("before,after", [(None, [fs()]), ([fs()], None), (None, None)])
def test_a_failed_read_withholds_everything(
    before: list[frozenset[int]] | None, after: list[frozenset[int]] | None
) -> None:
    r = attribute(before, after, fs(1), 1)
    assert (r.new_in_targets, r.recovered, r.missing_in_targets) == (None, None, None)
    assert r.attribution is Attribution.READBACK_FAILED


def test_only_the_planned_ranges_count() -> None:
    with pytest.raises(ValueError):
        attribute([fs()], [frozenset({slot(10)})], fs(), 0)  # end_ms is exclusive
    with pytest.raises(ValueError):
        attribute([fs()], [frozenset({slot(-1)})], fs(), 0)
    with pytest.raises(ValueError):
        attribute([fs(), fs()], [fs()], fs(), 0)  # shape mismatch


def test_new_slots_in_one_target_do_not_hide_foreign_slots_in_another() -> None:
    ts = [target(0, 10), target(20, 30)]
    r = attribute([fs(), fs()], [fs(1), fs(25)], fs(1), 1, targets=ts)
    assert (r.attribution, r.recovered, r.new_in_targets, r.missing_in_targets) == (
        Attribution.FOREIGN_WRITES,
        None,
        2,
        18,
    )
