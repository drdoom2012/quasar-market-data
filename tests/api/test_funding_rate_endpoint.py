"""``GET /v1/funding-rates`` (contract api-v1 6.7): body, order of checks, window and defaults, independence."""

import asyncio
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from quasar_market_data.api import app as app_module
from quasar_market_data.api.app import create_app
from quasar_market_data.api.database import read_funding_rates
from quasar_market_data.api.funding_rate_page import FundingRatePage
from quasar_market_data.api.params import FundingRatesRequest
from quasar_market_data.domain.funding_rate_coverage import MAX_FUNDING_WINDOW_MS, FundingRateRule
from quasar_market_data.storage.connection import open_writer
from tests.api.seed import (
    SAMPLE_RULE,
    SAMPLE_UNIVERSE,
    insert_funding_conflict,
    insert_funding_rate,
    make_database,
    set_funding_span,
)

RULE = FundingRateRule(publication_delay_ms=30_000, backfill_days=365)
H = 3_600_000
EIGHT = 8 * H
T0 = 1_792_656_000_017
CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "api-v1.md"
BASE = "venue=binance-usdm&symbol=BTC/USDT:USDT"


def ev(n: int) -> int:
    return T0 + n * EIGHT


NOW = ev(10)


def client_for(path: Path, now: int | Callable[[], int] = NOW, **kwargs: Any) -> TestClient:
    kwargs.setdefault("rule", SAMPLE_RULE)
    kwargs.setdefault("universe", SAMPLE_UNIVERSE)
    kwargs.setdefault("funding_rate", RULE)
    now_ms = now if callable(now) else (lambda: now)
    return TestClient(create_app(path, now_ms=now_ms, **kwargs), raise_server_exceptions=False)


def assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    error: dict[str, Any] = body["error"]
    return error


def contract_example(heading: str, *, fragment: bool = False) -> dict[str, Any]:
    """The first ```json block after ``heading``; ``fragment=True`` wraps a single ``"key": {...}`` member in braces."""
    text = CONTRACT.read_text(encoding="utf-8")
    after = text[text.index(heading) :]
    block = re.search(r"```json\n(.*?)\n```", after, re.DOTALL)
    assert block is not None
    return json.loads("{" + block.group(1) + "}" if fragment else block.group(1))  # type: ignore[no-any-return]


def example_database(path: Path) -> Path:
    """The rows of the contract example (6.7.4): an older row, four settlements in the window, one conflict."""
    make_database(path)
    conn = open_writer(path)
    try:
        insert_funding_rate(conn, 1792627200000)  # the oldest row of the series, outside the window
        insert_funding_rate(conn, 1792656000000, "Regular", funding_rate="0.0001", mark_price="82709.1")
        insert_funding_rate(conn, 1792684800000, "Regular", funding_rate="-0.00005123", mark_price="82744.5")
        insert_funding_rate(conn, 1792713600000, "Regular", funding_rate="0.00011", mark_price="82790")
        insert_funding_rate(conn, 1792713600650, "Special", funding_rate="0.0003", mark_price="82801")
        insert_funding_conflict(conn, 1792713600000, 1)
        set_funding_span(conn, 1792569600000, 1792727000000, verified_at_ms=1792727040000)
    finally:
        conn.close()
    return path


def seeded(tmp_path: Path, n: int = 5, **kwargs: Any) -> Path:
    return make_database(tmp_path / "m.db", funding=[ev(i) for i in range(n)], funding_span=(ev(0), ev(9)), **kwargs)


# --- the body ---------------------------------------------------------------------------------------------------


def test_the_contract_example_is_what_the_server_returns(tmp_path: Path) -> None:
    example = contract_example("### 6.7.4 Response `200`")
    client = client_for(example_database(tmp_path / "m.db"), example["coverage"]["as_of_ms"])

    response = client.get(f"/v1/funding-rates?{BASE}&start={example['start']}&end={example['end']}")

    assert response.status_code == 200
    assert response.json() == example
    assert response.text == json.dumps(example, ensure_ascii=False, separators=(",", ":"))  # key order, compact
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json; charset=utf-8"


