"""Reproducible benchmark of the restart reconcile read (S15, D13). Not part of the default suite except ``smoke``.

Builds a synthetic database in a fresh temporary directory (never an existing path, never production data, no
network), runs the window-only pre-start read once (``pre_start_ms``, what the start waits for) and the full read with
history twice (``first_run_ms``, ``second_run_ms``: the background post-check) behind a statement-timing connection
proxy, and prints one JSON object.

Neither run is a cold-disk figure: the operating-system cache is not dropped, and the second run is typically warmer.
Run by hand: ``make bench PROFILE=medium`` and ``make bench PROFILE=target``.
"""

import argparse
import json
import platform
import sqlite3
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import patch

from quasar_market_data.collector import reconcile_reader
from quasar_market_data.collector.reconcile_reader import (
    ReadGuard,
    ReconcileInputs,
    expected_target,
    reconcile_blocking,
)
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.statement_proxy import StatementProxy

BASE_MS = 1_700_000_100_000
TF = Timeframe("5m")
TF_MS = TF.milliseconds
VENUE = "bench"
HOLE_SLOTS = 5
YEAR_ROWS = 105_120  # one year of 5m candles
TARGET_SERIES = 600
TARGET_YEAR_ROWS = 3 * YEAR_ROWS
PROFILES: dict[str, tuple[int, int]] = {"smoke": (3, 2_000), "medium": (50, YEAR_ROWS), "target": (600, YEAR_ROWS)}
SCALING_SERIES = 50
SCALING_YEARS = (1, 2, 3)
NOTE = (
    "The operating-system cache is not dropped: neither run is a cold-disk figure. Statement timings are from the"
    " first run. A projection is total_ms x (600 x 315360) / rows, a linear extrapolation, not a measurement."
)

_SQL_NAMES = {
    reconcile_reader.BOUNDS_SQL: "bounds",
    reconcile_reader.CANDLES_SQL: "candles",
    reconcile_reader.CONFLICTS_SQL: "conflicts",
    reconcile_reader.UNAVAILABLE_SQL: "unavailable",
    reconcile_reader.HOLES_SQL: "holes",
    reconcile_reader.UNAVAILABLE_IN_HOLES_SQL: "unavailable_in_holes",
    reconcile_reader.CONFLICTED_SQL: "conflicted",
}
_PAIRS_COUNT_SQL = (
    "SELECT COUNT(*), COUNT(prev) FROM (SELECT open_time_ms, LAG(open_time_ms) OVER (ORDER BY open_time_ms) AS prev"
    " FROM candles WHERE venue = ? AND symbol = ? AND timeframe = ?)"
)


class BenchError(Exception):
    pass


@dataclass(frozen=True)
class BuildStats:
    rows_total: int
    holes: int
    unavailable: int
    conflicts: int
    span_slots_per_symbol: int


def symbol_name(index: int) -> str:
    return f"SYM{index:04d}/USDT:USDT"


