"""Judging one symbol's open interest and the overview object (OI-6b, design 8.4, 8.6, 8.7)."""

import dataclasses
import json
from typing import Any

import pytest

from tests.tools.symbol_overview.helpers import RULE, SYMBOL, T0, instrument
from tests.tools.symbol_overview.oi_helpers import (
    HORIZON_MS,
    OI_HEALTH,
    OI_WINDOW,
    WINDOW,
    api_doc,
    doc_from_states,
    slot,
)
from tools.symbol_overview.judge import (
    STATE_ORDER,
    OiPart,
    build_overview,
    judge_open_interest,
    open_interest_summary,
)
from tools.symbol_overview.model import (
    BUCKETS,
    TIMEFRAME,
    VENUE,
    Health,
    Invalid,
    OiCoverage,
    OiReport,
    Reason,
    SymbolReport,
)
from tools.symbol_overview.validate import open_interest_coverage_result
from tools.symbol_overview.window import build_open_interest_strip, open_interest_window

HEALTH = Health(T0, RULE)
O, N = slot(100), slot(8000)
NLR = "no_longer_recoverable"


def coverage_of(doc: dict[str, Any]) -> OiCoverage:
    result = open_interest_coverage_result(
        200,
        "application/json",
        json.dumps(doc).encode(),
        instrument=instrument(),
        oi_window=OI_WINDOW,
        health=HEALTH,
        oi_health=OI_HEALTH,
    )
    assert isinstance(result, OiCoverage), result
    return result


def judged(doc: dict[str, Any]) -> OiReport:
    return judge_open_interest(instrument(), coverage_of(doc), OI_WINDOW)


def old(count: int) -> dict[int, str]:
    """The first ``count`` slots as ``no_longer_recoverable`` (the oldest slots, as the horizon makes them)."""
    return {slot(i): NLR for i in range(count)}


def codes(report: OiReport) -> list[str]:
    return [reason.code for reason in report.reasons]


# --- reasons and the headline ----------------------------------------------------------------------------------------


def test_stale_conflict_and_holes_together() -> None:
    report = judged(
        doc_from_states(oldest=O, newest=N, states={slot(500): "missing", slot(600): "conflicted", slot(8001): "stale"})
    )

    assert report.state == "stale"  # the headline is the worst reason, in the candle order
    assert codes(report) == ["stale", "conflict", "missing"]
    assert report.reasons == (Reason("stale", 1, 1), Reason("conflict", 1, 1), Reason("missing", 1, 1))


def test_due_alone_is_behind() -> None:
    report = judged(doc_from_states(oldest=O, newest=N, states={slot(8001): "due"}))

    assert (report.state, report.reasons) == ("behind", (Reason("due", 1, 1),))


def test_not_yet_expected_alone_is_ok() -> None:
    report = judged(doc_from_states(oldest=O, newest=N))

    assert (report.state, report.reasons) == ("ok", ())
    assert (report.oldest_time_ms, report.newest_time_ms, report.as_of_ms) == (O, N, T0 + 1000)
    assert report.retrieval_horizon_ms == HORIZON_MS


def test_missing_before_the_oldest_row_is_not_a_hole() -> None:
    plain = judged(doc_from_states(oldest=O, newest=N))
    layered = judged(doc_from_states(oldest=O, newest=N, states=old(50)))  # no_longer_recoverable, then missing, to O

    for report in (plain, layered):
        assert (report.state, report.reasons) == ("ok", ())
        assert report.strip is not None and report.strip[:8] == "_" * 8 and report.strip[8] == "."