def test_the_body_states_the_semantics_and_orders_its_keys(tmp_path: Path) -> None:
    body = client_for(seeded(tmp_path)).get(f"/v1/funding-rates?{BASE}&start={ev(0)}&end={ev(4)}").json()

    assert list(body) == ["venue", "symbol", "time_semantics", "start", "end", "series", "rows", "next", "coverage"]
    assert body["time_semantics"] == "settlement"
    assert list(body["series"]) == ["oldest_funding_time_ms", "newest_funding_time_ms"]
    assert list(body["rows"][0]) == ["funding_time_ms", "rate_type", "funding_rate", "mark_price", "conflicted"]
    assert list(body["coverage"]) == [
        "as_of_ms",
        "verified_from_ms",
        "verified_through_ms",
        "verified_at_ms",
        "window_state",
        "events_in_window",
        "conflicted_events",
        "complete",
    ]


def test_a_null_mark_price_a_negative_rate_and_off_hour_times_are_returned_as_stored(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding_span=(ev(0), ev(9)))
    conn = open_writer(path)
    insert_funding_rate(conn, ev(1) + 7, funding_rate="-0.0000123", mark_price=None)
    conn.close()

    body = client_for(path).get(f"/v1/funding-rates?{BASE}&start={ev(0)}&end={ev(4)}").json()

    assert body["rows"] == [
        {
            "funding_time_ms": ev(1) + 7,
            "rate_type": "Regular",
            "funding_rate": "-0.0000123",
            "mark_price": None,
            "conflicted": False,
        }
    ]
    assert body["series"] == {"oldest_funding_time_ms": ev(1) + 7, "newest_funding_time_ms": ev(1) + 7}


def test_a_series_with_a_span_and_no_row_has_null_series_bounds(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding_span=(ev(0), ev(9)))

    body = client_for(path).get(f"/v1/funding-rates?{BASE}&start={ev(0)}&end={ev(4)}").json()

    assert body["series"] == {"oldest_funding_time_ms": None, "newest_funding_time_ms": None}
    assert (body["rows"], body["next"], body["coverage"]["window_state"], body["coverage"]["complete"]) == (
        [],
        None,
        "verified",
        True,
    )


# --- the window and its defaults --------------------------------------------------------------------------------


def test_a_defaulted_end_is_now_so_the_window_is_partly_verified_and_never_complete(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[ev(0), ev(1)], funding_span=(ev(0), ev(5)))

    body = client_for(path, ev(8)).get(f"/v1/funding-rates?{BASE}&start={ev(0)}").json()

    assert body["end"] == ev(8) and body["coverage"]["as_of_ms"] == ev(8)
    assert (body["coverage"]["window_state"], body["coverage"]["complete"]) == ("partly_verified", False)
    explicit = client_for(path, ev(8)).get(f"/v1/funding-rates?{BASE}&start={ev(0)}&end={ev(5)}").json()
    assert (explicit["coverage"]["window_state"], explicit["coverage"]["complete"]) == ("verified", True)


def test_a_start_after_now_is_a_one_millisecond_unverified_window(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[ev(0)], funding_span=(ev(0), ev(5)))

    body = client_for(path, ev(6)).get(f"/v1/funding-rates?{BASE}&start={ev(9)}").json()

    assert (body["start"], body["end"], body["rows"]) == (ev(9), ev(9), [])
    assert (body["coverage"]["window_state"], body["coverage"]["events_in_window"]) == ("unverified", 0)


def test_a_defaulted_window_longer_than_3650_days_is_capped_and_echoed_never_a_400(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[ev(0)], funding_span=(ev(0), ev(5)))
    start = ev(0) - 4000 * 86_400_000

    response = client_for(path, ev(8)).get(f"/v1/funding-rates?{BASE}&start={start}")

    assert response.status_code == 200
    body = response.json()
    assert body["end"] == start + MAX_FUNDING_WINDOW_MS  # the cap, echoed
    assert body["coverage"]["window_state"] == "unverified"  # the capped window ends before the span


def test_an_explicit_window_longer_than_3650_days_is_a_400_with_the_maximum_in_ms(tmp_path: Path) -> None:
    client = client_for(seeded(tmp_path))
    end = ev(0) + MAX_FUNDING_WINDOW_MS + 1

    error = assert_error(client.get(f"/v1/funding-rates?{BASE}&start={ev(0)}&end={end}"), 400, "invalid_parameter")

    assert error["details"]["maximum"] == MAX_FUNDING_WINDOW_MS