def _hole_rows(rows: int, holes: int) -> list[int]:
    """Row indexes before which a hole of ``HOLE_SLOTS`` slots opens (fixed fractions of the series)."""
    return [rows * (k + 1) // (holes + 1) for k in range(holes)]


def _slot_of(row: int, hole_rows: list[int]) -> int:
    return row + HOLE_SLOTS * sum(1 for h in hole_rows if h <= row)


def _candle_rows(symbol: str, rows: int, hole_rows: list[int]) -> Iterator[tuple[object, ...]]:
    for row in range(rows):
        open_time = BASE_MS + _slot_of(row, hole_rows) * TF_MS
        yield (VENUE, symbol, TF.label, open_time, "1", "1", "1", "1", "1", "1", BASE_MS)


def build_database(
    path: Path,
    *,
    symbols: int,
    rows_per_symbol: int,
    holes_per_symbol: int = 3,
    unavailable_per_symbol: int = 10,
    conflicts_per_symbol: int = 2,
) -> BuildStats:
    """Deterministic synthetic database: ``rows_per_symbol`` stored candles per series, holes at fixed fractions."""
    hole_rows = _hole_rows(rows_per_symbol, holes_per_symbol)
    hole_slots = [_slot_of(h, hole_rows) - HOLE_SLOTS + offset for h in hole_rows for offset in range(HOLE_SLOTS)]
    unavailable_slots = hole_slots[:unavailable_per_symbol]
    conflict_rows = min(conflicts_per_symbol, rows_per_symbol)
    conn = open_writer(path)
    try:
        migrate(conn, applied_at_ms=BASE_MS)
        conn.execute("PRAGMA synchronous=OFF")  # the benchmark database is disposable
        for index in range(symbols):
            symbol = symbol_name(index)
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _candle_rows(symbol, rows_per_symbol, hole_rows),
            )
            conn.executemany(
                "INSERT INTO unavailable_candles VALUES (?, ?, ?, ?, 'EXCHANGE_NO_DATA', ?)",
                [(VENUE, symbol, TF.label, BASE_MS + s * TF_MS, BASE_MS) for s in unavailable_slots],
            )
            conn.executemany(
                "INSERT INTO candle_conflicts (venue, symbol, timeframe, open_time_ms, open, high, low, close,"
                " volume, turnover, first_seen_at_ms) VALUES (?, ?, ?, ?, '1', '1', '1', '1', '2', '1', ?)",
                [
                    (VENUE, symbol, TF.label, BASE_MS + _slot_of(r, hole_rows) * TF_MS, BASE_MS)
                    for r in range(conflict_rows)
                ],
            )
            conn.execute("COMMIT")
    finally:
        conn.close()
    return BuildStats(
        rows_total=symbols * rows_per_symbol,
        holes=symbols * holes_per_symbol,
        unavailable=symbols * len(unavailable_slots),
        conflicts=symbols * conflict_rows,
        span_slots_per_symbol=rows_per_symbol + holes_per_symbol * HOLE_SLOTS,
    )


def _timed_run(
    db: Path, inputs: ReconcileInputs, as_of_ms: int, *, history: bool = True
) -> tuple[float, list[StatementProxy], int]:
    proxies: list[StatementProxy] = []
    real_open = open_reader

    def opener(path: Path) -> StatementProxy:
        proxy = StatementProxy(real_open(path))
        proxies.append(proxy)
        return proxy

    with patch("quasar_market_data.collector.reconcile_reader.open_reader", opener):
        start = perf_counter()
        result = reconcile_blocking(
            db,
            inputs,
            as_of_ms=as_of_ms,
            target_open_ms=expected_target(inputs, as_of_ms),
            history=history,
            guard=ReadGuard() if history else None,  # as the CLI runs them: only the post-check has a guard
        )
        elapsed_ms = (perf_counter() - start) * 1000
    unaccounted = sum(series.history_unaccounted_holes or 0 for series in result.values())  # a full read: never None
    return elapsed_ms, proxies, unaccounted


def _statement_report(db: Path, proxy: StatementProxy) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    params_by_name: dict[str, Any] = {}
    sql_by_name: dict[str, str] = {}
    for statement in proxy.statements:
        name = _SQL_NAMES.get(statement.sql, "other")
        entry = report.setdefault(name, {"count": 0, "total_ms": 0.0, "rows": 0, "plan": []})
        entry["count"] += 1
        entry["total_ms"] += statement.seconds * 1000
        entry["rows"] += statement.rows
        if name != "other" and name not in params_by_name:
            params_by_name[name] = statement.params
            sql_by_name[name] = statement.sql
    raw = open_reader(db)
    try:
        for name, sql in sql_by_name.items():
            plan = raw.execute("EXPLAIN QUERY PLAN " + sql, params_by_name[name]).fetchall()
            report[name]["plan"] = [row[3] for row in plan]
    finally:
        raw.close()
    return report


def _count_rows(db: Path, symbols: int) -> tuple[int, int]:
    conn = open_reader(db)
    try:
        rows = pairs = 0
        for index in range(symbols):
            count, with_prev = conn.execute(_PAIRS_COUNT_SQL, (VENUE, symbol_name(index), TF.label)).fetchone()
            rows += count
            pairs += with_prev
        return rows, pairs
    finally:
        conn.close()


def _db_bytes(db: Path) -> int:
    return sum(p.stat().st_size for p in db.parent.glob(db.name + "*") if p.is_file())


