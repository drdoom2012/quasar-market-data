import json
from collections.abc import Mapping

import pytest

from tests.tools.symbol_overview.helpers import RULE, T0, coverage_doc, instrument
from tools.symbol_overview.judge import STATE_ORDER, build_overview, judge_symbol, page_error
from tools.symbol_overview.model import TF_MS, Coverage, Health, Invalid, Reason, SymbolReport
from tools.symbol_overview.validate import coverage_result
from tools.symbol_overview.window import window_for

W = window_for(T0)
S = W.start
E = W.end
NEWEST = T0 - 2 * TF_MS
HEALTH = Health(T0, RULE)


def cover(
    *, oldest: int = S, newest: int = NEWEST, states: Mapping[int, str] | None = None, ahead: bool = False
) -> Coverage:
    doc = coverage_doc(W, oldest=oldest, newest=newest, slot_states=states, stored_ahead=ahead)
    result = coverage_result(
        200,
        "application/json",
        json.dumps(doc).encode(),
        instrument=instrument(newest=newest),
        window=W,
        health=HEALTH,
    )
    assert isinstance(result, Coverage), result
    return result


def judge(coverage: Coverage) -> SymbolReport:
    return judge_symbol(instrument(newest=coverage.newest_open_time_ms), coverage, W)


def codes(report: SymbolReport) -> list[str]:
    return [reason.code for reason in report.reasons]


# reasons (spec 5.4)


def test_no_data() -> None:
    report = judge_symbol(instrument(newest=None), None, W)
    assert report.state == "no_data"
    assert report.reasons == (Reason("no_data", 0, 0),)
    assert (report.strip, report.oldest_open_time_ms, report.newest_open_time_ms, report.as_of_ms) == (None,) * 4


def test_a_stored_instrument_needs_an_outcome_and_a_null_one_must_not_have_one() -> None:
    with pytest.raises(ValueError):
        judge_symbol(instrument(), None, W)
    with pytest.raises(ValueError):
        judge_symbol(instrument(newest=None), Invalid("counts"), W)


def test_invalid_outcome_is_unknown_never_ok() -> None:
    report = judge_symbol(instrument(), Invalid("counts"), W)
    assert report.state == "unknown"
    assert report.reasons == (Reason("invalid", 0, 0, "counts"),)
    assert report.strip is None


def test_invalid_detail_carries_the_api_error_code() -> None:
    report = judge_symbol(instrument(), Invalid("api_error", "not_ready"), W)
    assert report.reasons == (Reason("invalid", 0, 0, "api_error:not_ready"),)


def test_stored_ahead_is_unknown_but_keeps_the_strip() -> None:
    report = judge(cover(ahead=True))
    assert report.state == "unknown"
    assert codes(report) == ["stored_ahead"]
    assert report.strip is not None


def stale_states(newest: int) -> dict[int, str]:
    return {t: "stale" for t in range(newest + TF_MS, T0, TF_MS)}


def test_stale_slots_make_stale() -> None:
    newest = T0 - 30 * TF_MS
    report = judge(cover(newest=newest, states=stale_states(newest)))
    assert report.state == "stale"
    assert report.reasons == (Reason("stale", 29, 1),)


def test_conflicted_slots_make_conflict() -> None:
    report = judge(cover(states={S + 10 * TF_MS: "conflicted"}))
    assert (report.state, report.reasons) == ("conflict", (Reason("conflict", 1, 1),))


def test_a_real_hole_makes_holes() -> None:
    states = {S + t * TF_MS: "missing" for t in (10, 11, 12)}
    report = judge(cover(states=states))
    assert (report.state, report.reasons) == ("holes", (Reason("missing", 3, 1),))


def test_unavailable_alone_is_holes() -> None:
    report = judge(cover(states={S + 10 * TF_MS: "unavailable"}))
    assert (report.state, report.reasons) == ("holes", (Reason("unavailable", 1, 1),))


def test_due_alone_is_behind() -> None:
    report = judge(cover(states={T0 - TF_MS: "due"}))
    assert (report.state, report.reasons) == ("behind", (Reason("due", 1, 1),))


def test_not_yet_expected_alone_is_ok() -> None:
    report = judge(cover())
    assert (report.state, report.reasons) == ("ok", ())


def test_clean_series_is_ok_and_carries_its_facts() -> None:
    report = judge(cover())
    assert report.strip == "." * 719 + "n"  # the last hour holds the two slots that are not yet expected
    assert (report.oldest_open_time_ms, report.newest_open_time_ms, report.as_of_ms) == (S, NEWEST, T0 + 1000)


# history start


def test_leading_missing_run_is_not_a_hole() -> None:
    oldest = S + 100 * TF_MS
    report = judge(cover(oldest=oldest))
    assert (report.state, report.reasons) == ("ok", ())
    assert report.oldest_open_time_ms == oldest
    assert report.strip is not None
    assert report.strip[:8] == "_" * 8


