"""Informative benchmark of the largest ``GET /v1/indicators`` request (S18 Task 5 step 4). Not part of ``make check``.

Builds a generated database in a fresh temporary directory (never an existing path, never production data, no
network): ``--series`` series of 4000 present 5m candles with closes from ``generated_closes``. Against one series
it times the largest request: a window of 2016 slots with ``rsi:period=91`` to ``rsi:period=100`` (read range 3016
slots). Each repeat times four phases with ``time.perf_counter``:

(a) ``read``: ``read_indicators`` (the database read in one read transaction);
(b) ``compute``: ``indicator_results`` (slot states, segments and TA-Lib);
(c) ``serialize``: ``_indicators_body`` plus ``json.dumps`` exactly as ``_json`` encodes;
(d) ``request``: the whole request in process through ``TestClient`` with a fixed clock.

It prints one JSON object: min, median and max per phase in milliseconds, the body size in bytes and the
environment. There is no acceptance criterion. The page cache is not dropped: this is not a cold-disk figure, and
concurrency is not measured. Run by hand: ``make bench-indicators``.
"""

import argparse
import json
import platform
import shutil
import sqlite3
import statistics
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy
import talib
from starlette.testclient import TestClient

from quasar_market_data.api.app import _indicators_body, create_app
from quasar_market_data.api.database import read_indicators
from quasar_market_data.api.indicator_results import indicator_results
from quasar_market_data.api.params import parse_indicators_request
from quasar_market_data.api.rule import DEFAULT_RULE
from quasar_market_data.domain.indicator_series import read_start_ms
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate
from tests.indicator_data import generated_closes

TF_MS = 300_000
BASE_MS = 1_700_000_100_000  # aligned to 5m
ROWS_PER_SERIES = 4000
WINDOW_SLOTS = 2016
VENUE = "binance-usdm"
INDICATORS = ";".join(f"rsi:period={period}" for period in range(91, 101))
PHASES = ("read", "compute", "serialize", "request")


class BenchError(Exception):
    pass


def symbol_name(index: int) -> str:
    return f"SYM{index:04d}/USDT:USDT"


def build_database(path: Path, series: int) -> None:
    conn = open_writer(path)
    try:
        migrate(conn, applied_at_ms=BASE_MS)
        conn.execute("BEGIN")
        for i in range(series):
            closes = generated_closes(ROWS_PER_SERIES, seed=i)
            conn.executemany(
                "INSERT INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
                " turnover, ingested_at_ms) VALUES (?, ?, '5m', ?, ?, ?, ?, ?, '1', NULL, ?)",
                (
                    (VENUE, symbol_name(i), BASE_MS + k * TF_MS, close, close, close, close, BASE_MS)
                    for k, close in enumerate(closes)
                ),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _summary(times: list[float]) -> dict[str, float]:
    return {
        "min": round(min(times), 2),
        "median": round(statistics.median(times), 2),
        "max": round(max(times), 2),
    }


def _text(value: object) -> str:
    return value.decode("ascii", errors="replace") if isinstance(value, bytes) else str(value)


def environment() -> dict[str, str]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "talib": talib.__version__,
        "ta_lib_c": _text(vars(talib)["__ta_version__"]),  # not in talib's type stubs; bytes in 0.8.2
        "numpy": numpy.__version__,
        "note": "page cache not dropped",
    }


def run(work_dir: Path, *, repeat: int, series: int) -> dict[str, Any]:
    if repeat < 1 or series < 1:
        raise BenchError("--repeat and --series must be at least 1")
    if work_dir.exists():
        raise BenchError(f"{work_dir} already exists; the benchmark builds in a fresh directory")
    work_dir.mkdir(parents=True)
    try:
        db = work_dir / "bench.db"
        build_start = perf_counter()
        build_database(db, series)
        build_s = perf_counter() - build_start

        end = BASE_MS + ROWS_PER_SERIES * TF_MS
        start = end - WINDOW_SLOTS * TF_MS
        as_of_ms = end + DEFAULT_RULE.publication_delay_ms  # the window's last slot is the newest expected one
        rule = DEFAULT_RULE
        query = f"venue={VENUE}&symbol={symbol_name(0)}&timeframe=5m&start={start}&end={end}&indicators={INDICATORS}"
        request = parse_indicators_request(query.encode("ascii"))
        read_start = min(read_start_ms(start, spec.warmup_slots, TF_MS) for spec in request.indicators)
        client = TestClient(create_app(db, now_ms=lambda: as_of_ms, rule=rule))

        times: dict[str, list[float]] = {phase: [] for phase in PHASES}
        body_bytes = 0
        for _ in range(repeat):
            t0 = perf_counter()
            inputs = read_indicators(db, request, end=end, read_start=read_start)
            t1 = perf_counter()
            series_results = indicator_results(inputs, request.indicators, as_of_ms=as_of_ms, rule=rule)
            t2 = perf_counter()
            body = _indicators_body(inputs, request.indicators, series_results, as_of_ms, rule)
            content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            t3 = perf_counter()
            response = client.get(f"/v1/indicators?{query}")
            t4 = perf_counter()
            if response.status_code != 200:
                raise BenchError(f"/v1/indicators answered {response.status_code}")
            if response.content != content:
                raise BenchError("the in-process body differs from the served body")
            for phase, elapsed in zip(PHASES, (t1 - t0, t2 - t1, t3 - t2, t4 - t3), strict=True):
                times[phase].append(elapsed * 1000)
            body_bytes = len(content)

        return {
            "series": series,
            "rows_per_series": ROWS_PER_SERIES,
            "window_slots": WINDOW_SLOTS,
            "read_range_slots": (end - read_start) // TF_MS,
            "indicators": INDICATORS,
            "repeat": repeat,
            "build_s": round(build_s, 1),
            "phases_ms": {phase: _summary(times[phase]) for phase in PHASES},
            "body_bytes": body_bytes,
            "environment": environment(),
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: list[str] | None = None, run_bench: Callable[..., dict[str, Any]] = run) -> int:
    parser = argparse.ArgumentParser(prog="bench_indicators")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--series", type=int, default=50)
    args = parser.parse_args(argv)
    work_dir = Path(tempfile.mkdtemp(prefix="quasar-bench-indicators-")) / "work"
    try:
        result = run_bench(work_dir, repeat=args.repeat, series=args.series)
    except BenchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(work_dir.parent, ignore_errors=True)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