def _measure(profile: str, db: Path, symbols: int, rows_per_symbol: int) -> dict[str, Any]:
    started = perf_counter()
    stats = build_database(db, symbols=symbols, rows_per_symbol=rows_per_symbol)
    build_ms = (perf_counter() - started) * 1000
    inputs = ReconcileInputs(
        symbols=tuple(symbol_name(i) for i in range(symbols)),
        venue=VENUE,
        timeframe=TF,
        publication_delay_ms=10_000,
        lag_threshold_ms=60_000,
        default_limit=100,
        max_limit=1000,
    )
    as_of_ms = BASE_MS + stats.span_slots_per_symbol * TF_MS + 20_000
    pre_start_ms, _, _ = _timed_run(db, inputs, as_of_ms, history=False)  # first: the start's read, coldest cache
    first_ms, first_proxies, unaccounted = _timed_run(db, inputs, as_of_ms)
    second_ms, _, _ = _timed_run(db, inputs, as_of_ms)
    rows_visited, pairs_visited = _count_rows(db, symbols)
    return {
        "profile": profile,
        "symbols": symbols,
        "rows_per_symbol": rows_per_symbol,
        "rows_total": stats.rows_total,
        "rows_visited": rows_visited,
        "pairs_visited": pairs_visited,
        "holes_built": stats.holes,
        "unavailable_built": stats.unavailable,
        "conflicts_built": stats.conflicts,
        "history_unaccounted_holes": unaccounted,
        "db_bytes": _db_bytes(db),
        "build_ms": build_ms,
        "pre_start_ms": pre_start_ms,
        "total_ms": first_ms,
        "first_run_ms": first_ms,
        "second_run_ms": second_ms,
        "statements": _statement_report(db, first_proxies[0]),
        "sqlite_version": sqlite3.sqlite_version,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "note": NOTE,
    }


def _remove(db: Path) -> None:
    for path in db.parent.glob(db.name + "*"):
        path.unlink()


def _prepare(directory: Path | None) -> tuple[Path, bool]:
    """Return the directory and whether this call created it; refuse anything that already holds files."""
    if directory is None:
        return Path(tempfile.mkdtemp(prefix="quasar-bench-")), True
    if directory.exists():
        if not directory.is_dir() or any(directory.iterdir()):
            raise BenchError(f"{directory} exists and is not an empty directory; refusing to use it")
        return directory, False
    directory.mkdir(parents=True)
    return directory, True


def run(profile: str, directory: Path | None, keep: bool) -> dict[str, Any]:
    if profile != "scaling" and profile not in PROFILES:
        raise BenchError(f"unknown profile {profile!r}")
    root, created = _prepare(directory)
    dbs: list[Path] = []
    try:
        if profile == "scaling":
            steps = []
            for years in SCALING_YEARS:
                db = root / f"bench-{years}y.db"
                dbs.append(db)
                step = _measure(profile, db, SCALING_SERIES, years * YEAR_ROWS)
                step["years"] = years
                step["projection_to_600x3y_ms"] = (
                    step["total_ms"] * (TARGET_SERIES * TARGET_YEAR_ROWS) / step["rows_total"]
                )
                steps.append(step)
                if not keep:
                    _remove(db)
            result: dict[str, Any] = {
                "profile": profile,
                "steps": steps,
                "ms_per_million_rows": [s["total_ms"] / s["rows_total"] * 1e6 for s in steps],
                "note": NOTE + " Linear scaling shows as a constant ms_per_million_rows.",
            }
        else:
            db = root / "bench.db"
            dbs.append(db)
            symbols, rows = PROFILES[profile]
            result = _measure(profile, db, symbols, rows)
        result["directory"] = str(root)
        result["kept"] = keep
        return result
    finally:
        if not keep:
            for db in dbs:
                _remove(db)
            if created:
                root.rmdir()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the S15 restart reconcile read on a synthetic database.")
    parser.add_argument("--profile", required=True, choices=[*PROFILES, "scaling"])
    parser.add_argument("--dir", type=Path, default=None, help="a new or empty directory (default: a fresh mkdtemp)")
    parser.add_argument("--keep", action="store_true", help="keep the generated database")
    args = parser.parse_args(argv)
    try:
        result = run(args.profile, args.dir, args.keep)
    except BenchError as error:
        print(f"bench_reconcile: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