def test_end_equal_to_start_is_a_valid_one_millisecond_window(tmp_path: Path) -> None:
    client = client_for(seeded(tmp_path))

    body = client.get(f"/v1/funding-rates?{BASE}&start={ev(1)}&end={ev(1)}").json()

    assert [r["funding_time_ms"] for r in body["rows"]] == [ev(1)]
    assert body["coverage"]["events_in_window"] == 1


def test_paging_with_next_as_start_and_the_echoed_end_reads_every_row_once(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db",
        funding=[ev(i) for i in range(7)] + [(ev(3), "Special")],
        funding_span=(ev(0), ev(20)),
    )
    client = client_for(path, ev(30))
    seen, url = [], f"/v1/funding-rates?{BASE}&start={ev(0)}&end={ev(10)}&limit=3"
    for _ in range(10):
        body = client.get(url).json()
        seen += [(r["funding_time_ms"], r["rate_type"]) for r in body["rows"]]
        if body["next"] is None:
            break
        url = f"/v1/funding-rates?{BASE}&start={body['next']}&end={body['end']}&limit=3"

    assert seen == sorted([(ev(i), "Regular") for i in range(7)] + [(ev(3), "Special")])


# --- disabled, order of checks, errors --------------------------------------------------------------------------


def test_disabled_is_404_and_opens_no_database(tmp_path: Path) -> None:
    def no_connection(path: Path) -> sqlite3.Connection:
        raise AssertionError("the database must not be opened")

    client = client_for(seeded(tmp_path), funding_rate=None, open_connection=no_connection)

    error = assert_error(client.get(f"/v1/funding-rates?{BASE}&start={ev(0)}"), 404, "funding_rate_disabled")
    assert error["details"] == {}
    assert_error(client.get(f"/v1/funding-rates?{BASE}&start={ev(0)}&limit=0"), 400, "invalid_parameter")


def test_the_other_endpoints_work_when_funding_is_disabled(tmp_path: Path) -> None:
    client = client_for(seeded(tmp_path), funding_rate=None)

    assert client.get("/health/ingest").status_code == 200
    assert client.get("/v1/instruments").status_code == 200


