"""Validation of the funding answers (FR-6 design 4.2): every failure is an ``Invalid``, nothing yields an ``ok``."""

import json
from typing import Any

import pytest

from tests.tools.symbol_overview.funding_helpers import (
    BEHIND_AFTER_MS,
    END,
    FRESH_THROUGH,
    FUNDING_RULE,
    FWINDOW,
    HEALTHY_FROM,
    NEWEST,
    START,
    body_of,
    funding_doc,
    health_body,
    health_object,
)
from tests.tools.symbol_overview.helpers import RULE, T0, instrument
from tools.symbol_overview.model import (
    CALL_TIMEOUT_MS,
    MAX_BODY_BYTES,
    REFRESH_DEADLINE_MS,
    FundingCoverage,
    Health,
    Invalid,
)
from tools.symbol_overview.validate import funding_health, funding_result

HEALTH = Health(T0, RULE)
JSON = "application/json"


def check(doc: Any, *, status: int = 200, content_type: str | None = JSON, raw: bytes | None = None) -> Any:
    body = raw if raw is not None else body_of(doc)
    return funding_result(status, content_type, body, instrument=instrument(), funding_window=FWINDOW, health=HEALTH)


def code(result: Any) -> tuple[str, str]:
    assert isinstance(result, Invalid), result
    return result.code, result.detail


# --- the health object ------------------------------------------------------------------------------------------------


def health(funding_rate: Any) -> Any:
    return funding_health(200, JSON, health_body(funding_rate), health=HEALTH)


def test_a_well_formed_health_object_is_read() -> None:
    result = health(health_object(behind=2, quiet_series=1, status="degraded"))

    assert result is not None and not isinstance(result, Invalid)
    assert (
        result.rule == FUNDING_RULE and result.status == "degraded" and result.behind == 2 and result.quiet_series == 1
    )


def test_null_means_disabled() -> None:
    assert health(None) is None


def test_an_absent_key_is_unknown_not_disabled() -> None:
    assert code(funding_health(200, JSON, health_body(), health=HEALTH)) == ("schema", "funding_rate")


@pytest.mark.parametrize(
    "value",
    [
        3,
        "on",
        [],
        health_object(as_of_ms=T0 + 1),
        health_object(as_of_ms=T0 - 1),
        {**health_object(), "rule": {"behind_after_ms": 1}},
        {**health_object(), "rule": "x"},
        {**health_object(), "scope": "all"},
        {**health_object(), "status": "fine"},
        health_object(series_count=-1),
        health_object(quiet_series="1"),
        health_object(conflicted_series=True),
        {k: v for k, v in health_object().items() if k != "catching_up"},
        {**health_object(), "rule": {**health_object()["rule"], "quiet_after_ms": -1}},
    ],
    ids=lambda v: str(v)[:40],
)
def test_a_malformed_health_object_is_unknown(value: Any) -> None:
    assert code(health(value)) == ("schema", "funding_rate")


def test_a_health_answer_that_is_not_usable_keeps_its_code() -> None:
    assert code(funding_health(500, JSON, b"{}", health=HEALTH)) == ("bad_status", "500")
    assert code(funding_health(200, "text/html", b"{}", health=HEALTH)) == ("bad_content_type", "")


# --- the answer ---------------------------------------------------------------------------------------------------------


def test_a_healthy_answer_is_accepted_and_read() -> None:
    result = check(funding_doc())

    assert isinstance(result, FundingCoverage)
    assert (result.window_state, result.complete, result.conflicted_events) == ("verified", True, 0)
    assert (result.verified_from_ms, result.verified_through_ms) == (HEALTHY_FROM, FRESH_THROUGH)
    assert result.newest_funding_time_ms == NEWEST and result.events_in_window == 3


