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
concurrency is measured only by the served part below. Run by hand: ``make bench-indicators``.

Profiles: ``s18`` is the S18 request (ten ``rsi`` items, read range 3016 slots); ``s19`` (default) is the worst case of
S19: ten items over all six indicators at the largest warm-ups, three-output ones included (read range 3516 slots).

Served part (``--concurrency LIST``, default ``1,2,4,8``; ``0`` skips it): for each level one ``quasar-api serve``
subprocess on the generated database (loopback only), ``K`` concurrent worst-case requests through ``httpx``
(``--repeat`` rounds) while a probe calls ``GET /health`` every 10 ms. The server is stopped with SIGINT (SIGKILL after
5 s) and reaped with ``os.wait4`` on its own PID, which returns that one child's resource usage: ``RUSAGE_CHILDREN``
would report the largest peak of all children waited for so far, so it cannot give one server's peak. Every run
measures the final implementation (J7 included); there is no switch to turn it off.
"""

import argparse
import asyncio
import json
import os
import platform
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from socket import AF_INET, SOCK_STREAM, socket
from time import perf_counter
from typing import Any

import httpx
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
from tests.indicator_data import generated_bars

TF_MS = 300_000
BASE_MS = 1_700_000_100_000  # aligned to 5m
ROWS_PER_SERIES = 4000
WINDOW_SLOTS = 2016
VENUE = "binance-usdm"
PROFILES = {
    "s18": ";".join(f"rsi:period={period}" for period in range(91, 101)),
    "s19": (
        "rsi:period=100;ema:period=200;macd:fast=100,slow=200,signal=50;atr:period=100;adx:period=100;"
        "bbands:period=200,stddev=5;ema:period=2;macd:fast=12,slow=26,signal=9;adx:period=14;"
        "bbands:period=20,stddev=2"
    ),
}
PHASES = ("read", "compute", "serialize", "request")
START_TIMEOUT_S = 30
STOP_TIMEOUT_S = 5
PROBE_INTERVAL_S = 0.01
SERVED_NOTE = (
    "Per concurrency level one server process was started, used and reaped with os.wait4 on its PID. "
    "server_peak_rss_bytes is the peak resident set over that server's whole life (start-up, the /health readiness "
    "polls, one warm-up request and all requests of the level), not the memory of one request; "
    "server_cpu_user_s and server_cpu_system_s likewise cover its whole life including start-up; a child process "
    "spawned by the server would not be counted (uvicorn as started here runs one process). The server uses the "
    "real clock and the requests carry an explicit end in the generated past. One machine, warm page cache, "
    "no pass/fail criterion; more than 8 concurrent requests and a cold disk are not measured."
)


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
            bars = generated_bars(ROWS_PER_SERIES, seed=i)
            conn.executemany(
                "INSERT INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
                " turnover, ingested_at_ms) VALUES (?, ?, '5m', ?, ?, ?, ?, ?, '1', NULL, ?)",
                (
                    (VENUE, symbol_name(i), BASE_MS + k * TF_MS, open_, high, low, close, BASE_MS)
                    for k, (open_, high, low, close) in enumerate(bars)
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


def free_port() -> int:
    with socket(AF_INET, SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _peak_rss_bytes(ru_maxrss: int) -> int:
    """``ru_maxrss`` is bytes on macOS and KiB on Linux."""
    return ru_maxrss if sys.platform == "darwin" else ru_maxrss * 1024


def _reap(proc: "subprocess.Popen[bytes]") -> tuple[str, Any]:
    """Stop the server (SIGINT, SIGKILL after 5 s) and reap that one child with ``os.wait4``."""
    stopped = "sigint"
    proc.send_signal(signal.SIGINT)
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while True:
        pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            break
        if time.monotonic() >= deadline:
            stopped = "sigkill"
            proc.kill()
            pid, status, usage = os.wait4(proc.pid, 0)
            break
        time.sleep(0.02)
    proc.returncode = os.waitstatus_to_exitcode(status)  # Popen must not wait for the reaped child again
    return stopped, usage


def _wait_until_up(proc: "subprocess.Popen[bytes]", port: int) -> None:
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise BenchError(f"quasar-api exited with {proc.returncode} during startup")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2, trust_env=False).status_code == 200:
                return
        except httpx.TransportError:
            time.sleep(0.1)
    raise BenchError(f"quasar-api did not answer /health within {START_TIMEOUT_S} s")


async def _load(port: int, params: dict[str, str], level: int, repeat: int) -> tuple[list[float], list[float], int]:
    latencies: list[float] = []
    health: list[float] = []
    sizes: set[int] = set()
    base = f"http://127.0.0.1:{port}"
    async with (
        httpx.AsyncClient(base_url=base, timeout=120, trust_env=False) as client,
        httpx.AsyncClient(base_url=base, timeout=120, trust_env=False) as probe_client,
    ):

        async def one() -> None:
            t0 = perf_counter()
            response = await client.get("/v1/indicators", params=params)
            latencies.append((perf_counter() - t0) * 1000)
            if response.status_code != 200:
                raise BenchError(f"/v1/indicators answered {response.status_code}")
            sizes.add(len(response.content))

        async def probe() -> None:
            while True:
                t0 = perf_counter()
                response = await probe_client.get("/health")
                health.append((perf_counter() - t0) * 1000)
                if response.status_code != 200:
                    raise BenchError(f"/health answered {response.status_code}")
                await asyncio.sleep(PROBE_INTERVAL_S)

        await one()  # warm-up request, not measured
        latencies.clear()
        probing = asyncio.create_task(probe())
        try:
            for _ in range(repeat):
                await asyncio.gather(*(one() for _ in range(level)))
        finally:
            probing.cancel()
            try:
                await probing
            except asyncio.CancelledError:
                pass
    if len(sizes) != 1:
        raise BenchError(f"the concurrent responses differ in size: {sorted(sizes)}")
    return latencies, health, sizes.pop()


def serve_level(db: Path, params: dict[str, str], level: int, repeat: int, log: Path) -> dict[str, Any]:
    script = Path(sys.executable).parent / "quasar-api"
    if not script.exists():
        raise BenchError(f"the console script {script} is not installed in this environment")
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    with log.open("wb") as log_file:
        proc = subprocess.Popen(
            [str(script), "serve", "--db", str(db), "--host", "127.0.0.1", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            env=env,
        )
    try:
        _wait_until_up(proc, port)
        latencies, health, body_bytes = asyncio.run(_load(port, params, level, repeat))
    except BaseException:
        if proc.poll() is None:
            _reap(proc)
        raise
    stopped, usage = _reap(proc)
    return {
        "requests": len(latencies),
        "latency_ms": _summary(latencies),
        "body_bytes": body_bytes,
        "health_probes": len(health),
        "health_ms": _summary(health),
        "health_over_100ms": sum(1 for value in health if value > 100),
        "server_cpu_user_s": round(usage.ru_utime, 2),
        "server_cpu_system_s": round(usage.ru_stime, 2),
        "server_peak_rss_bytes": _peak_rss_bytes(usage.ru_maxrss),
        "stopped": stopped,
    }


def run(
    work_dir: Path, *, repeat: int, series: int, profile: str = "s19", concurrency: tuple[int, ...] = (1, 2, 4, 8)
) -> dict[str, Any]:
    if repeat < 1 or series < 1:
        raise BenchError("--repeat and --series must be at least 1")
    if profile not in PROFILES:
        raise BenchError(f"--profile must be one of {sorted(PROFILES)}")
    if any(level < 0 for level in concurrency):
        raise BenchError("--concurrency levels must not be negative")
    indicators = PROFILES[profile]
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
        query = f"venue={VENUE}&symbol={symbol_name(0)}&timeframe=5m&start={start}&end={end}&indicators={indicators}"
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

        served: dict[str, Any] = {}
        for level in sorted({level for level in concurrency if level > 0}):
            params = {
                "venue": VENUE,
                "symbol": symbol_name(0),
                "timeframe": "5m",
                "start": str(start),
                "end": str(end),
                "indicators": indicators,
            }
            served[str(level)] = serve_level(db, params, level, repeat, work_dir / f"api-{level}.log")

        return {
            "profile": profile,
            "series": series,
            "rows_per_series": ROWS_PER_SERIES,
            "window_slots": WINDOW_SLOTS,
            "read_range_slots": (end - read_start) // TF_MS,
            "indicators": indicators,
            "repeat": repeat,
            "build_s": round(build_s, 1),
            "phases_ms": {phase: _summary(times[phase]) for phase in PHASES},
            "body_bytes": body_bytes,
            "environment": environment(),
            "served": served,
            "served_note": SERVED_NOTE,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: list[str] | None = None, run_bench: Callable[..., dict[str, Any]] = run) -> int:
    parser = argparse.ArgumentParser(prog="bench_indicators")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--series", type=int, default=50)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="s19")
    parser.add_argument("--concurrency", default="1,2,4,8", help="comma-separated levels; 0 skips the served part")
    args = parser.parse_args(argv)
    try:
        levels = tuple(int(item) for item in args.concurrency.split(","))
    except ValueError:
        parser.error("--concurrency must be a comma-separated list of integers")
    work_dir = Path(tempfile.mkdtemp(prefix="quasar-bench-indicators-")) / "work"
    try:
        result = run_bench(work_dir, repeat=args.repeat, series=args.series, profile=args.profile, concurrency=levels)
    except BenchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(work_dir.parent, ignore_errors=True)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
