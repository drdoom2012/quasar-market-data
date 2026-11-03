"""Validation of the health ``open_interest`` object and of ``GET /v1/open-interest/coverage`` answers (design 8.5)."""

import copy
import json
import random
from collections.abc import Callable
from typing import Any

import pytest

from tests.tools.symbol_overview.helpers import RULE, T0, health_doc, instrument
from tests.tools.symbol_overview.oi_helpers import (
    ABSENT,
    E1,
    EXPECTED_AFTER_MS,
    HORIZON_MS,
    OI_HEALTH,
    OI_WINDOW,
    api_doc,
    doc_from_states,
    health_object,
    health_with,
    slot,
)
from tools.symbol_overview.model import (
    CALL_TIMEOUT_MS,
    MAX_BODY_BYTES,
    REFRESH_DEADLINE_MS,
    TF_MS,
    Health,
    Invalid,
    OiCoverage,
    OiHealth,
    OiRule,
)
from tools.symbol_overview.validate import open_interest_coverage_result, open_interest_health

JSON = "application/json; charset=utf-8"
HEALTH = Health(T0, RULE)
O, N = slot(100), slot(8000)  # the oldest and newest row of the base answer


def check_health(body: bytes, status: int = 200) -> OiHealth | None | Invalid:
    return open_interest_health(status, JSON, body, health=HEALTH)


def validate(
    doc: dict[str, Any] | bytes, *, status: int = 200, oi_health: OiHealth = OI_HEALTH, content_type: str | None = JSON
) -> OiCoverage | Invalid:
    body = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    return open_interest_coverage_result(
        status, content_type, body, instrument=instrument(), oi_window=OI_WINDOW, health=HEALTH, oi_health=oi_health
    )


def base() -> dict[str, Any]:
    return doc_from_states(oldest=O, newest=N)


def expect(doc: dict[str, Any], code: str, detail: str = "") -> None:
    assert validate(doc) == Invalid(code, detail)


# --- the health object -----------------------------------------------------------------------------------------------


def test_the_health_object_null_is_disabled() -> None:
    assert check_health(health_with(None)) is None


def test_a_well_formed_health_object_gives_the_rule() -> None:
    assert check_health(health_with(health_object())) == OiHealth(OiRule(1, EXPECTED_AFTER_MS, 60_000))


@pytest.mark.parametrize(
    "value",
    [
        ABSENT,
        3,
        "enabled",
        [],
        True,
        health_object(as_of_ms=T0 + 1),
        health_object(as_of_ms=T0 - 1),
        {**health_object(), "as_of_ms": str(T0)},
        {**health_object(), "as_of_ms": True},
        {key: value for key, value in health_object().items() if key != "rule"},
        health_object(rule={"cadence": 1, "expected_after_ms": EXPECTED_AFTER_MS}),
        health_object(rule={"cadence": -1, "expected_after_ms": EXPECTED_AFTER_MS, "lag_threshold_ms": 60_000}),
        health_object(rule={"cadence": True, "expected_after_ms": EXPECTED_AFTER_MS, "lag_threshold_ms": 60_000}),
        health_object(rule={"cadence": 1, "expected_after_ms": "5", "lag_threshold_ms": 60_000}),
        health_object(rule={"cadence": 1.0, "expected_after_ms": EXPECTED_AFTER_MS, "lag_threshold_ms": 60_000}),
    ],
)
def test_a_missing_or_malformed_health_object_is_invalid_schema(value: object) -> None:
    result = check_health(health_with(value))

    assert isinstance(result, Invalid)
    assert result.code == "schema"


def test_a_lag_threshold_that_differs_from_the_candle_rule_is_a_rule_mismatch() -> None:
    other = health_object(rule={"cadence": 1, "expected_after_ms": EXPECTED_AFTER_MS, "lag_threshold_ms": 61_000})

    assert check_health(health_with(other)) == Invalid("rule_mismatch")


# --- accepted answers ------------------------------------------------------------------------------------------------


