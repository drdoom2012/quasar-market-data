import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from quasar_market_data.api.app import HEALTH_BEHIND_MAX, create_app
from quasar_market_data.domain.freshness import Freshness, SeriesFreshness, series_freshness
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import load_migrations, migrate
from quasar_market_data.storage.schema import SCHEMA_VERSION
from tests.api.seed import SAMPLE_RULE, SAMPLE_UNIVERSE, SYMBOL, VENUE, insert_candle, make_database
from tests.api.test_health_universe import s17_fields

TF = 300_000
S = 1792643400000
DELAY = SAMPLE_RULE.publication_delay_ms
LAG = SAMPLE_RULE.lag_threshold_ms
PATHS = ["/health", "/health/ready", "/health/ingest"]


def slot(n: int) -> int:
    return S + n * TF


def x_of(newest: int) -> int:
    """The instant the slot after ``newest`` is expected."""
    return newest + 2 * TF + DELAY


def client_for(path: Path, now: int | Callable[[], int] = 0, **kwargs: Any) -> TestClient:
    kwargs.setdefault("rule", SAMPLE_RULE)
    kwargs.setdefault("universe", SAMPLE_UNIVERSE)
    now_ms = now if callable(now) else (lambda: now)
    return TestClient(create_app(path, now_ms=now_ms, **kwargs), raise_server_exceptions=False)


def make_series(path: Path, newest_by_symbol: dict[str, int]) -> Path:
    make_database(path)
    conn = open_writer(path)
    for symbol, newest in newest_by_symbol.items():
        insert_candle(conn, newest, symbol=symbol)
    conn.close()
    return path


def assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    error: dict[str, Any] = body["error"]
    return error


def foreign(path: Path) -> Path:
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA application_id = 1")
    raw.execute("CREATE TABLE t (x)")
    raw.commit()
    raw.close()
    return path


def newer(path: Path) -> Path:
    make_database(path, candles=[slot(0)])
    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO schema_migrations VALUES (?, 'x', 'x', 0)", (SCHEMA_VERSION + 1,))
    raw.commit()
    raw.close()
    return path


def garbage(path: Path) -> Path:
    path.write_text("hello")
    return path


def empty(path: Path) -> Path:
    path.touch()
    return path


BROKEN = [
    ("database_missing", lambda p: p),
    ("database_unreadable", empty),
    ("schema_mismatch", foreign),
    ("schema_mismatch", newer),
    ("schema_mismatch", garbage),
]


def boom() -> int:
    raise AssertionError("the host clock must not be read")


def test_liveness_is_200_and_touches_neither_database_nor_clock(tmp_path: Path) -> None:
    def no_connection(path: Path) -> sqlite3.Connection:
        raise AssertionError("the database must not be opened")

    client = client_for(tmp_path / "missing.db", boom, open_connection=no_connection)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


@pytest.mark.parametrize(("reason", "make"), BROKEN)
def test_liveness_stays_200_when_every_readiness_failure_occurs(
    tmp_path: Path, reason: str, make: Callable[[Path], Path]
) -> None:
    client = client_for(make(tmp_path / "market.db"), boom)

    assert client.get("/health").status_code == 200
    ready = client.get("/health/ready")
    assert assert_error(ready, 503, "not_ready")["details"] == {"reason": reason}


def test_readiness_200_with_schema_version(tmp_path: Path) -> None:
    response = client_for(make_database(tmp_path / "market.db"), boom).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "schema_version": 3}


def test_v2_database_answers_503_schema_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "market.db"
    writer = open_writer(path)
    migrate(writer, applied_at_ms=1_792_677_000_000, migrations=load_migrations()[:2])
    writer.close()

    response = client_for(path).get("/health/ready")

    assert assert_error(response, 503, "not_ready")["details"] == {"reason": "schema_mismatch"}


