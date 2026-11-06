"""The symbol detail against the real API application on a seeded temporary database.

The guard against drift between the fakes of the other tests and the contract: the real answers of ``/v1/candles``,
``/v1/open-interest`` and ``/v1/funding-rates`` must pass the detail's validation, and what the detail returns is what
was stored (exact strings), a hole is absent from the rows (the page draws it as a gap), a conflicted candle is marked.
The API's clock is fixed, so every figure is exact. This directory is outside ``tests/api/``, so the socket guard of
that directory does not apply.
"""

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn

from quasar_market_data.api.app import create_app
from quasar_market_data.config.settings import Instrument
from quasar_market_data.domain.funding_rate_coverage import FundingRateRule
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_writer
from tests.api.seed import (
    AT,
    SAMPLE_RULE,
    candle_values,
    insert_candle,
    insert_conflict,
    insert_funding_rate,
    insert_open_interest,
    make_database,
    open_interest_values,
)
from tools.symbol_overview.detail import detail_windows
from tools.symbol_overview.detail_refresh import Detailer
from tools.symbol_overview.fetch import http_fetch
from tools.symbol_overview.model import TF_MS

FULL, EMPTY = "BTC/USDT:USDT", "ETH/USDT:USDT"
HOLE_SLOTS = (40, 41, 42)  # slots back from the newest candle that are not stored
NEWEST = AT - 2 * TF_MS
OI_NEWEST = (AT - 500_000) // TF_MS * TF_MS
HOUR = 3_600_000
SETTLEMENTS = [AT - k * 8 * HOUR - 5 for k in range(1, 12)]
W = detail_windows(AT, "24h")


@contextmanager
def serve(db_path: Path) -> Iterator[str]:
    app = create_app(
        db_path,
        rule=SAMPLE_RULE,
        universe=[Instrument("binance-usdm", s, "5m") for s in (FULL, EMPTY)],
        open_interest=OpenInterestRule(
            cadence=1, expected_after_ms=500_000, lag_threshold_ms=60_000, period=Timeframe("5m")
        ),
        funding_rate=FundingRateRule(publication_delay_ms=30_000, backfill_days=30),
        now_ms=lambda: AT,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the API did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def insert_consistent(conn: Any, t: int, symbol: str) -> None:
    """The seed's default candles are not OHLC-consistent (a close above the high), which the detail refuses
    (fail closed); these have close == open, so high == low == open."""
    insert_candle(conn, t, symbol=symbol, close=candle_values(t)[0])


def seed(path: Path) -> None:
    make_database(path)
    conn = open_writer(path)
    try:
        conn.execute("BEGIN")
        holes = {NEWEST - n * TF_MS for n in HOLE_SLOTS}
        for t in range(W.shown.start - 10 * TF_MS, NEWEST + 1, TF_MS):
            if t not in holes:
                insert_consistent(conn, t, FULL)
        insert_conflict(conn, NEWEST - 5 * TF_MS, 0, symbol=FULL)  # a stored candle that also has a conflict record
        for t in range(W.shown.start - 10 * TF_MS + TF_MS, OI_NEWEST + 1, TF_MS):
            insert_open_interest(conn, t, symbol=FULL)
        for t in SETTLEMENTS:
            insert_funding_rate(conn, t, symbol=FULL)
        conn.execute("COMMIT")
    finally:
        conn.close()


def get(url: str, symbol: str, range_name: str = "24h") -> tuple[int, dict[str, Any]]:
    answer = Detailer(http_fetch(url)).get(symbol, "binance-usdm", "5m", range_name)
    return answer.status, json.loads(answer.body)


def test_the_real_answers_pass_the_validation_and_are_what_was_stored(tmp_path: Path) -> None:
    db = tmp_path / "quasar.db"
    seed(db)
    with serve(db) as url:
        status, doc = get(url, FULL)
    assert status == 200 and doc["as_of_ms"] == AT and doc["window"] == {"start": W.shown.start, "end": W.shown.end}
    assert [doc[p]["status"] for p in ("candles", "open_interest", "funding_rate")] == ["ok", "ok", "ok"]

    candles = doc["candles"]
    stored_o, _h, _l, _c, stored_v, stored_turnover = candle_values(NEWEST)
    stored_h = stored_l = stored_c = stored_o  # see insert_consistent
    last = candles["rows"][-1]
    assert (last["t"], last["o"], last["h"], last["l"], last["c"], last["v"]) == (
        NEWEST,
        stored_o,
        stored_h,
        stored_l,
        stored_c,
        stored_v,
    )  # exact strings
    assert last["turnover"] == stored_turnover and candles["newest_ms"] == NEWEST
    times = [r["t"] for r in candles["rows"]]
    assert all(NEWEST - n * TF_MS not in times for n in HOLE_SLOTS)  # a hole is absent, never filled in
    assert times == sorted(set(times)) and times[0] >= W.shown.start

    interest = doc["open_interest"]
    oi, value = open_interest_values(OI_NEWEST)
    assert interest["rows"][-1] == {"t": OI_NEWEST, "oi": oi, "value": value, "conflicted": False}

    funding = doc["funding_rate"]
    assert funding["latest"]["t"] == SETTLEMENTS[0] and funding["latest"]["rate_type"] == "Regular"
    assert [r["t"] for r in funding["rows"]] == sorted(t for t in SETTLEMENTS if t >= W.shown.start)


def test_a_thirty_day_detail_pages_the_real_api_and_stays_exact(tmp_path: Path) -> None:
    db = tmp_path / "quasar.db"
    make_database(db)
    conn = open_writer(db)
    try:
        conn.execute("BEGIN")
        for t in range(detail_windows(AT, "30d").shown.start, NEWEST + 1, TF_MS):
            insert_consistent(conn, t, FULL)
        conn.execute("COMMIT")
    finally:
        conn.close()
    with serve(db) as url:
        status, doc = get(url, FULL, "30d")
    assert status == 200 and doc["candles"]["status"] == "ok"
    assert (
        len(doc["candles"]["rows"]) == 8638 and doc["open_interest"]["status"] == "empty"
    )  # 8640 slots, the two newest not stored
    assert doc["funding_rate"]["status"] == "empty"  # nothing stored: the API's 404 unknown_series is an empty part


def test_a_configured_series_with_nothing_stored_and_an_unlisted_one(tmp_path: Path) -> None:
    db = tmp_path / "quasar.db"
    seed(db)
    with serve(db) as url:
        status, doc = get(url, EMPTY)
        assert status == 200 and doc["candles"]["status"] == "empty"
        assert doc["open_interest"]["status"] == "empty" and doc["funding_rate"]["status"] == "empty"
        status, doc = get(url, "XRP/USDT:USDT")
    assert status == 404 and doc == {"api_error": {"code": "unknown_series", "detail": ""}}


def test_an_ohlc_inconsistent_candle_fails_the_candle_part_and_says_why(tmp_path: Path) -> None:
    db = tmp_path / "quasar.db"
    make_database(db)
    conn = open_writer(db)
    try:
        conn.execute("BEGIN")
        for t in range(W.shown.start, NEWEST, TF_MS):
            insert_consistent(conn, t, FULL)
        insert_candle(conn, NEWEST, symbol=FULL, close="100000", high="1")  # a high below the close: impossible
        conn.execute("COMMIT")
    finally:
        conn.close()
    with serve(db) as url:
        status, doc = get(url, FULL)
    assert status == 200
    assert doc["candles"] == {"status": "error", "code": "schema", "detail": "ohlc"}  # no half-drawn series
