import copy
import json
from collections.abc import Callable
from typing import Any

import pytest

from tests.tools.symbol_overview.helpers import RULE, SYMBOL, T0, coverage_doc, health_doc, instrument
from tools.symbol_overview.model import (
    CALL_TIMEOUT_MS,
    MAX_BODY_BYTES,
    REFRESH_DEADLINE_MS,
    TF_MS,
    Coverage,
    Health,
    InstrumentRow,
    Invalid,
    Rule,
    Run,
)
from tools.symbol_overview.validate import coverage_result, health_result, instruments_result
from tools.symbol_overview.window import window_for

JSON = "application/json; charset=utf-8"
W = window_for(T0)
S = W.start
E = W.end
NEWEST = T0 - 2 * TF_MS
HEALTH = Health(T0, RULE)
INSTR = instrument()
Edit = Callable[[dict[str, Any]], None]


def body(doc: object) -> bytes:
    return json.dumps(doc).encode()


def base() -> dict[str, Any]:
    return coverage_doc(W, oldest=S, newest=NEWEST)


def cov(doc: object, *, instr: InstrumentRow = INSTR, status: int = 200, content_type: str | None = JSON) -> Any:
    return coverage_result(status, content_type, body(doc), instrument=instr, window=W, health=HEALTH)


def setp(path: str, value: object) -> Edit:
    def edit(doc: dict[str, Any]) -> None:
        *keys, last = path.split(".")
        node: Any = doc
        for key in keys:
            node = node[int(key)] if key.isdigit() else node[key]
        node[int(last) if last.isdigit() else last] = value

    return edit


def delp(path: str) -> Edit:
    def edit(doc: dict[str, Any]) -> None:
        *keys, last = path.split(".")
        node: Any = doc
        for key in keys:
            node = node[int(key)] if key.isdigit() else node[key]
        del node[last]

    return edit


def mutated(edit: Edit, doc: dict[str, Any] | None = None) -> dict[str, Any]:
    result = copy.deepcopy(doc if doc is not None else base())
    edit(result)
    return result


# transport level (through health_result, which shares the decoder)


def test_non_200_with_the_api_error_body() -> None:
    error = {"error": {"code": "not_ready", "message": "x", "details": {"reason": "database_missing"}}}
    assert health_result(503, JSON, body(error)) == Invalid("api_error", "not_ready")


@pytest.mark.parametrize("raw", [b"<html>bad gateway</html>", b"", b'{"error": "x"}', b'{"error": {"code": 5}}'])
def test_non_200_without_the_error_body_is_bad_status(raw: bytes) -> None:
    assert health_result(502, "text/html", raw) == Invalid("bad_status", "502")


@pytest.mark.parametrize("content_type", [None, "text/html", "text/plain; charset=utf-8"])
def test_wrong_content_type(content_type: str | None) -> None:
    assert health_result(200, content_type, body(health_doc())) == Invalid("bad_content_type")


def test_body_over_the_cap() -> None:
    assert health_result(200, JSON, b" " * (MAX_BODY_BYTES + 1)) == Invalid("too_large")


@pytest.mark.parametrize("raw", [b"\xff\xfe", b"not json", b"[]", b'"x"', b"null", b"1"])
def test_not_utf8_json_or_not_an_object(raw: bytes) -> None:
    assert health_result(200, JSON, raw) == Invalid("bad_json")


# health


def test_health_valid() -> None:
    assert health_result(200, JSON, body(health_doc())) == Health(T0, RULE)


@pytest.mark.parametrize(
    "edit",
    [
        delp("as_of_ms"),
        setp("as_of_ms", "1792677000000"),
        setp("as_of_ms", True),
        setp("as_of_ms", 5),
        setp("rule.lag_threshold_ms", -1),
        setp("rule.publication_delay_ms", 1.5),
        delp("rule"),
    ],
)
def test_health_schema_errors(edit: Edit) -> None:
    assert health_result(200, JSON, body(mutated(edit, health_doc()))) == Invalid("schema")


# instruments


def instruments_doc(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"instruments": list(rows)}