def test_a_conflict_before_the_oldest_row_is_a_finding() -> None:
    report = judged(doc_from_states(oldest=O, newest=N, states={slot(50): "conflicted"}))

    assert (report.state, report.reasons) == ("conflict", (Reason("conflict", 1, 1),))
    assert report.strip is not None and report.strip[50 // 12] == "c"


@pytest.mark.parametrize("position", [50, 500, 8010], ids=["before O", "between O and N", "after N"])
def test_a_conflict_without_a_row_is_reported_as_conflict_never_pre_history(position: int) -> None:
    report = judged(doc_from_states(oldest=O, newest=N, states={slot(position): "conflicted"}))

    assert (report.state, codes(report)) == ("conflict", ["conflict"])


def test_missing_and_unrecoverable_together_give_holes_and_both_reasons() -> None:
    states = {**old(100), slot(300): NLR, slot(500): "missing"}
    report = judged(doc_from_states(oldest=O, newest=N, states=states))

    assert report.state == "holes"  # `unrecoverable` has no headline state (option C)
    assert report.reasons == (Reason("missing", 1, 1), Reason("unrecoverable", 1, 1))
    assert report.strip is not None
    assert report.strip[300 // 12] == "r" and report.strip[500 // 12] == "m"


def test_unrecoverable_alone_is_ok_with_the_note() -> None:
    report = judged(doc_from_states(oldest=O, newest=N, states={**old(100), slot(300): NLR}))

    assert (report.state, report.reasons) == ("ok", (Reason("unrecoverable", 1, 1),))
    assert report.strip is not None and "r" in report.strip
    assert report.retrieval_horizon_ms == HORIZON_MS


def test_no_longer_recoverable_after_the_newest_row_is_not_a_reason() -> None:
    states = {**old(100), **{slot(i): NLR for i in (8001, 8002, 8003)}}  # stopped long ago; bucket 666 ends at 8003
    report = judged(doc_from_states(oldest=O, newest=N, states=states))

    assert (report.state, report.reasons) == ("ok", ())
    assert report.strip is not None and report.strip[8001 // 12] == "r"  # visible on the strip, not a reason


# --- the position of the oldest row against the window ----------------------------------------------------------------


@pytest.mark.parametrize("oldest", [-5000, -1, 0, 1, 4000, 8639], ids=lambda o: f"O={o}")
def test_the_position_of_the_oldest_row_against_the_window(oldest: int) -> None:
    report = judged(doc_from_states(oldest=slot(oldest), newest=slot(8639)))

    assert (report.state, report.reasons) == ("ok", ())  # a history that starts late is not a problem
    blank = max(0, min(BUCKETS, oldest // 12))  # buckets that end at or before the first stored row
    assert report.strip == "_" * blank + "." * (BUCKETS - blank)


def test_an_oldest_or_newest_row_at_or_after_the_end_of_the_window_is_unknown() -> None:
    for oldest, newest in ((slot(8640), slot(8640)), (slot(8639), slot(8640)), (slot(8700), slot(8800))):
        result = open_interest_coverage_result(
            200,
            "application/json",
            json.dumps(doc_from_states(oldest=oldest, newest=newest)).encode(),
            instrument=instrument(),
            oi_window=OI_WINDOW,
            health=HEALTH,
            oi_health=OI_HEALTH,
        )
        assert result == Invalid("clock_inconsistent")
        report = judge_open_interest(instrument(), result, OI_WINDOW)
        assert (report.state, report.strip) == ("unknown", None)  # never an ok strip of "_"


# --- outcomes that are not an answer ---------------------------------------------------------------------------------


def test_no_data_and_invalid_outcomes() -> None:
    nothing = judge_open_interest(instrument(), Invalid("api_error", "unknown_series"), OI_WINDOW)
    assert (nothing.state, nothing.reasons, nothing.strip) == ("no_data", (Reason("no_data", 0, 0),), None)
    assert (nothing.oldest_time_ms, nothing.newest_time_ms, nothing.as_of_ms, nothing.retrieval_horizon_ms) == (
        None,
    ) * 4

    broken = judge_open_interest(instrument(), Invalid("runs"), OI_WINDOW)
    assert (broken.state, broken.reasons, broken.strip) == ("unknown", (Reason("invalid", 0, 0, "runs"),), None)
    late = judge_open_interest(instrument(), Invalid("deadline"), OI_WINDOW)
    assert late.reasons == (Reason("invalid", 0, 0, "deadline"),)
    other = judge_open_interest(instrument(), Invalid("api_error", "open_interest_disabled"), OI_WINDOW)
    assert (other.state, other.reasons) == ("unknown", (Reason("invalid", 0, 0, "api_error:open_interest_disabled"),))
    transport = judge_open_interest(instrument(), Invalid("transport", "timeout"), OI_WINDOW)
    assert transport.reasons == (Reason("invalid", 0, 0, "transport:timeout"),)


def test_the_strip_matches_the_runs_and_the_oldest_row() -> None:
    doc = doc_from_states(oldest=O, newest=N, states={slot(500): "missing", slot(8001): "stale"})
    coverage = coverage_of(doc)

    report = judge_open_interest(instrument(), coverage, OI_WINDOW)

    assert report.strip == build_open_interest_strip(coverage.runs, coverage.oldest_time_ms, OI_WINDOW)
    assert report.strip is not None and len(report.strip) == BUCKETS


def test_an_answer_of_the_api_is_judged_without_error() -> None:
    doc = api_doc(rows=[slot(i) for i in range(0, 8000, 3)], conflicts=[slot(70)])
    report = judged(doc)

    assert report.state in STATE_ORDER and report.strip is not None


# --- the summary -----------------------------------------------------------------------------------------------------


def report_with(state: str) -> OiReport:
    return OiReport(state, (), None, None, None, None, None)


def test_summary_counts_each_symbol_once_by_headline() -> None:
    reports = [report_with(s) for s in ("ok", "ok", "holes", "stale", "no_data", "unknown", "behind", "conflict")]

    assert open_interest_summary(reports) == {
        "total": 8,
        "no_data": 1,
        "unknown": 1,
        "stale": 1,
        "conflict": 1,
        "holes": 1,
        "behind": 1,
        "ok": 2,
    }


def test_summary_has_every_state_key_with_zero() -> None:
    assert open_interest_summary([]) == {"total": 0, **dict.fromkeys(STATE_ORDER, 0)}
    assert "unrecoverable" not in open_interest_summary([])  # option C: no tile


def test_a_judged_report_has_no_field_for_runs() -> None:
    assert "runs" not in {field.name for field in dataclasses.fields(OiReport)}


# --- the overview object ---------------------------------------------------------------------------------------------


def candle_report(state: str = "ok") -> SymbolReport:
    return SymbolReport(VENUE, SYMBOL, TIMEFRAME, state, (), O, N, T0, "." * BUCKETS)


def overview(part: OiPart | None, reports: list[SymbolReport] | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {} if part is None else {"open_interest": part}
    return build_overview(
        HEALTH,
        WINDOW,
        reports if reports is not None else [candle_report()],
        duration_ms=7,
        deadline_hit=True,
        **kwargs,
    )


def test_build_overview_without_open_interest_is_todays_object() -> None:
    body = overview(None)

    assert set(body) == {"generated_at_ms", "rule", "window", "refresh", "summary", "symbols"}
    assert body["refresh"] == {"duration_ms": 7, "deadline_hit": True}
    assert set(body["symbols"][0]) == {
        "venue",
        "symbol",
        "timeframe",
        "state",
        "reasons",
        "oldest_open_time_ms",
        "newest_open_time_ms",
        "as_of_ms",
        "strip",
    }


def enabled_part(*reports: OiReport, **kwargs: Any) -> OiPart:
    values: dict[str, Any] = {
        "status": "enabled",
        "detail": "",
        "deadline_hit": False,
        "candle_not_asked": 0,
        "oi_not_asked": 0,
    }
    values.update(kwargs)
    return OiPart(reports=tuple(reports), **values)


def test_build_overview_with_open_interest_adds_only_new_keys() -> None:
    baseline = overview(None)
    stored = judged(doc_from_states(oldest=O, newest=N))
    body = overview(enabled_part(stored, deadline_hit=True, candle_not_asked=2, oi_not_asked=3))

    assert set(body) - set(baseline) == {"open_interest"}
    for key in ("generated_at_ms", "rule", "window", "summary"):
        assert body[key] == baseline[key]
    assert set(body["refresh"]) - set(baseline["refresh"]) == {"candle_not_asked", "oi_deadline_hit", "oi_not_asked"}
    assert body["refresh"]["deadline_hit"] is True  # candle-only
    assert (
        body["refresh"]["candle_not_asked"],
        body["refresh"]["oi_deadline_hit"],
        body["refresh"]["oi_not_asked"],
    ) == (2, True, 3)
    for old_symbol, new_symbol in zip(baseline["symbols"], body["symbols"], strict=True):
        assert set(new_symbol) - set(old_symbol) == {"open_interest"}
        assert {key: new_symbol[key] for key in old_symbol} == old_symbol
    shifted = open_interest_window(WINDOW)
    assert body["open_interest"] == {
        "status": "enabled",
        "detail": "",
        "summary": open_interest_summary([stored]),
        "window": {"start": shifted.start, "end": shifted.end},
    }
    assert body["symbols"][0]["open_interest"] == {
        "state": "ok",
        "reasons": [],
        "oldest_time_ms": O,
        "newest_time_ms": N,
        "as_of_ms": T0 + 1000,
        "retrieval_horizon_ms": HORIZON_MS,
        "strip": stored.strip,
    }


def test_a_disabled_deployment_has_no_open_interest_blocks() -> None:
    part = OiPart("disabled", "", (), False, 0, 0)

    body = overview(part)

    assert body["open_interest"]["status"] == "disabled" and body["open_interest"]["summary"] is None
    assert body["symbols"][0]["open_interest"] is None
    assert (body["refresh"]["oi_deadline_hit"], body["refresh"]["oi_not_asked"]) == (False, 0)


def test_an_unknown_open_interest_part_marks_every_symbol_unknown() -> None:
    invalid = Invalid("schema", "open_interest")
    reports = tuple(judge_open_interest(instrument(f"S{i}/USDT:USDT"), invalid, OI_WINDOW) for i in range(2))
    candles = [candle_report(), candle_report("holes")]

    body = overview(OiPart("unknown", "schema:open_interest", reports, False, 0, 0), candles)

    assert body["open_interest"]["status"] == "unknown"
    assert body["open_interest"]["detail"] == "schema:open_interest"
    assert body["open_interest"]["summary"]["unknown"] == 2
    assert [s["state"] for s in body["symbols"]] == ["ok", "holes"]  # the candle headline is never touched
    assert all(
        s["open_interest"]["state"] == "unknown" and s["open_interest"]["strip"] is None for s in body["symbols"]
    )


def test_reports_must_line_up_with_the_candle_reports() -> None:
    with pytest.raises(ValueError):
        overview(enabled_part(report_with("ok"), report_with("ok")))  # two reports for one symbol
    with pytest.raises(ValueError):
        overview(OiPart("disabled", "", (report_with("ok"),), False, 0, 0))  # disabled has none