def test_a_valid_answer_is_accepted() -> None:
    result = validate(base())

    assert isinstance(result, OiCoverage)
    assert (result.oldest_time_ms, result.newest_time_ms, result.retrieval_horizon_ms) == (O, N, HORIZON_MS)
    assert result.as_of_ms == T0 + 1000
    assert result.present_slots == 8000 - 100 + 1
    assert result.missing_slots == 100
    assert result.not_yet_expected_slots == 8640 - 8001
    assert sum(run.end - run.start for run in result.runs) == (100 + 639) * TF_MS


def random_case(rng: random.Random) -> dict[str, Any]:
    oldest = rng.randint(-300, 8639)  # N < E' (an answer with N at or after E' is a clock failure, tested apart)
    newest = min(oldest + rng.choice([0, 1, 5, 400, 3000, 9000]), 8639)  # N < E': index at most 8639
    newest = max(newest, oldest)
    density = rng.choice([0.05, 0.3, 0.7, 1.0])
    rows = {slot(oldest), slot(newest)} | {slot(i) for i in range(oldest, newest + 1) if rng.random() < density}
    conflicts = {slot(rng.randint(-20, 8660)) for _ in range(rng.choice([0, 0, 3, 40]))}
    return api_doc(
        rows=rows,
        conflicts=conflicts,
        as_of_ms=T0 + rng.randint(0, 70_000),
        horizon=rng.choice([HORIZON_MS, 100 * TF_MS, 2000 * TF_MS, 9000 * TF_MS]),
    )


def test_an_answer_the_api_produces_always_validates() -> None:
    rng = random.Random(20261103)
    for _ in range(400):
        doc = random_case(rng)
        result = validate(doc)
        assert isinstance(result, OiCoverage), (result, doc["series"], doc["coverage"], doc["runs"][:6])


def test_no_false_rejection_of_legal_answers() -> None:
    cases = {
        "two runs of one state separated by a present slot": doc_from_states(
            oldest=O, newest=N, states={slot(500): "missing", slot(502): "missing"}
        ),
        "conflict without a row before O": doc_from_states(oldest=O, newest=N, states={slot(50): "conflicted"}),
        "conflict without a row between O and N": doc_from_states(oldest=O, newest=N, states={slot(500): "conflicted"}),
        "conflict without a row after N": doc_from_states(oldest=O, newest=N, states={slot(8010): "conflicted"}),
        "not_yet_expected before O": doc_from_states(oldest=O, newest=N, states={slot(99): "not_yet_expected"}),
        "touching runs of different states": doc_from_states(
            oldest=O, newest=N, states={slot(500): "conflicted", slot(501): "missing"}
        ),
        "the answer clock exactly at the bound": doc_from_states(
            oldest=O, newest=N, as_of_ms=T0 + REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS
        ),
        "the answer clock exactly at T0": doc_from_states(oldest=O, newest=N, as_of_ms=T0),
        "the oldest row is the last slot": doc_from_states(oldest=slot(8639), newest=slot(8639)),
        "the oldest row is the first slot": doc_from_states(oldest=slot(0), newest=slot(8000)),
        "the series began long before the window": doc_from_states(oldest=slot(-5000), newest=N),
    }
    for name, doc in cases.items():
        assert isinstance(validate(doc), OiCoverage), name


# --- rejected answers ------------------------------------------------------------------------------------------------