def row(
    symbol: str = SYMBOL, newest: int | None = NEWEST, venue: str = "binance-usdm", timeframe: str = "5m"
) -> dict[str, Any]:
    return {"venue": venue, "symbol": symbol, "timeframe": timeframe, "newest_open_time_ms": newest}


def test_instruments_valid_and_null_newest_accepted() -> None:
    result = instruments_result(200, JSON, body(instruments_doc(row(), row("ETH/USDT:USDT", None))))
    assert result == (instrument(), instrument("ETH/USDT:USDT", None))


def test_instruments_other_venue_or_timeframe_is_accepted_and_judged_later() -> None:
    result = instruments_result(200, JSON, body(instruments_doc(row(timeframe="1h"), row("X", venue="other"))))
    assert isinstance(result, tuple)
    assert [r.timeframe for r in result] == ["1h", "5m"]


@pytest.mark.parametrize(
    "bad",
    [
        row(newest=NEWEST + 1),  # unaligned
        row(newest=5),  # out of range
        row(newest=True),
        {"venue": "binance-usdm", "symbol": SYMBOL, "timeframe": "5m"},  # missing key
        {**row(), "symbol": 5},
    ],
)
def test_instruments_schema_errors(bad: dict[str, Any]) -> None:
    assert instruments_result(200, JSON, body(instruments_doc(bad))) == Invalid("schema")


def test_instruments_duplicate_triple() -> None:
    assert instruments_result(200, JSON, body(instruments_doc(row(), row()))) == Invalid("schema")


def test_instruments_not_a_list() -> None:
    assert instruments_result(200, JSON, body({"instruments": {}})) == Invalid("schema")


def test_instruments_empty_list_is_an_error_never_an_all_green_page() -> None:
    assert instruments_result(200, JSON, body(instruments_doc())) == Invalid("empty_universe")


# coverage: valid answers


def test_valid_full_series_is_accepted() -> None:
    result = cov(base())
    assert isinstance(result, Coverage)
    assert result.oldest_open_time_ms == S
    assert result.newest_open_time_ms == NEWEST
    assert result.present_slots == 8638
    assert result.not_yet_expected_slots == 2
    assert result.runs == (Run("not_yet_expected", NEWEST + TF_MS, E),)
    assert result.as_of_ms == T0 + 1000
    assert result.rule == RULE
    assert result.stored_ahead_of_as_of is False


def test_valid_partial_history_is_accepted() -> None:
    oldest = S + 100 * TF_MS
    result = cov(coverage_doc(W, oldest=oldest, newest=NEWEST))
    assert isinstance(result, Coverage)
    assert result.missing_slots == 100
    assert result.runs[0] == Run("missing", S, oldest)


def test_valid_with_hole_conflict_and_stale_is_accepted() -> None:
    newest = T0 - 30 * TF_MS
    states = {S + 10 * TF_MS: "conflicted", S + 20 * TF_MS: "missing", S + 21 * TF_MS: "missing"}
    states |= {t: "stale" for t in range(newest + TF_MS, T0, TF_MS)}
    result = cov(coverage_doc(W, oldest=S, newest=newest, slot_states=states), instr=instrument(newest=newest))
    assert isinstance(result, Coverage)
    assert (result.conflicted_slots, result.missing_slots, result.stale_slots) == (1, 2, 29)
    assert result.not_yet_expected_slots == 1


def test_as_of_exactly_at_the_upper_bound_is_accepted() -> None:
    upper = T0 + REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS
    assert isinstance(cov(coverage_doc(W, oldest=S, newest=NEWEST, as_of_ms=upper)), Coverage)


def test_oldest_after_the_window_end_is_accepted_when_the_runs_cover_the_whole_window() -> None:
    doc = coverage_doc(W, oldest=E + TF_MS, newest=E + TF_MS, stored_ahead=True)
    assert isinstance(cov(doc, instr=instrument(newest=E + TF_MS)), Coverage)


# coverage: invalid answers

