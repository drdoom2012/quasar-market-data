"""``GET /v1/open-interest/coverage`` (contract api-v1 6.6): body, order of checks, size, independence."""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.api import app as app_module
from quasar_market_data.api.database import read_open_interest_coverage
from quasar_market_data.api.open_interest_coverage_reader import OpenInterestCoverageReport
from quasar_market_data.api.params import OpenInterestCoverageRequest
from quasar_market_data.storage.connection import open_reader, open_writer
from tests.api.seed import insert_open_interest_conflict, make_database
from tests.api.test_open_interest_endpoint import (
    NOW,
    TF,
    assert_error,
    client_for,
    contract_example,
    example_database,
    slot,
)
from tests.api.test_open_interest_endpoint import (
    S as START,
)

BASE = "venue=binance-usdm&symbol=BTC/USDT:USDT&period=5m"
Q = f"{BASE}&start={START}&end={START + 7 * TF}"
PATH = "/v1/open-interest/coverage"


def test_the_contract_example_is_what_the_server_returns(tmp_path: Path) -> None:
    example = contract_example("### 6.6.3 Response `200`")
    client = client_for(example_database(tmp_path / "m.db"), example["as_of_ms"])

    response = client.get(f"{PATH}?{BASE}&start={example['start']}&end={example['end']}")

    assert response.status_code == 200
    assert response.json() == example
    assert response.text == json.dumps(example, ensure_ascii=False, separators=(",", ":"))  # key order, compact
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json; charset=utf-8"


def test_the_counts_equal_open_interest_for_the_same_window_and_as_of(tmp_path: Path) -> None:
    example = contract_example("### 6.6.3 Response `200`")
    client = client_for(example_database(tmp_path / "m.db"), example["as_of_ms"])
    window = f"start={example['start']}&end={example['end']}"

    runs = client.get(f"{PATH}?{BASE}&{window}").json()
    page = client.get(f"/v1/open-interest?{BASE}&{window}").json()

    expected = {key: value for key, value in page["coverage"].items() if key != "span"}
    assert runs["coverage"] == expected
    assert runs["as_of_ms"] == page["as_of_ms"] and runs["rule"] == page["rule"] and runs["series"] == page["series"]
    assert sum(run["slots"] for run in runs["runs"]) == runs["coverage"]["slots"] - runs["coverage"]["present_slots"]


def test_the_order_of_checks(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    present = make_database(tmp_path / "m.db", open_interest=[slot(0)])
    unknown = Q.replace("BTC/USDT:USDT", "X/Y")
    opened: list[Path] = []

    def spy(path: Path) -> sqlite3.Connection:
        opened.append(path)
        return open_reader(path)

    assert_error(client_for(missing, open_interest=None).get(f"{PATH}?limit=0"), 400, "invalid_parameter")
    disabled = client_for(present, open_interest=None, open_connection=spy)
    error = assert_error(disabled.get(f"{PATH}?{Q}"), 404, "open_interest_disabled")
    assert error["details"] == {}
    assert opened == []  # a disabled configuration opens no database
    assert_error(client_for(missing, now=0).get(f"{PATH}?{Q}"), 500, "internal_error")  # the clock before the 503
    assert_error(client_for(missing).get(f"{PATH}?{unknown}"), 503, "not_ready")
    error = assert_error(client_for(present).get(f"{PATH}?{unknown}"), 404, "unknown_series")
    assert error["details"] == {"venue": "binance-usdm", "symbol": "X/Y", "period": "5m"}
    assert error["message"] == "no open interest row of this series has been stored"


def test_a_missing_end_is_refused(tmp_path: Path) -> None:
    client = client_for(make_database(tmp_path / "m.db", open_interest=[slot(0)]))

    error = assert_error(client.get(f"{PATH}?{BASE}&start={START}"), 400, "invalid_parameter")
    assert error["details"]["parameter"] == "end"


def test_a_series_with_only_conflict_records_is_unknown(tmp_path: Path) -> None:
    client = client_for(make_database(tmp_path / "m.db", open_interest_conflicts=[slot(0)]))

    assert_error(client.get(f"{PATH}?{Q}"), 404, "unknown_series")


def test_an_unaligned_stored_time_is_a_500_never_a_count(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(1) + 7])

    error = assert_error(client_for(path).get(f"{PATH}?{Q}"), 500, "internal_error")

    assert error["details"] == {} and "7" not in error["message"]


