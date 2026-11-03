"""Cost of the ``open_interest`` object of ``/health/ingest`` (OI-5 plan, task 6 step 5). Not part of ``make check``.

Builds a synthetic database in a fresh temporary directory (never production data, no network) with ``SERIES``
candle series and ``SERIES`` open interest series (plus a few conflicts) of ``ROWS`` rows each, and times the whole
``GET /health/ingest`` handler in process with open interest off and on. The cost is a seek per series, not per row
(``tests/api/test_open_interest_health.py`` asserts the statement count), so a small ``ROWS`` is representative of the
statements; it is NOT a figure for a 60-million-row database. The page cache is not dropped. The reference is the
250 ms target of S17 (H2). Run by hand: ``make bench-health-oi`` (``QUASAR_BENCH_ROWS=8352 make bench-health-oi`` for 29 days of 5m per series).
"""

import json
import os
import platform
import shutil
import sqlite3
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter

from starlette.testclient import TestClient

from quasar_market_data.api.app import create_app
from quasar_market_data.config.settings import Instrument
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate
from tests.api.seed import SAMPLE_RULE

BASE_MS = 1_700_000_100_000
TF_MS = 300_000
VENUE = "bench"
SERIES = 600
ROWS = int(os.environ.get("QUASAR_BENCH_ROWS", "300"))  # 8352 = 29 days of 5m, the open interest horizon
REPEATS = 9
H2_TARGET_MS = 250
RULE = OpenInterestRule(cadence=5, expected_after_ms=1_700_000, lag_threshold_ms=60_000, period=Timeframe("5m"))


def symbol_name(index: int) -> str:
    return f"SYM{index:04d}/USDT:USDT"


def build_database(path: Path) -> None:
    conn = open_writer(path)
    try:
        migrate(conn, applied_at_ms=BASE_MS)
        conn.execute("BEGIN")
        for i in range(SERIES):
            conn.executemany(
                "INSERT INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
                " turnover, ingested_at_ms) VALUES (?, ?, '5m', ?, '1', '2', '1', '1', '1', NULL, ?)",
                ((VENUE, symbol_name(i), BASE_MS + k * TF_MS, BASE_MS) for k in range(ROWS)),
            )
            conn.executemany(
                "INSERT INTO open_interest (venue, symbol, period, time_ms, open_interest, open_interest_value,"
                " ingested_at_ms) VALUES (?, ?, '5m', ?, '1', '2', ?)",
                ((VENUE, symbol_name(i), BASE_MS + k * TF_MS, BASE_MS) for k in range(ROWS)),
            )
            if i % 60 == 0:  # a few series with a conflict in the last 24 hours
                conn.execute(
                    "INSERT INTO open_interest_conflicts (venue, symbol, period, time_ms, open_interest,"
                    " open_interest_value, first_seen_at_ms) VALUES (?, ?, '5m', ?, '3', '4', ?)",
                    (VENUE, symbol_name(i), BASE_MS + (ROWS - 1) * TF_MS, BASE_MS),
                )
        conn.execute("COMMIT")
    finally:
        conn.close()


def handler_ms(path: Path, open_interest: OpenInterestRule | None) -> list[float]:
    universe = tuple(Instrument(VENUE, symbol_name(i), "5m") for i in range(SERIES))
    now = BASE_MS + (ROWS + 2) * TF_MS
    app = create_app(path, rule=SAMPLE_RULE, universe=universe, open_interest=open_interest, now_ms=lambda: now)
    client = TestClient(app)
    assert client.get("/health/ingest").status_code == 200  # one unmeasured warm-up request
    times: list[float] = []
    for _ in range(REPEATS):
        start = perf_counter()
        response = client.get("/health/ingest")
        times.append((perf_counter() - start) * 1000)
        assert response.status_code == 200
    return times


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="quasar-bench-health-oi-"))
    try:
        db = work / "bench.db"
        build_database(db)
        off = handler_ms(db, None)
        on = handler_ms(db, RULE)
        body = TestClient(
            create_app(
                db, rule=SAMPLE_RULE, universe=(), open_interest=RULE, now_ms=lambda: BASE_MS + (ROWS + 2) * TF_MS
            )
        ).get("/health/ingest")
        result = {
            "candle_series": SERIES,
            "open_interest_series": SERIES,
            "rows_per_series": ROWS,
            "handler_ms_open_interest_off": {"median": round(statistics.median(off), 1), "max": round(max(off), 1)},
            "handler_ms_open_interest_on": {"median": round(statistics.median(on), 1), "max": round(max(on), 1)},
            "within_250ms": max(on) <= H2_TARGET_MS,
            "open_interest_status": body.json()["open_interest"]["status"],
            "conflicted_series": body.json()["open_interest"]["conflicted_series"],
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "note": f"Page cache not dropped; {ROWS} rows per series, not the 60-million-row candle target database.",
        }
        print(json.dumps(result, indent=2))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