def test_hole_right_after_oldest_is_a_hole() -> None:
    oldest = S + 100 * TF_MS
    report = judge(cover(oldest=oldest, states={oldest + TF_MS: "missing"}))
    assert (report.state, report.reasons) == ("holes", (Reason("missing", 1, 1),))


def test_pre_history_conflict_is_reported() -> None:
    report = judge(cover(oldest=S + 100 * TF_MS, states={S + 10 * TF_MS: "conflicted"}))
    assert report.state == "conflict"


def test_oldest_equals_window_start_has_no_pre_history() -> None:
    assert judge(cover(oldest=S)).strip == "." * 719 + "n"


def test_oldest_one_slot_after_the_window_start_is_ok() -> None:
    report = judge(cover(oldest=S + TF_MS))
    assert (report.state, report.reasons) == ("ok", ())


# the clock


def test_series_older_than_window_is_stale() -> None:
    newest = S - 100 * TF_MS
    report = judge(cover(oldest=newest, newest=newest, states=stale_states(newest)))
    assert report.state == "stale"
    assert report.reasons == (Reason("stale", 8639, 1),)


def test_tool_uses_counts_not_its_own_thresholds() -> None:
    # The slot after the newest candle is expected at T0 + 15 s, after as_of (T0 + 1 s): by a threshold of the
    # tool's own it would be not_yet_expected. The API's answer says due, and that is what counts.
    report = judge(cover(states={T0 - TF_MS: "due"}))
    assert report.state == "behind"


# combination


def test_several_reasons_keep_all_and_the_headline_is_the_most_severe() -> None:
    newest = T0 - 30 * TF_MS
    states = stale_states(newest) | {S + 10 * TF_MS: "conflicted", S + 20 * TF_MS: "missing"}
    report = judge(cover(newest=newest, states=states))
    assert report.state == "stale"
    assert codes(report) == ["stale", "conflict", "missing"]


def test_due_and_unavailable_headline_is_holes() -> None:
    report = judge(cover(states={T0 - TF_MS: "due", S + 10 * TF_MS: "unavailable"}))
    assert report.state == "holes"
    assert codes(report) == ["unavailable", "due"]


def test_stored_ahead_together_with_a_hole_keeps_both_reasons() -> None:
    report = judge(cover(ahead=True, states={S + 10 * TF_MS: "missing"}))
    assert report.state == "unknown"
    assert codes(report) == ["stored_ahead", "missing"]


# overview


def reports() -> list[SymbolReport]:
    return [
        judge_symbol(instrument("A/USDT:USDT", newest=None), None, W),
        judge(cover()),
        judge_symbol(instrument("B/USDT:USDT"), Invalid("schema"), W),
        judge(cover(states={S + 10 * TF_MS: "missing"})),
        judge(cover()),
    ]


def test_summary_counts_each_symbol_once_by_headline() -> None:
    overview = build_overview(HEALTH, W, reports(), duration_ms=5, deadline_hit=False)
    assert overview["summary"] == {
        "total": 5,
        "no_data": 1,
        "unknown": 1,
        "stale": 0,
        "conflict": 0,
        "holes": 1,
        "behind": 0,
        "ok": 2,
    }


def test_summary_has_every_state_key_with_zero() -> None:
    overview = build_overview(HEALTH, W, [], duration_ms=0, deadline_hit=False)
    assert overview["summary"] == {"total": 0, **dict.fromkeys(STATE_ORDER, 0)}


def test_overview_carries_the_api_clock_the_rule_and_the_window() -> None:
    overview = build_overview(HEALTH, W, reports(), duration_ms=7, deadline_hit=True)
    assert overview["generated_at_ms"] == T0
    assert overview["rule"] == {"publication_delay_ms": 15_000, "lag_threshold_ms": 60_000}
    assert overview["window"] == {"start": S, "end": E, "bucket_slots": 12}
    assert overview["refresh"] == {"duration_ms": 7, "deadline_hit": True}


def test_overview_symbols_keep_the_input_order() -> None:
    overview = build_overview(HEALTH, W, reports(), duration_ms=0, deadline_hit=False)
    symbols = overview["symbols"]
    assert isinstance(symbols, list)
    assert [s["state"] for s in symbols] == ["no_data", "ok", "unknown", "holes", "ok"]
    assert symbols[3]["reasons"] == [{"code": "missing", "slots": 1, "runs": 1, "detail": ""}]


def test_page_error_shape() -> None:
    assert page_error(Invalid("api_error", "not_ready")) == {"api_error": {"code": "api_error", "detail": "not_ready"}}


def test_overview_size_for_600_symbols() -> None:
    one = judge(cover(states={S + 10 * TF_MS: "missing", T0 - TF_MS: "due"}))
    overview = build_overview(HEALTH, W, [one] * 600, duration_ms=0, deadline_hit=False)
    assert len(json.dumps(overview)) < 1_000_000
