import random
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from quasar_market_data.api.app import create_app
from quasar_market_data.api.params import MAX_WINDOW_SLOTS
from quasar_market_data.api.rule import MAX_LAG_THRESHOLD_MS
from quasar_market_data.domain.timed_coverage import ExpectationRule
from tests.api.seed import SAMPLE_RULE, SAMPLE_UNIVERSE, make_database, make_database_from_fixture

TF = 300_000
S = 1792643400000
NEWEST = 1792673100000  # the newest candle of the fixture database
AS_OF = 1792674030000
SERIES = "venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m"
DELAY = SAMPLE_RULE.publication_delay_ms
LAG = SAMPLE_RULE.lag_threshold_ms


def slot(n: int) -> int:
    return S + n * TF


def query(start: int, end: int) -> str:
    return f"{SERIES}&start={start}&end={end}"


def client_for(path: Path, **kwargs: Any) -> TestClient:
    kwargs.setdefault("rule", SAMPLE_RULE)
    kwargs.setdefault("universe", SAMPLE_UNIVERSE)
    return TestClient(create_app(path, **kwargs), raise_server_exceptions=False)


def assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    error: dict[str, Any] = body["error"]
    return error


def state_at(directory: Path, as_of: int) -> str:
    """The state of the one slot after a single stored candle, with the clock at ``as_of``."""
    db = make_database(directory / "m.db", candles=[slot(0)])
    response = client_for(db, now_ms=lambda: as_of).get(f"/v1/coverage?{query(slot(1), slot(2))}")
    assert response.status_code == 200
    (run,) = response.json()["runs"]
    state: str = run["state"]
    return state


def test_the_contract_example_is_what_the_server_returns(tmp_path: Path) -> None:
    client = client_for(make_database_from_fixture(tmp_path / "market.db"), now_ms=lambda: AS_OF)

    response = client.get(f"/v1/coverage?{query(1792672500000, 1792674300000)}")

    assert response.status_code == 200
    assert response.json() == {
        "venue": "binance-usdm",
        "symbol": "BTC/USDT:USDT",
        "timeframe": "5m",
        "start": 1792672500000,
        "end": 1792674300000,
        "as_of_ms": 1792674030000,
        "rule": {"publication_delay_ms": 15000, "lag_threshold_ms": 60000},
        "series": {"oldest_open_time_ms": 1792643400000, "newest_open_time_ms": 1792673100000},
        "stored_ahead_of_as_of": False,
        "coverage": {
            "slots": 6,
            "present_slots": 3,
            "conflicted_slots": 0,
            "unavailable_slots": 0,
            "missing_slots": 0,
            "not_yet_expected_slots": 1,
            "due_slots": 1,
            "stale_slots": 1,
            "fully_present": False,
        },
        "runs": [
            {"state": "stale", "start": 1792673400000, "end": 1792673700000, "slots": 1},
            {"state": "due", "start": 1792673700000, "end": 1792674000000, "slots": 1},
            {"state": "not_yet_expected", "start": 1792674000000, "end": 1792674300000, "slots": 1},
        ],
    }
    assert list(response.json()) == [
        "venue", "symbol", "timeframe", "start", "end", "as_of_ms", "rule", "series",
        "stored_ahead_of_as_of", "coverage", "runs",
    ]  # fmt: skip


def test_the_response_states_as_of_and_the_rule(tmp_path: Path) -> None:
    db = make_database(tmp_path / "m.db", candles=[slot(0)])
    rule = ExpectationRule(1000, 2000)

    body = client_for(db, now_ms=lambda: AS_OF, rule=rule).get(f"/v1/coverage?{query(slot(0), slot(1))}").json()

    assert body["as_of_ms"] == AS_OF
    assert body["rule"] == {"publication_delay_ms": 1000, "lag_threshold_ms": 2000}
    assert body["coverage"]["fully_present"] is True
    assert body["runs"] == []


def test_clock_boundaries_over_http(tmp_path: Path) -> None:
    expected_at = slot(0) + 2 * TF + DELAY
    observed = []
    for name, as_of in (
        ("a", expected_at - 1),
        ("b", expected_at),
        ("c", expected_at + LAG),
        ("d", expected_at + LAG + 1),
    ):
        (tmp_path / name).mkdir()
        observed.append(state_at(tmp_path / name, as_of))

    assert observed == ["not_yet_expected", "due", "due", "stale"]


