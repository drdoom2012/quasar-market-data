import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from quasar_market_data.api.app import create_app
from quasar_market_data.config.settings import Instrument
from quasar_market_data.storage.connection import open_reader, open_writer
from tests.api.seed import SAMPLE_RULE, SYMBOL, TIMEFRAME, VENUE, insert_candle, make_database
from tests.api.test_health_endpoints import BROKEN, assert_error

TF = 300_000
S = 1792643400000
ETH = "ETH/USDT:USDT"
LTC = "LTC/USDT:USDT"
BTC_ETH = (Instrument(VENUE, SYMBOL, TIMEFRAME), Instrument(VENUE, ETH, TIMEFRAME))


def slot(n: int) -> int:
    return S + n * TF


def client_for(path: Path, universe: Sequence[Instrument], **kwargs: Any) -> TestClient:
    return TestClient(create_app(path, rule=SAMPLE_RULE, universe=universe, **kwargs), raise_server_exceptions=False)


def entry(symbol: str, newest: int | None) -> dict[str, Any]:
    return {"venue": VENUE, "symbol": symbol, "timeframe": TIMEFRAME, "newest_open_time_ms": newest}


def test_lists_configured_instruments_with_newest_or_null(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0), slot(1), slot(2)])

    response = client_for(path, BTC_ETH).get("/v1/instruments")

    assert response.status_code == 200
    assert response.json() == {"instruments": [entry(SYMBOL, slot(2)), entry(ETH, None)]}
    assert [list(e) for e in response.json()["instruments"]] == [
        ["venue", "symbol", "timeframe", "newest_open_time_ms"]
    ] * 2


def test_a_stored_series_that_is_not_configured_is_absent(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0)], symbol=LTC)

    body = client_for(path, (Instrument(VENUE, SYMBOL, TIMEFRAME),)).get("/v1/instruments").json()

    assert body == {"instruments": [entry(SYMBOL, None)]}


def test_order_is_by_triple_whatever_the_universe_order(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db")
    universe = (
        Instrument("bybit", SYMBOL, "5m"),
        Instrument(VENUE, SYMBOL, "5m"),
        Instrument(VENUE, ETH, "5m"),
        Instrument(VENUE, ETH, "15m"),
    )
    expected = sorted((i.venue, i.symbol, i.timeframe) for i in universe)

    for given in (universe, tuple(reversed(universe))):
        body = client_for(path, given).get("/v1/instruments").json()
        assert [(e["venue"], e["symbol"], e["timeframe"]) for e in body["instruments"]] == expected


def test_create_app_holds_the_universe_sorted_by_triple(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = make_database(tmp_path / "market.db")
    seen: list[Sequence[Instrument]] = []

    def spy(db_path: Path, universe: Sequence[Instrument], **kwargs: Any) -> tuple[()]:
        seen.append(universe)
        return ()

    monkeypatch.setattr("quasar_market_data.api.app.read_instruments", spy)

    client_for(path, tuple(reversed(BTC_ETH))).get("/v1/instruments")

    assert seen == [tuple(sorted(BTC_ETH))]


def test_body_is_exactly_the_instruments_key(tmp_path: Path) -> None:
    body = client_for(make_database(tmp_path / "market.db"), BTC_ETH).get("/v1/instruments").json()

    assert set(body) == {"instruments"}


@pytest.mark.parametrize(("reason", "make"), BROKEN)
def test_503_when_the_database_is_not_ready(tmp_path: Path, reason: str, make: Callable[[Path], Path]) -> None:
    response = client_for(make(tmp_path / "market.db"), BTC_ETH).get("/v1/instruments")

    error = assert_error(response, 503, "not_ready")
    assert error["details"] == {"reason": reason}
    assert response.headers.get("retry-after") == (None if reason == "schema_mismatch" else "5")


@pytest.mark.parametrize("query", ["x=1", "=", "a=1&a=2", "x", "%FF"])
def test_a_query_parameter_is_400(tmp_path: Path, query: str) -> None:
    client = client_for(make_database(tmp_path / "market.db"), BTC_ETH)

    assert_error(client.get(f"/v1/instruments?{query}"), 400, "invalid_parameter")


def test_an_empty_query_is_accepted(tmp_path: Path) -> None:
    assert client_for(make_database(tmp_path / "market.db"), BTC_ETH).get("/v1/instruments?").status_code == 200


def test_no_store_utf8_json_and_405_for_post(tmp_path: Path) -> None:
    client = client_for(make_database(tmp_path / "market.db"), BTC_ETH)

    response = client.get("/v1/instruments")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    for method in ("POST", "PUT", "DELETE"):
        refused = client.request(method, "/v1/instruments")
        assert assert_error(refused, 405, "method_not_allowed")["details"] == {"allow": "GET"}
    assert_error(client.get("/v1/instruments/"), 404, "not_found")


def _bulk(path: Path, rows: int) -> None:
    make_database(path)
    conn = open_writer(path)
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover,"
        " ingested_at_ms) VALUES (?, ?, ?, ?, '1', '2', '1', '1', '1', NULL, 0)",
        [(VENUE, SYMBOL, TIMEFRAME, slot(k)) for k in range(rows)],
    )
    conn.execute("COMMIT")
    conn.close()


def _traced(path: Path, universe: Sequence[Instrument]) -> tuple[list[str], int]:
    statements: list[str] = []
    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    def factory(p: Path) -> sqlite3.Connection:
        conn = open_reader(p)
        conn.set_trace_callback(statements.append)
        conn.set_progress_handler(tick, 1)
        return conn

    assert client_for(path, universe, open_connection=factory).get("/v1/instruments").status_code == 200
    return statements, steps


def test_reads_one_seek_per_configured_instrument(tmp_path: Path) -> None:
    small, large = tmp_path / "small.db", tmp_path / "large.db"
    _bulk(small, 3)
    _bulk(large, 30_000)
    universe = tuple(Instrument(VENUE, f"S{i}/USDT:USDT", TIMEFRAME) for i in range(7)) + BTC_ETH[:1]

    small_statements, small_steps = _traced(small, universe)
    large_statements, large_steps = _traced(large, universe)

    for statements in (small_statements, large_statements):
        assert len([s for s in statements if "MAX(open_time_ms)" in s]) == len(universe)
        assert not any("GROUP BY" in s for s in statements)
    assert large_steps < 2 * small_steps + 100  # the work does not grow with the rows of the series


def test_the_answer_is_one_snapshot(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0)])
    fired: list[int] = []

    def factory(p: Path) -> Any:
        real = open_reader(p)

        class Proxy:
            def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
                cursor = real.execute(sql, params)
                if "MAX(open_time_ms)" in sql and not fired:
                    fired.append(1)  # after the first series was read, a writer commits to both series
                    writer = open_writer(p)
                    insert_candle(writer, slot(9), symbol=SYMBOL)
                    insert_candle(writer, slot(9), symbol=ETH)
                    writer.close()
                return cursor

            def close(self) -> None:
                real.close()

        return Proxy()

    client = client_for(path, BTC_ETH, open_connection=factory)
    first = client.get("/v1/instruments").json()
    second = client_for(path, BTC_ETH).get("/v1/instruments").json()

    assert first == {"instruments": [entry(SYMBOL, slot(0)), entry(ETH, None)]}  # ETH is not seen at slot 9
    assert second == {"instruments": [entry(SYMBOL, slot(9)), entry(ETH, slot(9))]}