SCHEMA = [
    ("count_as_string", setp("coverage.present_slots", "8638")),
    ("count_negative", setp("coverage.stale_slots", -1)),
    ("count_bool", setp("coverage.due_slots", True)),
    ("fully_present_not_bool", setp("coverage.fully_present", 0)),
    ("series_missing", delp("series")),
    ("stored_ahead_missing", delp("stored_ahead_of_as_of")),
    ("rule_string", setp("rule.lag_threshold_ms", "60000")),
    ("as_of_string", setp("as_of_ms", "1")),
    ("as_of_out_of_range", setp("as_of_ms", 5)),
    ("runs_not_a_list", setp("runs", {})),
    ("run_without_state", delp("runs.0.state")),
    ("oldest_after_newest", setp("series.oldest_open_time_ms", T0)),
    ("oldest_unaligned", setp("series.oldest_open_time_ms", S + 1)),
    ("newest_unaligned", setp("series.newest_open_time_ms", NEWEST + 1)),
]


@pytest.mark.parametrize("edit", [e for _, e in SCHEMA], ids=[n for n, _ in SCHEMA])
def test_schema_errors(edit: Edit) -> None:
    assert cov(mutated(edit)) == Invalid("schema")


ECHO = [
    ("symbol", setp("symbol", "ETH/USDT:USDT")),
    ("venue", setp("venue", "other")),
    ("timeframe", setp("timeframe", "1h")),
    ("start", setp("start", S + TF_MS)),
    ("end", setp("end", E + TF_MS)),
]


@pytest.mark.parametrize("edit", [e for _, e in ECHO], ids=[n for n, _ in ECHO])
def test_echo_must_equal_the_request(edit: Edit) -> None:
    assert cov(mutated(edit)) == Invalid("echo")


COUNTS = [
    ("slots_wrong", setp("coverage.slots", 8639)),
    ("sum_differs", setp("coverage.present_slots", 8639)),
    ("fully_present_contradicts", setp("coverage.fully_present", True)),
]


@pytest.mark.parametrize("edit", [e for _, e in COUNTS], ids=[n for n, _ in COUNTS])
def test_counts_must_add_up(edit: Edit) -> None:
    assert cov(mutated(edit)) == Invalid("counts")


def miscount_clock_state(doc: dict[str, Any]) -> None:
    """Move one slot from present to not_yet_expected in the counts only: the total still adds up."""
    doc["coverage"]["not_yet_expected_slots"] += 1
    doc["coverage"]["present_slots"] -= 1


RUNS = [
    ("state_present", setp("runs.0.state", "present")),
    ("state_unknown", setp("runs.0.state", "weird")),
    ("start_misaligned", setp("runs.0.start", NEWEST + TF_MS + 1)),
    ("start_before_window", setp("runs.0.start", S - TF_MS)),
    ("end_after_window", setp("runs.0.end", E + TF_MS)),
    ("empty_run", setp("runs.0.end", NEWEST + TF_MS)),
    ("slots_not_the_length", setp("runs.0.slots", 3)),
    ("per_state_run_sum_differs_from_count", miscount_clock_state),
]


@pytest.mark.parametrize("edit", [e for _, e in RUNS], ids=[n for n, _ in RUNS])
def test_runs_must_be_well_formed_and_agree_with_the_counts(edit: Edit) -> None:
    assert cov(mutated(edit)) == Invalid("runs")


def two_hole_doc() -> dict[str, Any]:
    return coverage_doc(W, oldest=S, newest=NEWEST, slot_states={S + 10 * TF_MS: "missing", S + 20 * TF_MS: "missing"})


def test_runs_unordered() -> None:
    doc = two_hole_doc()
    doc["runs"][0], doc["runs"][1] = doc["runs"][1], doc["runs"][0]
    assert cov(doc) == Invalid("runs")


def test_runs_overlapping() -> None:
    doc = two_hole_doc()
    doc["runs"][1]["start"] = doc["runs"][0]["start"]
    doc["runs"][1]["slots"] = (doc["runs"][1]["end"] - doc["runs"][1]["start"]) // TF_MS
    assert cov(doc) == Invalid("runs")