@pytest.mark.parametrize(
    ("span", "state", "complete"),
    [
        ((HEALTHY_FROM, FRESH_THROUGH), "verified", True),
        ((START + 1, FRESH_THROUGH), "partly_verified", False),
        ((HEALTHY_FROM, END - 1), "partly_verified", False),
        ((END + 1, END + 5), "unverified", False),
        ((START - 9, START - 1), "unverified", False),
        (None, "unverified", False),
    ],
)
def test_every_window_state_the_api_can_produce_is_accepted(
    span: tuple[int, int] | None, state: str, complete: bool
) -> None:
    result = check(funding_doc(span=span, events=2))  # a series with rows but no span is possible too

    assert isinstance(result, FundingCoverage), result
    assert (result.window_state, result.complete) == (state, complete)


def test_a_span_without_a_row_is_a_valid_answer() -> None:
    result = check(funding_doc(oldest=None, newest=None, events=0))

    assert isinstance(result, FundingCoverage) and result.newest_funding_time_ms is None and result.complete


def test_a_conflict_without_a_stored_row_is_accepted() -> None:
    assert isinstance(check(funding_doc(oldest=None, newest=None, events=0, conflicted=1)), FundingCoverage)


@pytest.mark.parametrize(
    ("status", "content_type", "raw", "expected"),
    [
        (404, JSON, json.dumps({"error": {"code": "unknown_series"}}).encode(), ("api_error", "unknown_series")),
        (
            404,
            JSON,
            json.dumps({"error": {"code": "funding_rate_disabled"}}).encode(),
            ("api_error", "funding_rate_disabled"),
        ),
        (503, JSON, json.dumps({"error": {"code": "not_ready"}}).encode(), ("api_error", "not_ready")),
        (500, JSON, b"oops", ("bad_status", "500")),
        (200, "text/html", b"{}", ("bad_content_type", "")),
        (200, None, b"{}", ("bad_content_type", "")),
        (200, JSON, b"[1]", ("bad_json", "")),
        (200, JSON, b"{", ("bad_json", "")),
        (200, JSON, b"x" * (MAX_BODY_BYTES + 1), ("too_large", "")),
    ],
)
def test_an_answer_that_is_not_a_usable_200_keeps_its_reason(
    status: int, content_type: str | None, raw: bytes, expected: tuple[str, str]
) -> None:
    assert code(check(None, status=status, content_type=content_type, raw=raw)) == expected


def mutate(path: str, value: Any) -> dict[str, Any]:
    doc = funding_doc()
    node = doc
    *parents, last = path.split(".")
    for key in parents:
        node = node[key]
    if value is KeyError:
        del node[last]
    else:
        node[last] = value
    return doc


@pytest.mark.parametrize(
    "path",
    [
        "series.oldest_funding_time_ms",
        "series.newest_funding_time_ms",
        "coverage.as_of_ms",
        "coverage.verified_from_ms",
        "coverage.verified_through_ms",
        "coverage.verified_at_ms",
        "coverage.window_state",
        "coverage.events_in_window",
        "coverage.conflicted_events",
        "coverage.complete",
        "series",
        "coverage",
        "rows",
        "venue",
        "symbol",
        "time_semantics",
        "start",
        "end",
    ],
)
def test_a_missing_field_is_a_schema_failure(path: str) -> None:
    assert code(check(mutate(path, KeyError))) == ("schema", "")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("series.oldest_funding_time_ms", "1"),
        ("series.newest_funding_time_ms", None),  # one of the two null
        ("series.oldest_funding_time_ms", 5),  # outside the valid range
        ("series.oldest_funding_time_ms", NEWEST + 1),  # oldest after newest
        ("coverage.verified_from_ms", None),  # one end of the span null
        ("coverage.verified_through_ms", HEALTHY_FROM - 1),  # from after through
        ("coverage.verified_at_ms", 1.5),
        ("coverage.as_of_ms", True),
        ("coverage.window_state", "complete"),
        ("coverage.events_in_window", -1),
        ("coverage.events_in_window", "3"),
        ("coverage.conflicted_events", None),
        ("coverage.complete", 1),
        ("rows", {}),
        ("rows", [{}, {}, {}]),  # more rows than the limit asked for
        ("start", "1"),
        ("end", 1.0),
        ("time_semantics", 3),
        ("venue", 3),
    ],
)
def test_a_value_of_the_wrong_type_or_range_is_a_schema_failure(path: str, value: Any) -> None:
    assert code(check(mutate(path, value)))[0] == "schema"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("venue", "other"),
        ("symbol", "ETH/USDT:USDT"),
        ("start", START + 1),
        ("end", END + 1),
        ("time_semantics", "period_end"),
    ],
)
def test_an_answer_for_something_else_is_an_echo_failure(path: str, value: Any) -> None:
    assert code(check(mutate(path, value))) == ("echo", "")


