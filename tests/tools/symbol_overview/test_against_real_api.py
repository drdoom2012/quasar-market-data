"""The tool against the real API application on a seeded temporary database.

This is the guard against drift between the fakes of the other tests and the contract: the real answers must pass
the tool's validation (no symbol may come out ``unknown``). The clock of the API is fixed, so the states are exact.
This directory is outside ``tests/api/``, so the socket guard of that directory does not apply.
"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn

from quasar_market_data.api.app import create_app
from quasar_market_data.config.settings import Instrument
from quasar_market_data.storage.connection import open_writer
from tests.api.seed import AT, SAMPLE_RULE, insert_candle, make_database
from tools.symbol_overview.fetch import http_fetch
from tools.symbol_overview.model import TF_MS
from tools.symbol_overview.refresh import Overviewer, Snapshot
from tools.symbol_overview.window import window_for

W = window_for(AT)
S = W.start
SYMBOLS = ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "ADA/USDT:USDT", "XRP/USDT:USDT")


def universe() -> list[Instrument]:
    return [Instrument("binance-usdm", symbol, "5m") for symbol in SYMBOLS]


@contextmanager
def serve(db_path: Path) -> Iterator[str]:
    app = create_app(db_path, rule=SAMPLE_RULE, universe=universe(), now_ms=lambda: AT)
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


def seed(path: Path) -> None:
    make_database(path)  # creates and migrates the file
    series = {
        "BTC/USDT:USDT": [t for t in range(S, AT - 2 * TF_MS + 1, TF_MS)],
        "ETH/USDT:USDT": [
            t for t in range(S, AT - 2 * TF_MS + 1, TF_MS) if not S + 4000 * TF_MS <= t < S + 4003 * TF_MS
        ],
        "SOL/USDT:USDT": [t for t in range(S + 100 * TF_MS, AT - 2 * TF_MS + 1, TF_MS)],
        "ADA/USDT:USDT": [t for t in range(S, AT - 20 * TF_MS + 1, TF_MS)],
    }
    conn = open_writer(path)
    try:
        conn.execute("BEGIN")
        for symbol, times in series.items():
            for open_time_ms in times:
                insert_candle(conn, open_time_ms, symbol=symbol)
        conn.execute("COMMIT")
    finally:
        conn.close()


def by_symbol(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    return {s["symbol"]: s for s in snapshot.body["symbols"]}  # type: ignore[attr-defined]


def test_real_answers_pass_validation_and_are_judged_as_designed(tmp_path: Path) -> None:
    db = tmp_path / "quasar.db"
    seed(db)
    with serve(db) as url:
        snapshot = Overviewer(http_fetch(url)).get()

    assert snapshot.status == 200
    symbols = by_symbol(snapshot)
    assert {s: v["state"] for s, v in symbols.items()} == {
        "BTC/USDT:USDT": "ok",
        "ETH/USDT:USDT": "holes",
        "SOL/USDT:USDT": "ok",
        "ADA/USDT:USDT": "stale",
        "XRP/USDT:USDT": "no_data",
    }
    assert snapshot.body["summary"] == {
        "total": 5,
        "no_data": 1,
        "unknown": 0,
        "stale": 1,
        "conflict": 0,
        "holes": 1,
        "behind": 0,
        "ok": 2,
    }
    assert snapshot.body["generated_at_ms"] == AT
    assert symbols["ETH/USDT:USDT"]["reasons"] == [{"code": "missing", "slots": 3, "runs": 1, "detail": ""}]
    assert symbols["ADA/USDT:USDT"]["reasons"][0]["code"] == "stale"
    sol = symbols["SOL/USDT:USDT"]
    assert sol["oldest_open_time_ms"] == S + 100 * TF_MS
    assert sol["strip"][:9] == "_" * 8 + "."  # eight whole hours before the history starts, the ninth holds slot 100
    assert sol["reasons"] == []


def test_a_database_that_does_not_exist_is_a_page_error(tmp_path: Path) -> None:
    with serve(tmp_path / "missing.db") as url:
        snapshot = Overviewer(http_fetch(url)).get()
    assert snapshot.status == 502
    assert snapshot.body == {"api_error": {"code": "api_error", "detail": "not_ready"}}
