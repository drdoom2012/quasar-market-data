"""The stored facts of every configured series at start-up, read in one snapshot (S15, D4, D13).

Same discipline as ``api.coverage_reader`` (whose window selects are repeated here, not imported: the Collector and the
API stay independent): a read-only connection, ``BEGIN`` so that every symbol sees one snapshot, bound statements
consumed one at a time. Only open times are read, never values. Nothing here writes, fetches or reads the clock.
"""

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from quasar_market_data.domain.reconcile import (
    HistoryFacts,
    SeriesFacts,
    SeriesReconcile,
    reconcile_series,
    reconcile_window,
)
from quasar_market_data.domain.schedule import newest_expected_open_ms
from quasar_market_data.domain.timed_coverage import ExpectationRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import StorageError, open_reader
from quasar_market_data.storage.schema import verify_schema

_SERIES: Final = "venue = :venue AND symbol = :symbol AND timeframe = :timeframe"
_WINDOW: Final = f"{_SERIES} AND open_time_ms >= :start AND open_time_ms < :end"
_PAIRS: Final = (
    f"SELECT open_time_ms, LAG(open_time_ms) OVER (ORDER BY open_time_ms) AS prev FROM candles WHERE {_SERIES}"
)

# Two scalar subqueries: SQLite seeks the primary key for a lone MIN or MAX, but walks the series for both at once.
BOUNDS_SQL: Final = (
    f"SELECT (SELECT MIN(open_time_ms) FROM candles WHERE {_SERIES}),"
    f" (SELECT MAX(open_time_ms) FROM candles WHERE {_SERIES})"
)
CANDLES_SQL: Final = f"SELECT open_time_ms FROM candles WHERE {_WINDOW}"
CONFLICTS_SQL: Final = f"SELECT DISTINCT open_time_ms FROM candle_conflicts WHERE {_WINDOW}"
UNAVAILABLE_SQL: Final = f"SELECT open_time_ms FROM unavailable_candles WHERE {_WINDOW}"
# One walk of the series: the holes are materialized once, then aggregated and ranked. 1 to 5 rows of
# (raw, raw_old, runs, start_ms, end_ms); the first three are equal on every row, and a series without holes gives one
# row with NULL run columns. Runs are ordered longest first, then oldest first.
HOLES_SQL: Final = (
    f"WITH gaps AS MATERIALIZED (SELECT prev, open_time_ms FROM ({_PAIRS}) WHERE open_time_ms - prev > :tf)"
    " SELECT s.raw, s.raw_old, s.runs, top.start_ms, top.end_ms FROM"
    " (SELECT COALESCE(SUM((open_time_ms - prev) / :tf - 1), 0) AS raw,"
    " COALESCE(SUM(MAX(0, (MIN(open_time_ms, :ws) - prev) / :tf - 1)), 0) AS raw_old, COUNT(*) AS runs FROM gaps) AS s"
    " LEFT JOIN (SELECT prev + :tf AS start_ms, open_time_ms AS end_ms FROM gaps"
    " ORDER BY open_time_ms - prev DESC, prev ASC LIMIT 5) AS top ON 1"
    " ORDER BY top.end_ms - top.start_ms DESC, top.start_ms ASC"
)
UNAVAILABLE_IN_HOLES_SQL: Final = (
    "SELECT COUNT(*), COALESCE(SUM(u.open_time_ms < :ws), 0) FROM unavailable_candles AS u"
    " WHERE u.venue = :venue AND u.symbol = :symbol AND u.timeframe = :timeframe"
    " AND u.open_time_ms >= :oldest AND u.open_time_ms <= :newest"
    " AND NOT EXISTS (SELECT 1 FROM candles AS c WHERE c.venue = u.venue AND c.symbol = u.symbol"
    " AND c.timeframe = u.timeframe AND c.open_time_ms = u.open_time_ms)"
)
CONFLICTED_SQL: Final = f"SELECT COUNT(DISTINCT open_time_ms) FROM candle_conflicts WHERE {_SERIES}"


class ReconcileError(StorageError):
    """The restart reconcile could not read or classify the stored facts."""


@dataclass(frozen=True)
class ReconcileInputs:
    symbols: tuple[str, ...]
    venue: str
    timeframe: Timeframe
    publication_delay_ms: int
    default_limit: int
    max_limit: int


