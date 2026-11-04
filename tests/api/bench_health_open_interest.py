"""Cost of the ``open_interest`` and ``funding_rate`` objects of ``/health/ingest`` (OI-5 and FR-5 plans). Not part of ``make check``.

Builds a synthetic database in a fresh temporary directory (never production data, no network) with ``SERIES``
candle series and ``SERIES`` open interest series (plus a few conflicts) of ``ROWS`` rows each and ``SERIES`` funding
series (a verified span, ``FUNDING_ROWS`` rows and a few conflicts each), and times the whole ``GET /health/ingest``
handler in process with open interest off and on, and with open interest and funding on. The cost is a seek per series, not per row
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
from quasar_market_data.domain.funding_rate_coverage import FundingRateRule
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
FUNDING_ROWS = 100
REPEATS = 9
H2_TARGET_MS = 250
FUNDING_RULE = FundingRateRule(publication_delay_ms=30_000, backfill_days=365)
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
            conn.executemany(
                "INSERT INTO funding_rates (venue, symbol, funding_time_ms, rate_type, funding_rate, mark_price,"
                " ingested_at_ms) VALUES (?, ?, ?, 'Regular', '0.0001', '1', ?)",
                ((VENUE, symbol_name(i), BASE_MS + k * 28_800_000, BASE_MS) for k in range(FUNDING_ROWS)),
            )
            conn.execute(
                "INSERT INTO funding_rate_verified (venue, symbol, from_ms, through_ms, verified_at_ms)"
                " VALUES (?, ?, ?, ?, ?)",
                (VENUE, symbol_name(i), BASE_MS, BASE_MS + FUNDING_ROWS * 28_800_000, BASE_MS),
            )
            if i % 60 == 0:
                conn.execute(
                    "INSERT INTO funding_rate_conflicts (venue, symbol, funding_time_ms, rate_type, funding_rate,"
                    " mark_price, first_seen_at_ms) VALUES (?, ?, ?, 'Regular', '0.0002', '2', ?)",
                    (VENUE, symbol_name(i), BASE_MS + (FUNDING_ROWS - 1) * 28_800_000, BASE_MS),
                )
        conn.execute("COMMIT")
    finally:
        conn.close()


def handler_ms(
    path: Path, open_interest: OpenInterestRule | None, funding_rate: FundingRateRule | None = None
) -> list[float]:
    universe = tuple(Instrument(VENUE, symbol_name(i), "5m") for i in range(SERIES))
    now = BASE_MS + (ROWS + 2) * TF_MS
    app = create_app(
        path,
        rule=SAMPLE_RULE,
        universe=universe,
        open_interest=open_interest,
        funding_rate=funding_rate,
        now_ms=lambda: now,
    )
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
        both = handler_ms(db, RULE, FUNDING_RULE)
        body = TestClient(
            create_app(
                db,
                rule=SAMPLE_RULE,
                universe=(),
                open_interest=RULE,
                funding_rate=FUNDING_RULE,
                now_ms=lambda: BASE_MS + (ROWS + 2) * TF_MS,
            )
        ).get("/health/ingest")
        result = {
            "candle_series": SERIES,
            "open_interest_series": SERIES,
            "rows_per_series": ROWS,
            "handler_ms_open_interest_off": {"median": round(statistics.median(off), 1), "max": round(max(off), 1)},
            "handler_ms_open_interest_on": {"median": round(statistics.median(on), 1), "max": round(max(on), 1)},
            "handler_ms_open_interest_and_funding_on": {
                "median": round(statistics.median(both), 1),
                "max": round(max(both), 1),
            },
            "within_250ms": max(on) <= H2_TARGET_MS,
            "within_250ms_with_funding": max(both) <= H2_TARGET_MS,
            "open_interest_status": body.json()["open_interest"]["status"],
            "conflicted_series": body.json()["open_interest"]["conflicted_series"],
            "funding_series": body.json()["funding_rate"]["series_count"],
            "funding_status": body.json()["funding_rate"]["status"],
            "funding_conflicted_series": body.json()["funding_rate"]["conflicted_series"],
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