def test_a_conflict_without_a_row_is_counted_in_the_endpoint(tmp_path: Path) -> None:
    # rows at 2 and 4 (the newest); conflict records, never a row, before the oldest, between, and after the newest
    path = make_database(
        tmp_path / "m.db", open_interest=[slot(2), slot(4)], open_interest_conflicts=[slot(1), slot(3), slot(5)]
    )

    body = client_for(path).get(f"{PATH}?{BASE}&start={slot(0)}&end={slot(7)}").json()

    assert body["coverage"]["conflicted_slots"] == 3
    assert body["coverage"]["present_slots"] == 2
    assert body["series"] == {"oldest_time_ms": slot(2), "newest_time_ms": slot(4)}  # conflicts move no bound
    conflicted = [(run["start"], run["end"]) for run in body["runs"] if run["state"] == "conflicted"]
    assert conflicted == [(slot(1), slot(2)), (slot(3), slot(4)), (slot(5), slot(6))]


def test_the_largest_body_is_under_one_megabyte(tmp_path: Path) -> None:
    slots = 10_000
    path = make_database(
        tmp_path / "m.db",
        open_interest=[slot(slots + 1)],  # a newer row: the series exists and every slot is judged
        open_interest_conflicts=[slot(i) for i in range(0, slots, 2)],
    )

    response = client_for(path, slot(slots + 20)).get(f"{PATH}?{BASE}&start={slot(0)}&end={slot(slots)}")

    assert response.status_code == 200
    body = response.json()
    assert len(body["runs"]) == slots  # a run per slot: conflicted and not conflicted alternate
    assert body["coverage"]["slots"] == slots
    assert len(response.content) < 1_000_000


def test_the_database_is_read_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = read_open_interest_coverage
    on_loop: list[bool] = []

    def spy(path: Path, request: OpenInterestCoverageRequest, **kwargs: Any) -> OpenInterestCoverageReport:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(path, request, **kwargs)

    monkeypatch.setattr(app_module, "read_open_interest_coverage", spy)
    client = client_for(make_database(tmp_path / "m.db", open_interest=[slot(0)]))

    assert client.get(f"{PATH}?{Q}").status_code == 200
    assert on_loop == [False]


def test_every_statement_the_new_endpoint_issues_is_read_only(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(3)], open_interest_conflicts=[slot(1)])
    log: list[str] = []

    def traced(target: Path) -> sqlite3.Connection:
        conn = open_reader(target)
        conn.set_trace_callback(log.append)  # only appends to a list; it never touches a connection
        return conn

    assert client_for(path, open_connection=traced).get(f"{PATH}?{Q}").status_code == 200

    assert log
    for statement in log:
        text = statement.lstrip().upper()
        assert text.startswith(("BEGIN", "SELECT", "PRAGMA", "ROLLBACK")), statement
        if text.startswith("PRAGMA"):
            assert "=" not in text, statement  # no assignment PRAGMA


def test_a_handler_does_not_write(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0)])
    before = sqlite3.connect(path).execute("SELECT COUNT(*) FROM open_interest").fetchone()

    client_for(path).get(f"{PATH}?{Q}")

    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM open_interest").fetchone() == before


def test_a_conflict_record_written_later_shows_in_the_next_request(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(2)])
    client = client_for(path, NOW)

    first = client.get(f"{PATH}?{BASE}&start={slot(0)}&end={slot(3)}").json()
    writer = open_writer(path)
    insert_open_interest_conflict(writer, slot(1), 1)
    writer.close()
    second = client.get(f"{PATH}?{BASE}&start={slot(0)}&end={slot(3)}").json()

    assert first["coverage"]["conflicted_slots"] == 0 and first["coverage"]["missing_slots"] == 1
    assert second["coverage"]["conflicted_slots"] == 1 and second["coverage"]["missing_slots"] == 0


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([slot(0) - 7, slot(2)], id="unaligned oldest, outside the window"),
        pytest.param([slot(0), slot(9) + 7], id="unaligned newest, outside the window"),
    ],
)
def test_an_unaligned_series_bound_is_a_500_for_every_window(tmp_path: Path, rows: list[int]) -> None:
    path = make_database(tmp_path / "m.db", open_interest=rows)

    error = assert_error(client_for(path).get(f"{PATH}?{BASE}&start={slot(1)}&end={slot(5)}"), 500, "internal_error")

    assert error["details"] == {} and "7" not in error["message"]


def test_a_window_ending_one_period_after_the_last_valid_time_is_answered(tmp_path: Path) -> None:
    last = 4102444800000  # the maximum valid time_ms
    path = make_database(tmp_path / "m.db", open_interest=[last])

    response = client_for(path).get(f"{PATH}?{BASE}&start={last - TF}&end={last + TF}")

    assert response.status_code == 200
    body = response.json()
    assert body["end"] == last + TF and body["coverage"]["slots"] == 2 and body["coverage"]["present_slots"] == 1