def expected_target(inputs: ReconcileInputs, as_of_ms: int) -> int | None:
    """The newest open time expected at ``as_of_ms``, or ``None`` when no slot is expected yet."""
    return newest_expected_open_ms(as_of_ms, inputs.timeframe, inputs.publication_delay_ms)


def reconcile_blocking(
    db: Path, inputs: ReconcileInputs, *, as_of_ms: int, target_open_ms: int | None
) -> dict[str, SeriesReconcile]:
    rule = ExpectationRule(publication_delay_ms=inputs.publication_delay_ms)
    tf = inputs.timeframe.milliseconds

    def classify(facts: SeriesFacts | None, history: HistoryFacts | None) -> SeriesReconcile:
        return reconcile_series(
            facts,
            history,
            as_of_ms=as_of_ms,
            target_open_ms=target_open_ms,
            timeframe_ms=tf,
            rule=rule,
            default_limit=inputs.default_limit,
            max_limit=inputs.max_limit,
        )

    if target_open_ms is None:
        return {symbol: classify(None, None) for symbol in inputs.symbols}

    window = reconcile_window(as_of_ms, target_open_ms, tf, inputs.max_limit)
    conn = open_reader(db)
    try:
        conn.execute("BEGIN")
        try:
            verify_schema(conn)
            return {symbol: _read_series(conn, inputs, symbol, window, tf, classify) for symbol in inputs.symbols}
        finally:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # a broken connection must not mask the original error; it is closed below
    finally:
        conn.close()


def _read_series(
    conn: sqlite3.Connection,
    inputs: ReconcileInputs,
    symbol: str,
    window: tuple[int, int] | None,
    tf: int,
    classify: Callable[[SeriesFacts | None, HistoryFacts | None], SeriesReconcile],
) -> SeriesReconcile:
    series = {"venue": inputs.venue, "symbol": symbol, "timeframe": inputs.timeframe.label}
    oldest, newest = conn.execute(BOUNDS_SQL, series).fetchone()
    if oldest is None:
        return classify(None, None)

    candles: set[int] = set()
    conflicts: set[int] = set()
    unavailable: set[int] = set()
    if window is not None:
        bounds = {**series, "start": max(window[0], oldest), "end": window[1]}
        candles = {row[0] for row in conn.execute(CANDLES_SQL, bounds).fetchall()}
        conflicts = {row[0] for row in conn.execute(CONFLICTS_SQL, bounds).fetchall()}
        unavailable = {row[0] for row in conn.execute(UNAVAILABLE_SQL, bounds).fetchall()}
    facts = SeriesFacts(
        oldest_open_ms=oldest,
        newest_open_ms=newest,
        candles=frozenset(candles),
        conflicts=frozenset(conflicts),
        unavailable=frozenset(unavailable),
    )

    ws = oldest if window is None else window[0]
    hole_rows = conn.execute(HOLES_SQL, {**series, "tf": tf, "ws": ws}).fetchall()
    raw, raw_old, runs = hole_rows[0][:3]
    largest = tuple((row[3], row[4]) for row in hole_rows if row[3] is not None)
    in_holes, in_holes_old = conn.execute(
        UNAVAILABLE_IN_HOLES_SQL, {**series, "ws": ws, "oldest": oldest, "newest": newest}
    ).fetchone()
    conflicted = conn.execute(CONFLICTED_SQL, series).fetchone()[0]
    history = HistoryFacts(
        raw_hole_slots=raw,
        raw_hole_slots_older_than_window=raw_old,
        hole_runs=runs,
        largest_runs=largest,
        unavailable_in_holes=in_holes,
        unavailable_in_holes_older_than_window=in_holes_old,
        conflicted_slots=conflicted,
    )
    return classify(facts, history)


async def reconcile(
    db: Path, inputs: ReconcileInputs, *, as_of_ms: int, target_open_ms: int | None
) -> dict[str, SeriesReconcile]:
    try:
        return await asyncio.to_thread(reconcile_blocking, db, inputs, as_of_ms=as_of_ms, target_open_ms=target_open_ms)
    except Exception as error:
        raise ReconcileError(f"restart reconcile failed: {error}") from error
