"""Measure the 100-row open interest transaction on a disk (ADR-0003 P-9 acceptance criterion: p99 <= 50 ms).

Standard library only, so it runs where the project is not installed, for example on the VPS:

    docker run --rm -v <empty dir on the database filesystem>:/bench -v $PWD:/src:ro python:3.12-slim \\
        python /src/bench_open_interest_write.py --schema /src/002_open_interest.sql --dir /bench/run --max-p99-ms 50

It creates a fresh database in ``--dir`` (missing or empty; created if missing), applies ``--schema``
(``002_open_interest.sql``), and writes ``--transactions`` transactions of 100 new rows with the same INSERT
statement and connection settings as the Collector's open interest writer (WAL, ``synchronous=FULL``,
``recursive_triggers=ON``, ``busy_timeout`` 1000). It times each transaction from holding the write lock to the
end of ``COMMIT``, prints one line and removes its files unless ``--keep``. It never touches another database.

The database starts empty, so B-tree depth and checkpoint work are lower than in a database that has grown for
weeks; the number is a lower bound of the steady-state cost, not a proof.
"""

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

ROWS_PER_TRANSACTION: Final = 100
DB_NAME: Final = "oi-bench.db"
START_MS: Final = 1_700_000_100_000  # aligned to 5 minutes
PERIOD_MS: Final = 300_000

# Must equal ``collector.open_interest_writer.INSERT_OPEN_INTEREST`` (asserted by a test).
INSERT_OPEN_INTEREST: Final = (
    "INSERT INTO open_interest (venue, symbol, period, time_ms, open_interest, open_interest_value, ingested_at_ms)"
    " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
)


def nearest_rank(sorted_values: Sequence[int], percent: int) -> int:
    """Nearest-rank percentile of already sorted values (integer arithmetic)."""
    if not sorted_values:
        raise ValueError("no values")
    rank = max(1, -(-percent * len(sorted_values) // 100))
    return sorted_values[rank - 1]


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
        raise SystemExit("WAL journal mode is unavailable")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA recursive_triggers=ON")
    conn.execute("PRAGMA busy_timeout=1000")
    return conn


def run(schema: Path, directory: Path, transactions: int) -> list[int]:
    conn = _connect(directory / DB_NAME)
    try:
        conn.executescript(schema.read_text("utf-8"))
        durations: list[int] = []
        for tx in range(transactions):
            conn.execute("BEGIN IMMEDIATE")
            locked = time.perf_counter_ns()
            for i in range(ROWS_PER_TRANSACTION):
                time_ms = START_MS + (tx * ROWS_PER_TRANSACTION + i) * PERIOD_MS
                conn.execute(
                    INSERT_OPEN_INTEREST,
                    ("binance-usdm", "BTC/USDT:USDT", "5m", time_ms, "95677.779", "7446123150.675", START_MS),
                )
            conn.execute("COMMIT")
            durations.append(time.perf_counter_ns() - locked)
        return durations
    finally:
        conn.close()


def _ms(ns: int) -> str:
    return f"{ns / 1_000_000:.3f}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--schema", type=Path, required=True, help="path to 002_open_interest.sql")
    parser.add_argument("--dir", type=Path, default=None, help="missing or empty directory (default: a new temp dir)")
    parser.add_argument("--transactions", type=int, default=1_000)
    parser.add_argument("--max-p99-ms", type=float, default=None, help="exit 1 when p99 is above this")
    parser.add_argument("--keep", action="store_true", help="keep the database")
    args = parser.parse_args(argv)
    if args.transactions < 1:
        parser.error("--transactions must be >= 1")

    created = False
    if args.dir is None:
        directory = Path(tempfile.mkdtemp(prefix="oi-bench-"))
        created = True
    else:
        directory = args.dir
        if directory.exists():
            if not directory.is_dir() or any(directory.iterdir()):
                print(f"error: {directory} is not an empty directory; nothing was written", file=sys.stderr)
                return 2
        else:
            directory.mkdir(parents=True)
            created = True

    try:
        durations = sorted(run(args.schema, directory, args.transactions))
    finally:
        if not args.keep:
            for suffix in ("", "-wal", "-shm"):
                (directory / f"{DB_NAME}{suffix}").unlink(missing_ok=True)
            if created:
                shutil.rmtree(directory, ignore_errors=True)

    p99 = nearest_rank(durations, 99)
    print(
        f"oi_write rows_per_tx={ROWS_PER_TRANSACTION} transactions={len(durations)}"
        f" p50_ms={_ms(nearest_rank(durations, 50))} p99_ms={_ms(p99)} max_ms={_ms(durations[-1])}"
        f" dir={directory} sqlite={sqlite3.sqlite_version} date={datetime.now(UTC).date().isoformat()}",
        flush=True,
    )
    if args.max_p99_ms is not None and p99 > args.max_p99_ms * 1_000_000:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