def test_a_dead_series_is_stale_not_present(tmp_path: Path) -> None:
    db = make_database(tmp_path / "m.db", candles=[slot(0)])

    body = client_for(db, now_ms=lambda: slot(1000)).get(f"/v1/coverage?{query(slot(1), slot(4))}").json()

    assert body["coverage"]["stale_slots"] == 3
    assert body["coverage"]["present_slots"] == 0
    assert body["coverage"]["fully_present"] is False
    assert body["runs"] == [{"state": "stale", "start": slot(1), "end": slot(4), "slots": 3}]


def test_a_window_entirely_in_the_future_is_not_yet_expected(tmp_path: Path) -> None:
    db = make_database(tmp_path / "m.db", candles=[slot(0)])

    body = client_for(db, now_ms=lambda: slot(1)).get(f"/v1/coverage?{query(slot(10), slot(14))}").json()

    assert body["coverage"]["not_yet_expected_slots"] == 4
    assert body["runs"] == [{"state": "not_yet_expected", "start": slot(10), "end": slot(14), "slots": 4}]


def test_a_reader_clock_behind_the_data_sets_stored_ahead_of_as_of_and_keeps_the_candle_present(
    tmp_path: Path,
) -> None:
    db = make_database(tmp_path / "m.db", candles=[slot(0)])

    body = client_for(db, now_ms=lambda: slot(0)).get(f"/v1/coverage?{query(slot(0), slot(3))}").json()

    assert body["stored_ahead_of_as_of"] is True
    assert body["coverage"]["present_slots"] == 1
    assert body["coverage"]["not_yet_expected_slots"] == 2
    assert body["runs"] == [{"state": "not_yet_expected", "start": slot(1), "end": slot(3), "slots": 2}]