@pytest.mark.parametrize("state", ["not_yet_expected", "due", "stale"])
def test_clock_state_run_at_or_before_the_newest_candle(state: str) -> None:
    doc = coverage_doc(W, oldest=S, newest=NEWEST, slot_states={S + 10 * TF_MS: state})
    assert cov(doc) == Invalid("runs")


def test_missing_run_after_the_newest_slot() -> None:
    doc = coverage_doc(W, oldest=S, newest=NEWEST, slot_states={T0: "missing"})
    assert cov(doc) == Invalid("runs")


def test_present_slot_before_the_oldest_candle() -> None:
    doc = coverage_doc(W, oldest=S + 100 * TF_MS, newest=NEWEST, slot_states={S + 50 * TF_MS: "present"})
    assert cov(doc) == Invalid("runs")


def test_missing_run_crossing_the_oldest_candle() -> None:
    oldest = S + 100 * TF_MS
    doc = coverage_doc(W, oldest=oldest, newest=NEWEST, slot_states={oldest: "missing"})
    assert cov(doc) == Invalid("inconsistent_runs")


def test_rule_must_equal_the_health_rule() -> None:
    doc = coverage_doc(W, oldest=S, newest=NEWEST, rule=Rule(15_000, 61_000))
    assert cov(doc) == Invalid("rule_mismatch")


@pytest.mark.parametrize("as_of", [T0 - 1, T0 + REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS + 1])
def test_as_of_outside_the_refresh_interval(as_of: int) -> None:
    assert cov(coverage_doc(W, oldest=S, newest=NEWEST, as_of_ms=as_of)) == Invalid("clock_inconsistent")


def test_newest_older_than_the_instruments_answer() -> None:
    assert cov(base(), instr=instrument(newest=T0 - TF_MS)) == Invalid("regressed")


def test_newest_newer_than_the_instruments_answer_is_fine() -> None:
    assert isinstance(cov(base(), instr=instrument(newest=NEWEST - TF_MS)), Coverage)


def test_a_404_unknown_series_for_a_stored_symbol() -> None:
    error = {"error": {"code": "unknown_series", "message": "x", "details": {}}}
    assert cov(error, status=404) == Invalid("inconsistent_with_instruments")


def test_other_api_errors_keep_their_code() -> None:
    error = {"error": {"code": "not_ready", "message": "x", "details": {"reason": "database_unreadable"}}}
    assert cov(error, status=503) == Invalid("api_error", "not_ready")


def test_coverage_result_needs_a_stored_instrument() -> None:
    with pytest.raises(ValueError):
        cov(base(), instr=instrument(newest=None))


# a slot after the newest candle can never be present (C§4): the runs must cover all of them


def test_every_slot_after_the_newest_candle_counted_present_is_a_contradiction() -> None:
    # A candle 30 days old, yet the answer says the whole window is present and lists no run.
    later = {t: "present" for t in range(S + TF_MS, E, TF_MS)}
    doc = coverage_doc(W, oldest=S, newest=S, slot_states=later)
    assert doc["coverage"]["fully_present"] is True and doc["runs"] == []
    assert cov(doc, instr=instrument(newest=S)) == Invalid("runs")


def test_one_slot_after_the_newest_candle_counted_present_is_a_contradiction() -> None:
    assert cov(coverage_doc(W, oldest=S, newest=NEWEST, slot_states={NEWEST + TF_MS: "present"})) == Invalid("runs")


@pytest.mark.parametrize("newest", [E, E + TF_MS])
def test_a_newest_candle_at_or_after_the_window_end_needs_the_stored_ahead_flag(newest: int) -> None:
    flagless = coverage_doc(W, oldest=newest, newest=newest, stored_ahead=False)
    assert cov(flagless, instr=instrument(newest=newest)) == Invalid("clock_inconsistent")
    flagged = coverage_doc(W, oldest=newest, newest=newest, stored_ahead=True)
    assert isinstance(cov(flagged, instr=instrument(newest=newest)), Coverage)