@pytest.mark.parametrize(
    "overrides",
    [
        {"window_state": "partly_verified"},  # verified span, wrong state
        {"window_state": "unverified"},
        {"complete": False},  # verified, no conflict, but not complete
        {"conflicted_events": 1},  # a conflict, still complete
        {"events_in_window": 0, "newest": None},
    ],
    ids=["state partly", "state unverified", "complete false", "complete with conflict", "series null with events"],
)
def test_an_answer_that_contradicts_itself_is_a_counts_failure(overrides: dict[str, Any]) -> None:
    doc = funding_doc()
    if "newest" in overrides:
        doc["series"] = {"oldest_funding_time_ms": None, "newest_funding_time_ms": None}
        doc["coverage"]["events_in_window"] = 4
    else:
        doc["coverage"].update(overrides)

    assert code(check(doc)) == ("counts", "")


def test_a_verified_window_from_a_span_that_starts_after_the_window_is_refused() -> None:
    doc = funding_doc(span=(START + 1, FRESH_THROUGH))
    doc["coverage"]["window_state"] = "verified"
    doc["coverage"]["complete"] = True

    assert code(check(doc)) == ("counts", "")


def test_events_in_the_window_that_the_whole_series_bounds_exclude_are_refused() -> None:
    assert code(check(funding_doc(oldest=START - 10, newest=START - 1, events=1))) == ("counts", "")  # all before
    assert code(check(funding_doc(oldest=END + 1, newest=END + 5, events=1))) == ("counts", "")  # all after
    assert isinstance(check(funding_doc(oldest=START - 10, newest=START, events=1)), FundingCoverage)  # at the edge
    assert isinstance(check(funding_doc(oldest=END, newest=END + 5, events=1, span=(START, END + 5))), FundingCoverage)
    assert isinstance(check(funding_doc(oldest=START - 10, newest=START - 1, events=0)), FundingCoverage)  # none inside


def test_events_in_the_window_without_a_stored_row_is_refused() -> None:
    assert code(check(funding_doc(oldest=None, newest=None, events=2))) == ("counts", "")


@pytest.mark.parametrize(
    ("as_of", "ok"),
    [
        (T0, True),
        (T0 + REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS, True),
        (T0 + REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS + 1, False),
        (T0 - 1, False),
    ],
)
def test_the_clock_of_the_answer_is_bounded_by_the_refresh(as_of: int, ok: bool) -> None:
    result = check(funding_doc(as_of_ms=as_of))

    assert isinstance(result, FundingCoverage) if ok else code(result) == ("clock_inconsistent", "")


def test_a_span_or_a_settlement_after_the_answers_own_clock_is_refused() -> None:
    as_of = T0 + 1000
    assert isinstance(check(funding_doc(span=(HEALTHY_FROM, as_of), newest=as_of)), FundingCoverage)  # == is fine
    assert code(check(funding_doc(span=(HEALTHY_FROM, as_of + 1)))) == ("clock_inconsistent", "")
    assert code(check(funding_doc(newest=as_of + 1))) == ("clock_inconsistent", "")


def test_the_two_derived_ends_of_the_window_agree_with_the_rule() -> None:
    # the page asks about [S, T0 - behind_after_ms + 1]: a span ending there is not behind, one ms less is
    assert T0 - END < BEHIND_AFTER_MS <= T0 - (END - 1)