def mutate(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    doc = copy.deepcopy(base())
    change(doc)
    return doc


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(venue="binance-spot"),
        lambda d: d.update(symbol="ETH/USDT:USDT"),
        lambda d: d.update(period="1h"),
        lambda d: d.update(start=d["start"] + TF_MS),
        lambda d: d.update(end=d["end"] - TF_MS),
        lambda d: d.update(time_semantics="period_start"),
        lambda d: d.pop("time_semantics"),
    ],
)
def test_a_wrong_echo_is_rejected(change: Callable[[dict[str, Any]], None]) -> None:
    result = validate(mutate(change))

    assert isinstance(result, Invalid)
    assert result.code in ("echo", "schema")  # a removed field is a schema failure, a changed one an echo failure


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.pop("series"),
        lambda d: d.pop("coverage"),
        lambda d: d.pop("runs"),
        lambda d: d.update(runs={}),
        lambda d: d["series"].update(oldest_time_ms=str(O)),
        lambda d: d["series"].update(oldest_time_ms=O + 1),
        lambda d: d["series"].update(newest_time_ms=N + 1),
        lambda d: d["series"].update(oldest_time_ms=N + TF_MS),  # O > N
        lambda d: d["series"].update(newest_time_ms=True),
        lambda d: d.update(as_of_ms="1"),
        lambda d: d.update(as_of_ms=True),
        lambda d: d["rule"].update(cadence=-1),
        lambda d: d["rule"].pop("retrieval_horizon_ms"),
        lambda d: d["rule"].update(retrieval_horizon_ms=1.5),
        lambda d: d["coverage"].update(missing_slots="100"),
        lambda d: d["coverage"].pop("gap_free"),
        lambda d: d["coverage"].update(gap_free=1),
        lambda d: d["runs"][0].update(state=5),
        lambda d: d["runs"][0].pop("slots"),
        lambda d: d["runs"].append("run"),
    ],
)
def test_a_malformed_answer_is_schema(change: Callable[[dict[str, Any]], None]) -> None:
    expect(mutate(change), "schema")


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["coverage"].update(slots=8641),
        lambda d: d["coverage"].update(present_slots=d["coverage"]["present_slots"] + 1),
        lambda d: d["coverage"].update(gap_free=True),
    ],
)
def test_counts_that_do_not_add_up_are_rejected(change: Callable[[dict[str, Any]], None]) -> None:
    expect(mutate(change), "counts")


def test_gap_free_must_agree_with_present_slots() -> None:
    complete = doc_from_states(oldest=slot(0), newest=slot(8639))
    assert complete["coverage"]["gap_free"] is True
    assert isinstance(validate(complete), OiCoverage)
    complete["coverage"]["gap_free"] = False
    expect(complete, "counts")


def split_first_run(doc: dict[str, Any]) -> None:
    first = doc["runs"][0]
    middle = first["start"] + TF_MS
    doc["runs"][0:1] = [
        {**first, "end": middle, "slots": 1},
        {**first, "start": middle, "slots": first["slots"] - 1},
    ]


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["runs"][0].update(state="unavailable"),  # a candle state
        lambda d: d["runs"][0].update(state="present"),
        lambda d: d["runs"][0].update(start=d["runs"][0]["start"] + 1),
        lambda d: d["runs"][0].update(end=d["runs"][0]["end"] + 1),
        lambda d: d["runs"][-1].update(end=d["runs"][-1]["end"] + TF_MS, slots=d["runs"][-1]["slots"] + 1),  # outside
        lambda d: d["runs"][0].update(start=d["runs"][0]["start"] - TF_MS, slots=d["runs"][0]["slots"] + 1),  # outside
        lambda d: d["runs"].reverse(),
        lambda d: d["runs"][1].update(start=d["runs"][0]["end"] - TF_MS),  # overlap
        lambda d: d["runs"][0].update(slots=d["runs"][0]["slots"] + 1),
        lambda d: d["coverage"].update(  # per-state sums differ from the counts, the total still adds up
            missing_slots=d["coverage"]["missing_slots"] - 1, present_slots=d["coverage"]["present_slots"] + 1
        ),
        split_first_run,  # touching runs of one state
    ],
)
def test_malformed_runs_are_rejected(change: Callable[[dict[str, Any]], None]) -> None:
    result = validate(mutate(change))

    assert isinstance(result, Invalid)
    assert result.code == "runs"


def test_a_run_that_contains_the_oldest_or_newest_row_must_be_conflicted() -> None:
    expect(doc_from_states(oldest=O, newest=N, states={O: "missing"}), "inconsistent_runs")
    expect(doc_from_states(oldest=O, newest=N, states={N: "missing"}), "inconsistent_runs")
    expect(doc_from_states(oldest=O, newest=N, states={N: "stale"}), "inconsistent_runs")
    assert isinstance(
        validate(doc_from_states(oldest=O, newest=N, states={O: "conflicted", N: "conflicted"})), OiCoverage
    )


