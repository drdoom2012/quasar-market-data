"""The tool with open interest against the real API application on a seeded temporary database (OI-6b).

The guard against drift between the fakes of the other tests and the contract: the real answers of
``GET /v1/open-interest/coverage`` must pass the tool's validation (no symbol may come out ``unknown``) and be judged
as designed. The API's clock is fixed, so every state is exact. Written after the implementation: it passes on its
first run and is a check, not a red-green step.
"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import uvicorn

from quasar_market_data.api.app import create_app
from quasar_market_data.config.settings import Instrument
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_writer
from tests.api.seed import AT, SAMPLE_RULE, insert_candle, insert_open_interest, make_database
from tools.symbol_overview.fetch import Fetch, RawResponse, http_fetch
from tools.symbol_overview.model import TF_MS
from tools.symbol_overview.refresh import Overviewer, Snapshot
from tools.symbol_overview.window import open_interest_window, window_for

W = window_for(AT)
S = W.start
S1 = open_interest_window(W).start  # S'
EXPECTED_AFTER_MS = 500_000
NEWEST_EXPECTED = (AT - EXPECTED_AFTER_MS) // TF_MS * TF_MS  # the newest slot a row is expected for at AT
OI_RULE = OpenInterestRule(
    cadence=1, expected_after_ms=EXPECTED_AFTER_MS, lag_threshold_ms=60_000, period=Timeframe("5m")
)
FULL, HOLE, YOUNG, UNRECOVERABLE, NONE = (
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "ADA/USDT:USDT",
    "XRP/USDT:USDT",
)
SYMBOLS = (FULL, HOLE, YOUNG, UNRECOVERABLE, NONE)


def universe() -> list[Instrument]:
    return [Instrument("binance-usdm", symbol, "5m") for symbol in SYMBOLS]


@contextmanager
def serve(db_path: Path, *, open_interest: OpenInterestRule | None) -> Iterator[str]:
    app = create_app(db_path, rule=SAMPLE_RULE, universe=universe(), now_ms=lambda: AT, open_interest=open_interest)
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
    make_database(path)
    candle_times = range(S, AT - 2 * TF_MS + 1, TF_MS)
    # open interest rows, as slot ENDS: every series is current (a row for the newest expected slot)
    full = list(range(S1 - 100 * TF_MS, NEWEST_EXPECTED + 1, TF_MS))  # began before the window
    hole = [t for t in range(S1, NEWEST_EXPECTED + 1, TF_MS) if not S1 + 4000 * TF_MS <= t < S1 + 4003 * TF_MS]
    young = list(range(S1 + 100 * TF_MS, NEWEST_EXPECTED + 1, TF_MS))  # began 100 slots into the window
    # a hole inside the stored history, in the first day of the window: older than the retrieval horizon
    unrecoverable = [t for t in range(S1, NEWEST_EXPECTED + 1, TF_MS) if not S1 + 100 * TF_MS <= t < S1 + 104 * TF_MS]
    conn = open_writer(path)
    try:
        conn.execute("BEGIN")
        for symbol in SYMBOLS:
            for open_time_ms in candle_times:
                insert_candle(conn, open_time_ms, symbol=symbol)
        for symbol, times in ((FULL, full), (HOLE, hole), (YOUNG, young), (UNRECOVERABLE, unrecoverable)):
            for time_ms in times:
                insert_open_interest(conn, time_ms, symbol=symbol)
        conn.execute("COMMIT")
    finally:
        conn.close()


def body_of(snapshot: Snapshot) -> dict[str, Any]:
    return cast(dict[str, Any], snapshot.body)


def by_symbol(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    return {s["symbol"]: s for s in body_of(snapshot)["symbols"]}


def recording(url: str) -> tuple[Fetch, list[str]]:
    real = http_fetch(url)
    paths: list[str] = []

    def fetch(path: str) -> RawResponse:
        paths.append(path.partition("?")[0])
        return real(path)

    return fetch, paths


def test_the_real_answers_pass_validation_and_are_judged_as_designed(tmp_path: Path) -> None:
    db = tmp_path / "quasar.db"
    seed(db)
    with serve(db, open_interest=OI_RULE) as url:
        fetch, _ = recording(url)
        snapshot = Overviewer(fetch).get()

    assert snapshot.status == 200
    body = body_of(snapshot)
    assert body["open_interest"]["status"] == "enabled"
    symbols = by_symbol(snapshot)
    assert {s: v["state"] for s, v in symbols.items()} == dict.fromkeys(SYMBOLS, "ok")  # the candle part is fine
    states = {s: v["open_interest"]["state"] for s, v in symbols.items()}
    assert states == {FULL: "ok", HOLE: "holes", YOUNG: "ok", UNRECOVERABLE: "ok", NONE: "no_data"}
    assert body["open_interest"]["summary"] == {
        "total": 5,
        "no_data": 1,
        "unknown": 0,
        "stale": 0,
        "conflict": 0,
        "holes": 1,
        "behind": 0,
        "ok": 3,
    }

    full = symbols[FULL]["open_interest"]
    assert full["reasons"] == [] and full["strip"][:1] == "." and "_" not in full["strip"]
    assert full["oldest_time_ms"] == S1 - 100 * TF_MS
    hole = symbols[HOLE]["open_interest"]
    assert hole["reasons"] == [{"code": "missing", "slots": 3, "runs": 1, "detail": ""}]
    assert hole["strip"][4000 // 12] == "m"
    young = symbols[YOUNG]["open_interest"]
    assert young["reasons"] == [] and young["oldest_time_ms"] == S1 + 100 * TF_MS
    assert young["strip"][:9] == "_" * 8 + "."  # eight whole hours before the first stored row
    gone = symbols[UNRECOVERABLE]["open_interest"]
    assert gone["reasons"] == [{"code": "unrecoverable", "slots": 4, "runs": 1, "detail": ""}]
    assert gone["strip"][100 // 12] == "r" and gone["retrieval_horizon_ms"] == OI_RULE.retrieval_horizon_ms
    none = symbols[NONE]["open_interest"]
    assert none["reasons"] == [{"code": "no_data", "slots": 0, "runs": 0, "detail": ""}] and none["strip"] is None


def test_disabled_makes_no_open_interest_call_and_leaves_the_candle_part_as_it_was(tmp_path: Path) -> None:
    db = tmp_path / "quasar.db"
    seed(db)
    with serve(db, open_interest=OI_RULE) as url:
        enabled = Overviewer(recording(url)[0]).get()
    with serve(db, open_interest=None) as url:
        fetch, paths = recording(url)
        disabled = Overviewer(fetch).get()

    assert "/v1/open-interest/coverage" not in paths
    assert body_of(disabled)["open_interest"]["status"] == "disabled"
    assert all(s["open_interest"] is None for s in by_symbol(disabled).values())
    for symbol in SYMBOLS:  # every candle field is the same with open interest enabled and disabled
        on = {k: v for k, v in by_symbol(enabled)[symbol].items() if k != "open_interest"}
        off = {k: v for k, v in by_symbol(disabled)[symbol].items() if k != "open_interest"}
        assert on == off
    assert body_of(enabled)["summary"] == body_of(disabled)["summary"]
