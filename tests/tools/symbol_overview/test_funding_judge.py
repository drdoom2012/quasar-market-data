"""Judging one symbol's funding and the overview object it adds (FR-6 design 4.3, 6)."""

from typing import Any

import pytest

from tests.tools.symbol_overview.funding_helpers import (
    END,
    FRESH_THROUGH,
    FUNDING_RULE,
    FWINDOW,
    HEALTHY_FROM,
    NEWEST,
    QUIET_AFTER_MS,
    START,
    body_of,
    funding_doc,
    parsed_health,
)
from tests.tools.symbol_overview.helpers import RULE, SYMBOL, T0, instrument
from tools.symbol_overview.judge import (
    FundingPart,
    build_overview,
    funding_summary,
    judge_funding,
    judge_symbol,
)
from tools.symbol_overview.model import (
    FUNDING_STATE_ORDER,
    FundingCoverage,
    FundingReport,
    Health,
    Invalid,
    Reason,
    Window,
)
from tools.symbol_overview.validate import funding_result

HEALTH = Health(T0, RULE)
FH = parsed_health()


def answer(**kwargs: Any) -> FundingCoverage:
    result = funding_result(
        200,
        "application/json",
        body_of(funding_doc(**kwargs)),
        instrument=instrument(),
        funding_window=FWINDOW,
        health=HEALTH,
    )
    assert isinstance(result, FundingCoverage), result
    return result


def judged(**kwargs: Any) -> FundingReport:
    return judge_funding(answer(**kwargs), FWINDOW, FH, T0)


def codes(report: FundingReport) -> list[str]:
    return [r.code for r in report.reasons]


# --- every state alone --------------------------------------------------------------------------------------------------


def test_a_complete_series_with_a_recent_settlement_is_ok() -> None:
    report = judged()

    assert (report.state, report.reasons) == ("ok", ())
    assert report.complete is True and report.window_state == "verified"
    assert (report.verified_from_ms, report.verified_through_ms) == (HEALTHY_FROM, FRESH_THROUGH)
    assert report.newest_funding_time_ms == NEWEST and report.events_in_window == 3


def test_a_conflict_is_a_conflict_with_its_number_of_events() -> None:
    report = judged(conflicted=2)

    assert report.state == "conflict" and report.reasons == (Reason("conflict", 2, 0),)
    assert report.complete is False


def test_a_series_without_a_row_is_quiet_even_when_verified() -> None:
    report = judged(oldest=None, newest=None, events=0)

    assert report.state == "quiet" and codes(report) == ["quiet"]
    assert report.window_state == "verified" and report.complete is True  # the API's statement stays visible


def test_a_series_whose_newest_settlement_is_old_is_quiet_only_past_the_rule() -> None:
    at_rule = judged(newest=T0 - QUIET_AFTER_MS, span=(HEALTHY_FROM, FRESH_THROUGH))
    past = judged(newest=T0 - QUIET_AFTER_MS - 1, span=(HEALTHY_FROM, FRESH_THROUGH))

    assert at_rule.state == "ok"  # strict, as the API's rule is
    assert past.state == "quiet" and codes(past) == ["quiet"]


def test_a_span_that_ends_before_the_window_end_is_behind() -> None:
    report = judged(span=(HEALTHY_FROM, END - 1))

    assert report.state == "behind" and codes(report) == ["behind"]
    assert report.window_state == "partly_verified"


def test_a_span_that_ends_exactly_at_the_window_end_is_not_behind() -> None:
    assert judged(span=(HEALTHY_FROM, END)).state == "ok"


def test_a_span_that_starts_after_the_window_start_is_partly_verified() -> None:
    report = judged(span=(START + 1, FRESH_THROUGH))

    assert report.state == "partly_verified" and codes(report) == ["partly_verified"]
    assert judged(span=(START, FRESH_THROUGH)).state == "ok"


def test_a_span_that_misses_both_ends_gives_both_facts() -> None:
    report = judged(span=(START + 1, END - 1))

    assert codes(report) == ["behind", "partly_verified"] and report.state == "behind"


def test_a_series_without_a_span_is_unverified() -> None:
    report = judged(span=None)

    assert report.state == "unverified" and codes(report) == ["unverified"]
    assert report.verified_from_ms is None and report.complete is False


def test_a_span_that_ends_before_the_window_is_unverified_and_behind() -> None:
    report = judged(span=(HEALTHY_FROM, START - 1))

    assert codes(report) == ["behind", "unverified"] and report.state == "behind"


def test_a_span_that_starts_after_the_window_is_unverified_and_not_behind() -> None:
    report = judged(span=(END + 1, END + 10))

    assert codes(report) == ["unverified"] and report.state == "unverified"


def test_a_404_unknown_series_is_no_data() -> None:
    report = judge_funding(Invalid("api_error", "unknown_series"), FWINDOW, FH, T0)

    assert report.state == "no_data" and report.reasons == (Reason("no_data", 0, 0),)
    assert report.complete is None and report.verified_from_ms is None


@pytest.mark.parametrize(
    "invalid",
    [
        Invalid("transport", "timeout"),
        Invalid("deadline"),
        Invalid("schema"),
        Invalid("api_error", "funding_rate_disabled"),
    ],
)
def test_anything_else_that_failed_is_unknown_with_its_reason(invalid: Invalid) -> None:
    report = judge_funding(invalid, FWINDOW, FH, T0)

    detail = f"{invalid.code}:{invalid.detail}" if invalid.detail else invalid.code
    assert report.state == "unknown" and report.reasons == (Reason("invalid", 0, 0, detail),)