@pytest.mark.parametrize(
    "states",
    [
        pytest.param({N - TF_MS: "due"}, id="a due run ending at N"),
        pytest.param({N - TF_MS: "stale"}, id="a stale run ending at N"),
        pytest.param({slot(8010): "missing"}, id="a missing run after N"),
        pytest.param({slot(50): "present"}, id="a slot before O that is in no run"),
        pytest.param({slot(8010): "present"}, id="a slot after N that is in no run"),
        pytest.param({slot(50): "due"}, id="a due run before O"),
        pytest.param({slot(50): "stale"}, id="a stale run before O"),
        pytest.param({slot(10): "not_yet_expected"}, id="[not_yet_expected][missing]"),
        pytest.param({slot(8001): "due", slot(8002): "stale"}, id="[due][stale] after N"),
        pytest.param({slot(10): "no_longer_recoverable"}, id="[missing][no_longer_recoverable]"),
        pytest.param({slot(8001): "stale", slot(8002): "not_yet_expected", slot(8003): "due"}, id="[nye][due]"),
    ],
)
def test_a_contradiction_of_the_slot_model_is_rejected(states: dict[int, str]) -> None:
    expect(doc_from_states(oldest=O, newest=N, states=states), "runs")


@pytest.mark.parametrize(
    "rule",
    [
        OiRule(2, EXPECTED_AFTER_MS, 60_000),
        OiRule(1, EXPECTED_AFTER_MS + 1, 60_000),
        OiRule(1, EXPECTED_AFTER_MS, 61_000),
    ],
)
def test_a_rule_that_differs_from_the_health_object_is_a_rule_mismatch(rule: OiRule) -> None:
    assert validate(base(), oi_health=OiHealth(rule)) == Invalid("rule_mismatch")


def test_the_retrieval_horizon_is_not_compared() -> None:
    assert isinstance(validate(doc_from_states(oldest=O, newest=N, horizon=1_000_000)), OiCoverage)


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param(doc_from_states(oldest=O, newest=N, as_of_ms=T0 - 1), id="A before T0"),
        pytest.param(
            doc_from_states(oldest=O, newest=N, as_of_ms=T0 + REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS + 1),
            id="A after the bound",
        ),
        pytest.param(doc_from_states(oldest=O, newest=slot(8640)), id="N at E'"),
        pytest.param(doc_from_states(oldest=slot(8640), newest=slot(8641)), id="O and N after the window"),
        pytest.param(doc_from_states(oldest=slot(8640), newest=slot(8640)), id="O at E'"),
    ],
)
def test_clocks_that_cannot_be_consistent_are_rejected(doc: dict[str, Any]) -> None:
    assert E1 == slot(8640)
    expect(doc, "clock_inconsistent")


# --- statuses and bodies ---------------------------------------------------------------------------------------------


def error_body(code: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": "x", "details": {}}}).encode()


def test_a_404_unknown_series_comes_back_as_its_own_invalid() -> None:
    assert validate(error_body("unknown_series"), status=404) == Invalid("api_error", "unknown_series")


def test_other_failures_keep_their_codes() -> None:
    assert validate(error_body("open_interest_disabled"), status=404) == Invalid("api_error", "open_interest_disabled")
    assert validate(error_body("internal_error"), status=500) == Invalid("api_error", "internal_error")
    assert validate(b"not json", status=502) == Invalid("bad_status", "502")
    assert validate(json.dumps(base()).encode(), content_type="text/html") == Invalid("bad_content_type")
    assert validate(json.dumps(base()).encode(), content_type=None) == Invalid("bad_content_type")
    assert validate(b"[1, 2]") == Invalid("bad_json")
    assert validate(b"\xff\xfe") == Invalid("bad_json")
    assert validate(b"x" * (MAX_BODY_BYTES + 1)) == Invalid("too_large")


def test_a_health_body_without_the_object_is_not_mistaken_for_disabled() -> None:
    assert health_doc()  # the existing helper has no open_interest key, like an API without OI-5
    assert isinstance(check_health(json.dumps(health_doc()).encode()), Invalid)