def test_runs_list_exactly_the_non_present_slots_with_their_slot_counts(tmp_path: Path) -> None:
    rng = random.Random(21)
    n = 300
    kinds = [rng.choice(["present", "conflicted", "unavailable", "missing"]) for _ in range(n - 1)] + ["present"]
    db = make_database(
        tmp_path / "m.db",
        candles=[slot(i) for i, k in enumerate(kinds) if k == "present"],
        conflicts=[slot(i) for i, k in enumerate(kinds) if k == "conflicted"],
        unavailable=[slot(i) for i, k in enumerate(kinds) if k == "unavailable"],
    )

    body = client_for(db, now_ms=lambda: slot(n + 10)).get(f"/v1/coverage?{query(slot(0), slot(n))}").json()

    runs = body["runs"]
    assert all(r["state"] != "present" for r in runs)
    assert [r["start"] for r in runs] == sorted(r["start"] for r in runs)
    assert all(r["slots"] == (r["end"] - r["start"]) // TF for r in runs)
    summed: Counter[str] = Counter()
    for r in runs:
        summed[r["state"]] += r["slots"]
    expected = Counter(k for k in kinds if k != "present")
    assert summed == expected
    for state in ("conflicted", "unavailable", "missing"):
        assert body["coverage"][f"{state}_slots"] == expected[state]
    assert body["coverage"]["present_slots"] == kinds.count("present")
    covered = {slot_open for r in runs for slot_open in range(r["start"], r["end"], TF)}
    assert covered == {slot(i) for i, k in enumerate(kinds) if k != "present"}


def test_the_true_maximum_window_answers_200(tmp_path: Path) -> None:
    end = slot(MAX_WINDOW_SLOTS)
    db = make_database(tmp_path / "m.db", candles=[end], conflicts=[slot(i) for i in range(0, MAX_WINDOW_SLOTS, 2)])

    response = client_for(db, now_ms=lambda: slot(MAX_WINDOW_SLOTS + 1000)).get(f"/v1/coverage?{query(slot(0), end)}")

    assert response.status_code == 200
    body = response.json()
    runs = body["runs"]
    assert len(runs) == 10_000
    assert all(r["slots"] == 1 for r in runs)
    assert sum(r["slots"] for r in runs) == 10_000
    assert body["coverage"]["conflicted_slots"] == 5000
    assert body["coverage"]["missing_slots"] == 5000
    assert body["coverage"]["fully_present"] is False


def test_a_window_of_10001_slots_is_400(tmp_path: Path) -> None:
    db = make_database(tmp_path / "m.db", candles=[slot(0)])

    response = client_for(db).get(f"/v1/coverage?{query(slot(0), slot(MAX_WINDOW_SLOTS + 1))}")

    assert assert_error(response, 400, "invalid_parameter")["details"]["maximum"] == 10_000


def test_create_app_refuses_an_out_of_bound_rule(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        create_app(tmp_path / "m.db", rule=ExpectationRule(0, MAX_LAG_THRESHOLD_MS + 1), universe=SAMPLE_UNIVERSE)


def test_request_errors(tmp_path: Path) -> None:
    db = make_database(tmp_path / "m.db", candles=[slot(0)])
    client = client_for(db, now_ms=lambda: AS_OF)

    missing_end = client.get(f"/v1/coverage?{SERIES}&start={slot(0)}")
    assert assert_error(missing_end, 400, "invalid_parameter")["details"]["parameter"] == "end"
    for extra in ("as_of=1", "limit=1"):
        assert_error(client.get(f"/v1/coverage?{query(slot(0), slot(1))}&{extra}"), 400, "invalid_parameter")
    unknown = query(slot(0), slot(1)).replace("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert_error(client.get(f"/v1/coverage?{unknown}"), 404, "unknown_series")


def test_503_for_a_missing_database_and_400_precedes_it(tmp_path: Path) -> None:
    client = client_for(tmp_path / "missing.db", now_ms=lambda: AS_OF)

    response = client.get(f"/v1/coverage?{query(slot(0), slot(1))}")
    assert assert_error(response, 503, "not_ready")["details"] == {"reason": "database_missing"}
    assert response.headers["retry-after"] == "5"
    assert_error(client.get(f"/v1/coverage?{SERIES}&start={slot(0)}"), 400, "invalid_parameter")


def test_other_methods_and_paths(tmp_path: Path) -> None:
    client = client_for(make_database(tmp_path / "m.db", candles=[slot(0)]))

    head = client.head(f"/v1/coverage?{query(slot(0), slot(1))}")
    assert head.status_code == 405
    assert head.headers["allow"] == "GET"
    post = client.post("/v1/coverage")
    assert_error(post, 405, "method_not_allowed")
    assert post.headers["allow"] == "GET"
    assert_error(client.get(f"/v1/coverage/?{query(slot(0), slot(1))}"), 404, "not_found")


def test_a_clock_outside_the_valid_range_is_a_500_with_the_standard_body(tmp_path: Path) -> None:
    db = make_database(tmp_path / "m.db", candles=[slot(0)])

    response = client_for(db, now_ms=lambda: 0).get(f"/v1/coverage?{query(slot(0), slot(1))}")

    assert_error(response, 500, "internal_error")


def test_the_clock_is_read_once_per_request(tmp_path: Path) -> None:
    db = make_database(tmp_path / "m.db", candles=[slot(0)])
    calls: list[int] = []

    def clock() -> int:
        calls.append(1)
        return AS_OF

    client = client_for(db, now_ms=clock)

    assert client.get(f"/v1/coverage?{query(slot(0), slot(1))}").status_code == 200
    assert len(calls) == 1
    assert client.get(f"/v1/coverage?{SERIES}&start={slot(0)}").status_code == 400
    assert len(calls) == 1


def test_no_store_header(tmp_path: Path) -> None:
    client = client_for(make_database(tmp_path / "m.db", candles=[slot(0)]), now_ms=lambda: AS_OF)
    unknown = query(slot(0), slot(1)).replace("BTC/USDT:USDT", "ETH/USDT:USDT")
    missing = client_for(tmp_path / "missing.db", now_ms=lambda: AS_OF)
    bad_clock = client_for(tmp_path / "m.db", now_ms=lambda: 0)

    responses = {
        200: client.get(f"/v1/coverage?{query(slot(0), slot(1))}"),
        400: client.get(f"/v1/coverage?{SERIES}"),
        404: client.get(f"/v1/coverage?{unknown}"),
        405: client.post("/v1/coverage"),
        503: missing.get(f"/v1/coverage?{query(slot(0), slot(1))}"),
        500: bad_clock.get(f"/v1/coverage?{query(slot(0), slot(1))}"),
    }
    for status, response in responses.items():
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store", status


def test_the_order_of_errors_with_real_databases(tmp_path: Path) -> None:
    existing = client_for(make_database(tmp_path / "m.db", candles=[slot(0)]), now_ms=lambda: AS_OF)
    lacking = make_database(tmp_path / "other.db", candles=[slot(0)], symbol="ETH/USDT:USDT")
    missing = client_for(tmp_path / "missing.db", now_ms=lambda: AS_OF)

    malformed = client_for(lacking, now_ms=lambda: AS_OF).get(f"/v1/coverage?{query(slot(1), slot(1))}")
    assert_error(malformed, 400, "invalid_parameter")
    assert_error(existing.get(f"/v1/coverage?{query(slot(0), slot(1))}".replace("BTC", "ETH")), 404, "unknown_series")
    assert_error(missing.get(f"/v1/coverage?{query(slot(0), slot(1))}"), 503, "not_ready")