def test_api_built_for_version_2_answers_503_on_a_v3_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The P-15 deployment row: an API of the previous schema version against a version 3 database."""
    path = make_database(tmp_path / "market.db")
    monkeypatch.setattr("quasar_market_data.storage.schema.SCHEMA_VERSION", 2)

    response = client_for(path).get("/health/ready")

    assert assert_error(response, 503, "not_ready")["details"] == {"reason": "schema_mismatch"}


@pytest.mark.parametrize(("reason", "make"), BROKEN)
def test_readiness_503_reasons_headers_and_body(tmp_path: Path, reason: str, make: Callable[[Path], Path]) -> None:
    response = client_for(make(tmp_path / "market.db")).get("/health/ready")

    error = assert_error(response, 503, "not_ready")
    assert error["details"] == {"reason": reason}
    assert response.headers.get("retry-after") == (None if reason == "schema_mismatch" else "5")


def test_readiness_is_checked_on_every_call(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db")
    client = client_for(path)
    assert client.get("/health/ready").status_code == 200

    path.unlink()
    assert assert_error(client.get("/health/ready"), 503, "not_ready")["details"] == {"reason": "database_missing"}

    make_database(path)
    assert client.get("/health/ready").status_code == 200


def test_readiness_does_not_depend_on_freshness(tmp_path: Path) -> None:
    empty_db = make_database(tmp_path / "empty.db")
    old = make_series(tmp_path / "old.db", {SYMBOL: slot(0)})
    days_later = slot(0) + 5 * 86_400_000

    assert client_for(empty_db, days_later).get("/health/ready").status_code == 200
    assert client_for(old, days_later).get("/health/ready").status_code == 200


def ingest(path: Path, as_of: int, **kwargs: Any) -> dict[str, Any]:
    response = client_for(path, as_of, **kwargs).get("/health/ingest")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    body: dict[str, Any] = response.json()
    return s17_fields(body)


def test_ingest_no_series_is_200_no_series_and_means_only_zero_stored_series(tmp_path: Path) -> None:
    migrated = make_database(tmp_path / "migrated.db")
    only_unavailable = make_database(tmp_path / "unavailable.db", unavailable=[slot(0), slot(1)])
    as_of = slot(10)

    a = ingest(migrated, as_of)
    b = ingest(only_unavailable, as_of)

    assert a == b
    assert a == {
        "as_of_ms": as_of,
        "rule": {"publication_delay_ms": DELAY, "lag_threshold_ms": LAG},
        "scope": "stored_series",
        "status": "no_series",
        "series_count": 0,
        "current": 0,
        "due": 0,
        "stale": 0,
        "behind": [],
        "truncated": False,
    }


def test_ingest_never_reports_a_series_that_was_not_stored(tmp_path: Path) -> None:
    path = make_series(tmp_path / "market.db", {"BTC/USDT:USDT": slot(0)})
    body = ingest(path, x_of(slot(0)) - 1)

    assert body["series_count"] == 1
    assert body["status"] == "current"
    assert body["scope"] == "stored_series"


def test_ingest_current_due_and_stale_counts_and_listing(tmp_path: Path) -> None:
    newest = {"A": slot(10), "B": slot(8), "C": slot(9)}
    path = make_series(tmp_path / "market.db", newest)
    # A is current (nothing after it is expected yet), C is due (just expected), B is stale (two slots overdue)
    as_of = x_of(slot(9)) + 1_000
    assert x_of(slot(10)) > as_of and x_of(slot(9)) <= as_of <= x_of(slot(9)) + LAG and as_of > x_of(slot(8)) + LAG

    body = ingest(path, as_of)

    assert (body["series_count"], body["current"], body["due"], body["stale"]) == (3, 1, 1, 1)
    assert body["status"] == "behind"
    assert body["truncated"] is False
    assert body["behind"] == [
        {
            "venue": VENUE,
            "symbol": "B",
            "timeframe": "5m",
            "newest_open_time_ms": slot(8),
            "lag_slots": 2,
            "state": "stale",
        },
        {
            "venue": VENUE,
            "symbol": "C",
            "timeframe": "5m",
            "newest_open_time_ms": slot(9),
            "lag_slots": 1,
            "state": "due",
        },
    ]


def test_ingest_every_series_due_is_catching_up_not_behind(tmp_path: Path) -> None:
    path = make_series(tmp_path / "market.db", {f"S{i}": slot(10) for i in range(3)})

    body = ingest(path, x_of(slot(10)) + 5_000)

    assert (body["current"], body["due"], body["stale"]) == (0, 3, 0)
    assert body["status"] == "catching_up"


@pytest.mark.parametrize(
    ("offset", "state", "status"),
    [(-1, None, "current"), (0, "due", "catching_up"), (LAG, "due", "catching_up"), (LAG + 1, "stale", "behind")],
)
def test_ingest_boundaries_of_the_lag_threshold(tmp_path: Path, offset: int, state: str | None, status: str) -> None:
    path = make_series(tmp_path / "market.db", {SYMBOL: slot(10)})

    body = ingest(path, x_of(slot(10)) + offset)

    assert body["status"] == status
    assert [entry["state"] for entry in body["behind"]] == ([] if state is None else [state])


def test_ingest_due_with_lag_slots_above_one_under_a_900000_threshold(tmp_path: Path) -> None:
    path = make_series(tmp_path / "market.db", {SYMBOL: slot(10)})
    rule = type(SAMPLE_RULE)(DELAY, 900_000)

    body = ingest(path, x_of(slot(10)) + 900_000, rule=rule)

    assert body["status"] == "catching_up"
    assert body["behind"][0]["state"] == "due"
    assert body["behind"][0]["lag_slots"] == 4
    assert body["rule"] == {"publication_delay_ms": DELAY, "lag_threshold_ms": 900_000}


def test_ingest_behind_is_stalest_first_and_capped_with_truncated(tmp_path: Path) -> None:
    # the stalest series (6 slots behind) have the symbols that sort LAST, so a cap applied before the sort drops them
    newest = {f"S{i:03d}": slot(100 - 1 - i % 5) for i in range(55)} | {f"Z{i}": slot(100 - 6) for i in range(5)}
    newest |= {f"C{i}": slot(100) for i in range(5)}
    path = make_series(tmp_path / "market.db", newest)
    body = ingest(path, x_of(slot(100)) + LAG + 2 * TF)  # every series is past its lag threshold

    assert body["series_count"] == 65
    assert (body["current"], body["due"], body["stale"]) == (0, 0, 65)
    assert len(body["behind"]) == HEALTH_BEHIND_MAX == 50
    assert body["truncated"] is True
    keys = [(-e["lag_slots"], e["symbol"]) for e in body["behind"]]
    assert keys == sorted(keys)  # most overdue first, then by key
    assert [e["symbol"] for e in body["behind"][:5]] == [f"Z{i}" for i in range(5)]  # the stalest survive the cap


@pytest.mark.parametrize(("count", "truncated"), [(49, False), (50, False), (51, True)])
def test_ingest_truncated_exactly_above_the_cap(tmp_path: Path, count: int, truncated: bool) -> None:
    path = make_series(tmp_path / "market.db", {f"S{i:03d}": slot(0) for i in range(count)})

    body = ingest(path, x_of(slot(0)) + LAG + 1)

    assert body["stale"] == count
    assert len(body["behind"]) == min(count, HEALTH_BEHIND_MAX)
    assert body["truncated"] is truncated


def test_ingest_stale_sorts_before_due_across_timeframes(tmp_path: Path) -> None:
    rule = type(SAMPLE_RULE)(DELAY, 900_000)
    minute, hour = Timeframe("1m"), Timeframe("1h")
    # a 1h series that is stale with a single slot of lag ...
    n_hour = hour.align_down(slot(100))
    as_of = n_hour + 2 * hour.milliseconds + DELAY + 1_000_000  # 1 000 000 ms after the next hour slot was expected
    assert series_freshness(n_hour, as_of, hour, rule) == SeriesFreshness(Freshness.STALE, 1)
    # ... next to a 1m series that is due with many slots of lag
    n_minute = minute.align_down(as_of - 800_000 - DELAY - 2 * minute.milliseconds)
    assert series_freshness(n_minute, as_of, minute, rule).state is Freshness.DUE
    assert series_freshness(n_minute, as_of, minute, rule).lag_slots > 1
    path = make_database(tmp_path / "market.db")
    conn = open_writer(path)
    insert_candle(conn, n_minute, symbol="M", timeframe="1m")
    insert_candle(conn, n_hour, symbol="H", timeframe="1h")
    conn.close()

    body = ingest(path, as_of, rule=rule)

    assert [(e["timeframe"], e["state"]) for e in body["behind"]] == [("1h", "stale"), ("1m", "due")]
    assert body["behind"][0]["lag_slots"] < body["behind"][1]["lag_slots"]  # the key is the state, not the lag


def test_ingest_holes_do_not_make_a_series_behind(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0), slot(5), slot(6)])  # holes at 1-4

    body = ingest(path, x_of(slot(6)) - 1)

    assert body["status"] == "current"
    assert body["behind"] == []


def test_ingest_stored_ahead_of_the_clock_is_current(tmp_path: Path) -> None:
    path = make_series(tmp_path / "market.db", {SYMBOL: slot(50)})

    body = ingest(path, slot(0))

    assert body["status"] == "current"
    assert body["behind"] == []


def test_ingest_a_frozen_series_is_stale_whatever_the_cause(tmp_path: Path) -> None:
    path = make_series(tmp_path / "market.db", {SYMBOL: slot(0)})

    body = ingest(path, slot(0) + 3 * 86_400_000)

    assert body["status"] == "behind"
    assert body["behind"][0]["state"] == "stale"
    assert body["behind"][0]["lag_slots"] == (3 * 86_400_000 - 2 * TF - DELAY) // TF + 1


def test_ingest_a_retired_series_keeps_status_behind(tmp_path: Path) -> None:
    path = make_series(tmp_path / "market.db", {"RETIRED": slot(0), "LIVE": slot(10_000)})

    body = ingest(path, x_of(slot(10_000)) - 1)

    assert body["status"] == "behind"
    assert [e["symbol"] for e in body["behind"]] == ["RETIRED"]
    assert body["current"] == 1


def test_ingest_an_unknown_timeframe_label_is_a_500_not_a_fabricated_status(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db")
    conn = open_writer(path)
    insert_candle(conn, slot(0), timeframe="7m")
    conn.close()

    response = client_for(path, slot(10)).get("/health/ingest")

    assert_error(response, 500, "internal_error")


def test_ingest_invalid_host_clock_is_500_even_with_an_unreadable_database(tmp_path: Path) -> None:
    response = client_for(tmp_path / "missing.db", -1).get("/health/ingest")

    assert_error(response, 500, "internal_error")


def test_ingest_503_when_database_not_ready(tmp_path: Path) -> None:
    response = client_for(tmp_path / "missing.db", slot(0)).get("/health/ingest")

    assert assert_error(response, 503, "not_ready")["details"] == {"reason": "database_missing"}
    assert response.headers["retry-after"] == "5"


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("query", ["x=1", "=", "a=1&a=2", "x", "%FF", "limit=10"])
def test_health_paths_reject_query_parameters_with_400(tmp_path: Path, path: str, query: str) -> None:
    client = client_for(make_database(tmp_path / "market.db"), slot(0))

    assert_error(client.get(f"{path}?{query}"), 400, "invalid_parameter")


@pytest.mark.parametrize("path", PATHS)
def test_an_empty_query_is_accepted(tmp_path: Path, path: str) -> None:
    client = client_for(make_database(tmp_path / "market.db"), slot(0))

    assert client.get(f"{path}?").status_code == 200


@pytest.mark.parametrize("path", PATHS)
def test_head_post_and_trailing_slash_use_the_contract_error_body(tmp_path: Path, path: str) -> None:
    client = client_for(make_database(tmp_path / "market.db"), slot(0))

    for method in ("POST", "PUT", "DELETE", "OPTIONS"):
        response = client.request(method, path)
        assert assert_error(response, 405, "method_not_allowed")["details"] == {"allow": "GET"}
        assert response.headers["allow"] == "GET"
    head = client.head(path)
    assert head.status_code == 405
    assert head.headers["allow"] == "GET"
    assert_error(client.get(f"{path}/"), 404, "not_found")


@pytest.mark.parametrize("path", PATHS)
def test_health_responses_are_no_store_utf8_json(tmp_path: Path, path: str) -> None:
    response = client_for(make_database(tmp_path / "market.db"), slot(0)).get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json; charset=utf-8"


def test_ingest_does_not_see_an_uncommitted_writer_transaction(tmp_path: Path) -> None:
    path = make_series(tmp_path / "market.db", {"A": slot(0)})
    writer = open_writer(path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        insert_candle(writer, slot(5), symbol="UNCOMMITTED")
        body = ingest(path, slot(10))
        writer.execute("COMMIT")
    finally:
        writer.close()

    assert body["series_count"] == 1
    assert ingest(path, slot(10))["series_count"] == 2


def test_ingest_an_unaligned_open_time_is_a_500_not_a_fabricated_status(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db")
    conn = open_writer(path)
    insert_candle(conn, slot(0) + 1)  # the schema checks the range, not the alignment
    conn.close()

    assert_error(client_for(path, slot(10)).get("/health/ingest"), 500, "internal_error")


def test_a_locked_database_is_not_ready_within_the_busy_timeout_and_liveness_is_unaffected(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0)])
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        client = client_for(path, slot(10), open_connection=lambda p: open_reader(p, busy_timeout_ms=50))
        ready = client.get("/health/ready")
        ingest_response = client.get("/health/ingest")
        live = client.get("/health")
    finally:
        holder.close()

    assert assert_error(ready, 503, "not_ready")["details"] == {"reason": "database_unreadable"}
    assert assert_error(ingest_response, 503, "not_ready")["details"] == {"reason": "database_unreadable"}
    assert ready.headers["retry-after"] == "5"
    assert live.status_code == 200


def test_v1_database_answers_503_schema_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "market.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=1_792_677_000_000, migrations=load_migrations()[:1])
    conn.close()

    response = client_for(path, boom).get("/health/ready")

    error = assert_error(response, 503, "not_ready")
    assert error["details"] == {"reason": "schema_mismatch"}
