"""Reproducible benchmark of the ``/health/ingest`` read (S17, Decision 11). Not part of the default suite except ``smoke``.

Builds a synthetic database in a fresh temporary directory (never an existing path, never production data, no
network), reads the stored series with ``read_stored_series`` (H1, H2), then reads it again while a writer connection
commits in the middle of each read (H3), and prints one JSON object with the criteria H1 to H3 of the S17 plan.

H1: steps per series on the profile are within 2x of the same count on a tiny database (rows must not matter).
H2: wall time of one snapshot and of the whole handler (the target profile has 600 series x 100 000 rows; the plan proposes at most 250 ms).
H3: a writer that commits in the middle of a read (a new series sorting after the cursor, newer candles in a series
already read and in one not yet read) neither fails the read nor changes its result: the read equals the whole
``StoredSeries`` tuple taken just before the commit, ``newest_open_time_ms`` included.

The operating-system cache is not dropped: this is not a cold-disk figure. Run by hand:
``make bench-health PROFILE=medium`` and ``make bench-health PROFILE=target`` (the target database has 60 million rows).
"""

import argparse
import json
import platform
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Any

from starlette.testclient import TestClient

from quasar_market_data.api.app import create_app
from quasar_market_data.api.health_reader import StoredSeries, read_stored_series
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate

BASE_MS = 1_700_000_100_000
TF_MS = 300_000
VENUE = "bench"
PROFILES: dict[str, tuple[int, int]] = {"smoke": (20, 300), "medium": (100, 100_000), "target": (600, 100_000)}
BASELINE = (20, 3)  # series, rows per series: the tiny database H1 compares with
READS = 5
H1_FACTOR = 2
H2_TARGET_MS = 250


class BenchError(Exception):
    pass


def symbol_name(index: int) -> str:
    return f"SYM{index:04d}/USDT:USDT"