# --- together -------------------------------------------------------------------------------------------------------------


def test_several_reasons_are_all_listed_and_the_worst_state_wins() -> None:
    report = judged(conflicted=1, oldest=None, newest=None, events=0, span=(START + 1, END - 1))

    assert codes(report) == ["conflict", "quiet", "behind", "partly_verified"]
    assert report.state == "conflict"


def test_the_headline_order_is_the_documented_one() -> None:
    assert FUNDING_STATE_ORDER == (
        "no_data",
        "unknown",
        "conflict",
        "quiet",
        "behind",
        "unverified",
        "partly_verified",
        "ok",
    )


def test_ok_is_never_reached_without_complete() -> None:
    for kwargs in (
        {"conflicted": 1},
        {"span": None},
        {"span": (HEALTHY_FROM, END - 1)},
        {"span": (START + 1, FRESH_THROUGH)},
    ):
        assert judged(**kwargs).state != "ok", kwargs


def test_a_validated_answer_that_is_not_complete_and_has_no_reason_is_not_green() -> None:
    contradictory = FundingCoverage(T0, 1, NEWEST, HEALTHY_FROM, FRESH_THROUGH, T0, "verified", 1, 0, False)

    report = judge_funding(contradictory, FWINDOW, FH, T0)

    assert report.state == "unknown" and report.reasons == (Reason("invalid", 0, 0, "counts"),)


def test_the_summary_counts_each_symbol_once() -> None:
    reports = [judged(), judged(conflicted=1), judged(span=None), judged(oldest=None, newest=None, events=0)]

    summary = funding_summary(reports)

    assert summary == {"total": 4, **dict.fromkeys(FUNDING_STATE_ORDER, 0)} | {
        "ok": 1,
        "conflict": 1,
        "unverified": 1,
        "quiet": 1,
    }


# --- the overview object ----------------------------------------------------------------------------------------------


def candle_report(symbol: str = SYMBOL) -> Any:
    return judge_symbol(instrument(symbol, newest=None), None, Window(0, 300_000))


def overview(funding: FundingPart | None, count: int = 2) -> dict[str, Any]:
    reports = [candle_report(f"S{i}/USDT:USDT") for i in range(count)]
    body = build_overview(
        HEALTH, Window(START, START + 300_000), reports, duration_ms=5, deadline_hit=False, funding=funding
    )
    return body


def test_without_a_funding_part_the_object_is_the_old_one() -> None:
    body = overview(None)

    assert "funding_rate" not in body and "funding_deadline_hit" not in body["refresh"]
    assert all("funding_rate" not in s for s in body["symbols"])


def test_an_enabled_funding_part_adds_keys_and_changes_none() -> None:
    reports = (judged(), judged(conflicted=1))
    part = FundingPart("enabled", "", reports, False, 0, FWINDOW, FH, clamped=False)
    plain, body = overview(None), overview(part)

    assert body["refresh"] == {**plain["refresh"], "funding_deadline_hit": False, "funding_not_asked": 0}
    assert {k: v for k, v in body.items() if k not in ("funding_rate", "refresh", "symbols")} == {
        k: v for k, v in plain.items() if k not in ("refresh", "symbols")
    }
    assert [{k: v for k, v in s.items() if k != "funding_rate"} for s in body["symbols"]] == plain["symbols"]
    block = body["funding_rate"]
    assert block["status"] == "enabled" and block["window"] == {"start": START, "end": END, "clamped": False}
    assert block["summary"]["ok"] == 1 and block["summary"]["conflict"] == 1 and block["summary"]["total"] == 2
    assert block["rule"] == {
        "behind_after_ms": FUNDING_RULE.behind_after_ms,
        "quiet_after_ms": QUIET_AFTER_MS,
        "backfill_days": 30,
    }
    assert block["health"]["status"] == "current" and block["health"]["quiet_series"] == 0
    assert [s["funding_rate"]["state"] for s in body["symbols"]] == ["ok", "conflict"]
    assert body["symbols"][0]["funding_rate"]["verified_through_ms"] == FRESH_THROUGH


def test_a_disabled_part_has_no_reports_and_no_summary() -> None:
    body = overview(FundingPart("disabled", "", (), False, 0))

    assert body["funding_rate"]["status"] == "disabled" and body["funding_rate"]["summary"] is None
    assert all(s["funding_rate"] is None for s in body["symbols"])
    with pytest.raises(ValueError):
        overview(FundingPart("disabled", "", (judged(),), False, 0))


def test_an_unknown_part_carries_a_report_per_symbol_and_no_window() -> None:
    unknown = judge_funding(Invalid("schema", "funding_rate"), FWINDOW, FH, T0)
    body = overview(FundingPart("unknown", "schema:funding_rate", (unknown, unknown), False, 0))

    block = body["funding_rate"]
    assert block["status"] == "unknown" and block["detail"] == "schema:funding_rate"
    assert block["window"] is None and block["rule"] is None and block["health"] is None
    assert block["summary"]["unknown"] == 2


def test_the_reports_must_line_up_with_the_symbols() -> None:
    with pytest.raises(ValueError):
        overview(FundingPart("enabled", "", (judged(),), False, 0, FWINDOW, FH), count=2)