def test_order_of_checks(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    q = f"{BASE}&start={ev(0)}"
    unknown = q.replace("BTC/USDT:USDT", "X/Y")
    present = seeded(tmp_path)

    assert_error(client_for(missing).get("/v1/funding-rates?limit=0"), 400, "invalid_parameter")
    assert_error(client_for(missing, funding_rate=None).get(f"/v1/funding-rates?{q}"), 404, "funding_rate_disabled")
    assert_error(
        client_for(missing, funding_rate=None, now=0).get(f"/v1/funding-rates?{q}"), 404, "funding_rate_disabled"
    )
    assert_error(client_for(missing, now=0).get(f"/v1/funding-rates?{q}"), 500, "internal_error")  # clock before 503
    assert_error(client_for(missing).get(f"/v1/funding-rates?{unknown}"), 503, "not_ready")
    error = assert_error(client_for(present).get(f"/v1/funding-rates?{unknown}"), 404, "unknown_series")
    assert error["details"] == {"venue": "binance-usdm", "symbol": "X/Y"}
    assert error["message"] == "no funding rate span or row of this series has been stored"


def test_a_503_is_never_an_empty_200(tmp_path: Path) -> None:
    response = client_for(tmp_path / "missing.db").get(f"/v1/funding-rates?{BASE}&start={ev(0)}")

    error = assert_error(response, 503, "not_ready")
    assert error["details"] == {"reason": "database_missing"}
    assert response.headers["retry-after"] == "5"


def test_a_series_with_only_candles_is_an_unknown_series(tmp_path: Path) -> None:
    client = client_for(make_database(tmp_path / "m.db", candles=[ev(0)]))

    assert_error(client.get(f"/v1/funding-rates?{BASE}&start={ev(0)}"), 404, "unknown_series")


def test_method_path_and_trailing_slash_errors(tmp_path: Path) -> None:
    client = client_for(seeded(tmp_path))
    q = f"{BASE}&start={ev(0)}"

    post = client.post(f"/v1/funding-rates?{q}")
    assert_error(post, 405, "method_not_allowed")
    assert post.headers["allow"] == "GET"
    assert_error(client.get(f"/v1/funding-rates/?{q}"), 404, "not_found")


# --- the cases of ADR-0004 P-15 at API level ---------------------------------------------------------------------


def test_a_conflicted_event_is_flagged_in_its_row_counted_and_makes_the_window_incomplete(tmp_path: Path) -> None:
    path = seeded(tmp_path, funding_conflicts=[ev(2)])

    body = client_for(path).get(f"/v1/funding-rates?{BASE}&start={ev(0)}&end={ev(4)}").json()

    assert [r["conflicted"] for r in body["rows"]] == [False, False, True, False, False]
    assert (body["coverage"]["conflicted_events"], body["coverage"]["complete"]) == (1, False)
    assert body["coverage"]["window_state"] == "verified"


def test_a_window_of_2500_events_page_one_describes_the_window_and_the_later_pages_the_remainder(
    tmp_path: Path,
) -> None:
    path = make_database(
        tmp_path / "m.db",
        funding=[ev(i) for i in range(2500)],
        funding_span=(ev(0), ev(2600)),
        funding_conflicts=[ev(7)],
    )
    client = client_for(path, ev(3000))
    coverages, url = [], f"/v1/funding-rates?{BASE}&start={ev(0)}&end={ev(2499)}"
    while True:
        body = client.get(url).json()
        coverages.append(body["coverage"])
        if body["next"] is None:
            break
        url = f"/v1/funding-rates?{BASE}&start={body['next']}&end={body['end']}"

    assert [c["events_in_window"] for c in coverages] == [2500, 1500, 500]
    assert coverages[0]["conflicted_events"] == 1 and coverages[1]["conflicted_events"] == 0
    assert [c["window_state"] for c in coverages] == ["verified"] * 3


def test_the_state_changed_between_two_requests_shows_in_the_second_only(tmp_path: Path) -> None:
    path = seeded(tmp_path)
    client = client_for(path)
    url = f"/v1/funding-rates?{BASE}&start={ev(0)}&end={ev(4)}"
    before = client.get(url).json()["coverage"]

    conn = open_writer(path)
    insert_funding_conflict(conn, ev(3), 0)
    conn.close()
    after = client.get(url).json()["coverage"]

    assert (before["complete"], before["conflicted_events"]) == (True, 0)
    assert (after["complete"], after["conflicted_events"]) == (False, 1)


# --- independence -----------------------------------------------------------------------------------------------


def test_the_database_is_read_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = read_funding_rates
    on_loop: list[bool] = []

    def spy(path: Path, request: FundingRatesRequest, **kwargs: Any) -> FundingRatePage:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(path, request, **kwargs)

    monkeypatch.setattr(app_module, "read_funding_rates", spy)

    assert client_for(seeded(tmp_path)).get(f"/v1/funding-rates?{BASE}&start={ev(0)}&end={ev(4)}").status_code == 200
    assert on_loop == [False]


def test_the_other_v1_endpoints_are_byte_identical_with_funding_rows_present(tmp_path: Path) -> None:
    candles = [1_792_656_000_000 + i * 300_000 for i in range(200)]
    plain = make_database(tmp_path / "plain.db", candles=candles)
    with_funding = make_database(
        tmp_path / "funding.db",
        candles=candles,
        open_interest=[candles[i] for i in range(1, 100)],
        funding=[ev(i) for i in range(40)],
        funding_conflicts=[ev(5)],
        funding_span=(ev(0), ev(60)),
    )
    cq = f"venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start={candles[0]}"
    urls = [
        f"/v1/candles?{cq}&limit=50",
        f"/v1/coverage?{cq}&end={candles[0] + 250 * 300_000}",
        f"/v1/indicators?{cq}&end={candles[0] + 150 * 300_000}&indicators=rsi:period=2;ema:period=3",
        "/v1/instruments",
    ]

    for url in urls:
        a = client_for(plain, candles[0] + 300 * 300_000).get(url)
        b = client_for(with_funding, candles[0] + 300 * 300_000).get(url)
        assert a.status_code == b.status_code == 200, url
        assert a.text == b.text, url


def test_a_handler_does_not_write(tmp_path: Path) -> None:
    path = seeded(tmp_path)
    count = "SELECT COUNT(*) FROM funding_rates"
    before = sqlite3.connect(path).execute(count).fetchone()

    client_for(path).get(f"/v1/funding-rates?{BASE}&start={ev(0)}&end={ev(4)}")

    assert sqlite3.connect(path).execute(count).fetchone() == before