def build_database(path: Path, series: int, rows_per_series: int) -> int:
    conn = open_writer(path)
    try:
        migrate(conn, applied_at_ms=BASE_MS)
        conn.execute("BEGIN")
        for i in range(series):
            conn.executemany(
                "INSERT INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
                " turnover, ingested_at_ms) VALUES (?, ?, '5m', ?, '1', '2', '1', '1', '1', NULL, ?)",
                ((VENUE, symbol_name(i), BASE_MS + k * TF_MS, BASE_MS) for k in range(rows_per_series)),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return series * rows_per_series


def steps_per_series(path: Path, series: int) -> float:
    conn = open_reader(path)
    count = 0

    def tick() -> int:
        nonlocal count
        count += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        read_stored_series(conn)
    finally:
        conn.set_progress_handler(None, 1)
        conn.close()
    return count / series


def _timed_reads(path: Path, reads: int) -> tuple[list[float], list[tuple[StoredSeries, ...]]]:
    times: list[float] = []
    results: list[tuple[StoredSeries, ...]] = []
    for _ in range(reads):
        conn = open_reader(path)
        try:
            start = perf_counter()
            results.append(read_stored_series(conn))
            times.append((perf_counter() - start) * 1000)
        finally:
            conn.close()
    return times, results


def _handler_ms(path: Path, reads: int) -> list[float]:
    """The whole ``GET /health/ingest`` handler in process (routing, thread pool, read, freshness, JSON)."""
    client = TestClient(create_app(path, now_ms=lambda: BASE_MS + 10 * TF_MS))
    times: list[float] = []
    for _ in range(reads):
        start = perf_counter()
        response = client.get("/health/ingest")
        times.append((perf_counter() - start) * 1000)
        if response.status_code != 200:
            raise BenchError(f"/health/ingest answered {response.status_code}")
    return times


class _CommitDuringRead:
    """A connection proxy: after the ``after``-th next-series read, a second writer connection commits."""

    def __init__(self, real: sqlite3.Connection, commit: Callable[[], None], after: int) -> None:
        self._real = real
        self._commit = commit
        self._after = after
        self._seen = 0
        self.commits = 0

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        cursor = self._real.execute(sql, params)
        if "ORDER BY venue" in sql:
            self._seen += 1
            if self._seen == self._after:
                self._commit()
                self.commits += 1
        return cursor


def _reads_with_commits(path: Path, series: int, rows: int, reads: int) -> tuple[int, int, list[float]]:
    """(commits made during reads, reads whose result differed from the pre-read snapshot, times in ms)."""
    writer = open_writer(path)
    next_time = BASE_MS + rows * TF_MS
    counter = 0

    def commit() -> None:
        nonlocal next_time, counter
        counter += 1
        next_time += TF_MS
        rows_to_add = [
            (f"ZZZ{counter:06d}", BASE_MS),  # a new series that sorts after every cursor position
            (symbol_name(0), next_time),  # a newer candle in a series that was already read
            (symbol_name(series - 1), next_time),  # a newer candle in a series that is read later
        ]
        writer.execute("BEGIN")
        writer.executemany(
            "INSERT INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover,"
            " ingested_at_ms) VALUES (?, ?, '5m', ?, '1', '2', '1', '1', '1', NULL, ?)",
            [(VENUE, symbol, open_time, BASE_MS) for symbol, open_time in rows_to_add],
        )
        writer.execute("COMMIT")

    commits = differing = 0
    times: list[float] = []
    try:
        for _ in range(reads):
            ref_conn = open_reader(path)
            try:
                reference = read_stored_series(ref_conn)
            finally:
                ref_conn.close()
            conn = open_reader(path)
            proxy = _CommitDuringRead(conn, commit, after=max(1, series // 2))
            try:
                start = perf_counter()
                result = read_stored_series(proxy)  # type: ignore[arg-type]
                times.append((perf_counter() - start) * 1000)
            finally:
                conn.close()
            commits += proxy.commits
            differing += result != reference
    finally:
        writer.close()
    return commits, differing, times


def run(profile: str, work_dir: Path, *, keep: bool) -> dict[str, Any]:
    if profile not in PROFILES:
        raise BenchError(f"unknown profile {profile!r}; expected one of {sorted(PROFILES)}")
    if work_dir.exists():
        raise BenchError(f"{work_dir} already exists; the benchmark builds in a fresh directory")
    work_dir.mkdir(parents=True)
    try:
        series, rows = PROFILES[profile]
        db = work_dir / "bench.db"
        tiny = work_dir / "tiny.db"
        build_start = perf_counter()
        total_rows = build_database(db, series, rows)
        build_database(tiny, *BASELINE)
        build_s = perf_counter() - build_start

        steps = steps_per_series(db, series)
        baseline_steps = steps_per_series(tiny, BASELINE[0])
        times, _quiet = _timed_reads(db, READS)
        handler = _handler_ms(db, READS)

        commits, differing, busy_times = _reads_with_commits(db, series, rows, READS)
        h3 = commits == READS and differing == 0

        return {
            "profile": profile,
            "series": series,
            "rows_per_series": rows,
            "rows_total": total_rows,
            "build_s": round(build_s, 1),
            "steps_per_series": round(steps, 1),
            "baseline_steps_per_series": round(baseline_steps, 1),
            "snapshot_ms_quiet": [round(t, 1) for t in times],
            "snapshot_ms_with_writer": [round(t, 1) for t in busy_times],
            "H1_steps_within_2x": steps <= H1_FACTOR * baseline_steps,
            "handler_ms_quiet": [round(t, 1) for t in handler],
            "H2_snapshot_ms_max": round(max(times + busy_times), 1),
            "H2_within_250ms": max(times + busy_times + handler) <= H2_TARGET_MS,
            "H3_writer_does_not_break_or_change_reads": h3,
            "H3_commits_during_reads": commits,
            "H3_reads_that_differed_from_their_snapshot": differing,
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "note": "Page cache not dropped: not a cold-disk figure. The criteria are proposals for the user (HARD STOP 2).",
        }
    finally:
        if not keep:
            shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: list[str] | None = None, run_profile: Callable[..., dict[str, Any]] = run) -> int:
    parser = argparse.ArgumentParser(prog="bench_health")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="smoke")
    parser.add_argument("--keep", action="store_true", help="keep the generated database")
    args = parser.parse_args(argv)
    work_dir = Path(tempfile.mkdtemp(prefix="quasar-bench-health-")) / "work"
    try:
        result = run_profile(args.profile, work_dir, keep=args.keep)
    except BenchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        if not args.keep:
            shutil.rmtree(work_dir.parent, ignore_errors=True)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
