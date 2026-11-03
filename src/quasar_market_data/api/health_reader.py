"""Read-only facts for the health endpoints (contract api-v1 section 6.2).

Same discipline as ``coverage_reader``: ``BEGIN`` makes the schema check and every read see one snapshot, every
statement is bound and consumed before the next. A series is a ``(venue, symbol, timeframe)`` with at least one
row in ``candles``; rows in other tables never create one. The work is one next-series seek and one ``MAX`` seek
per series, both on the primary key ``(venue, symbol, timeframe, open_time_ms)``, so it depends on the number of
series and not on the number of rows in each (a ``GROUP BY`` would scan every row; a recursive-CTE skip scan was
measured as quadratic).

Open interest (contract 6.5.8) is read in the same snapshot, the same way: a series is a ``(venue, symbol, period)``
with a row in ``open_interest``; per series one next-series seek, one ``MAX`` seek and one ``LIMIT 1`` probe of the
conflict index ``(venue, symbol, period, time_ms)`` for a conflict newer than the cut-off.
"""

import sqlite3
from dataclasses import dataclass

from quasar_market_data.storage.schema import SCHEMA_VERSION, verify_schema

_NEXT_SERIES = (
    "SELECT venue, symbol, timeframe FROM candles"
    " WHERE (venue, symbol, timeframe, open_time_ms) > (?, ?, ?, ?)"
    " ORDER BY venue, symbol, timeframe, open_time_ms LIMIT 1"
)
_NEWEST = "SELECT MAX(open_time_ms) FROM candles WHERE venue = ? AND symbol = ? AND timeframe = ?"
_BEFORE_ALL_ROWS = ("", "", "", -1)  # sorts before every row: the empty string is the smallest text, -1 below any time
_NEXT_OI_SERIES = (
    "SELECT venue, symbol, period FROM open_interest"
    " WHERE (venue, symbol, period, time_ms) > (?, ?, ?, ?)"
    " ORDER BY venue, symbol, period, time_ms LIMIT 1"
)
_NEWEST_OI = "SELECT MAX(time_ms) FROM open_interest WHERE venue = ? AND symbol = ? AND period = ?"
_CONFLICT_PROBE = (
    "SELECT 1 FROM open_interest_conflicts WHERE venue = ? AND symbol = ? AND period = ? AND time_ms > ? LIMIT 1"
)


@dataclass(frozen=True)
class StoredSeries:
    venue: str
    symbol: str
    timeframe: str
    newest_open_time_ms: int


@dataclass(frozen=True)
class StoredOpenInterestSeries:
    venue: str
    symbol: str
    period: str
    newest_time_ms: int


@dataclass(frozen=True)
class OpenInterestFacts:
    series: tuple[StoredOpenInterestSeries, ...]
    conflicted: frozenset[tuple[str, str, str]]  # series with a conflict record newer than the cut-off


def check_ready(conn: sqlite3.Connection) -> int:
    """Raise ``SchemaMismatchError`` (or a subclass) unless the schema is the one this API was built for."""
    conn.execute("BEGIN")
    try:
        verify_schema(conn)
        return SCHEMA_VERSION
    finally:
        _rollback(conn)


def read_stored_series(conn: sqlite3.Connection) -> tuple[StoredSeries, ...]:
    """Every stored series with its newest open time, ordered by ``(venue, symbol, timeframe)``, in one snapshot."""
    conn.execute("BEGIN")
    try:
        verify_schema(conn)
        return _walk_candle_series(conn)
    finally:
        _rollback(conn)


def read_ingest_snapshot(
    conn: sqlite3.Connection, conflicts_after_ms: int
) -> tuple[tuple[StoredSeries, ...], OpenInterestFacts]:
    """The candle series and the open interest series, with their conflicts, in ONE snapshot (contract 6.5.8).

    ``conflicts_after_ms`` is the cut-off: a series counts as conflicted when a conflict record has ``time_ms``
    strictly greater than it.
    """
    conn.execute("BEGIN")
    try:
        verify_schema(conn)
        candles = _walk_candle_series(conn)
        return candles, _walk_open_interest_series(conn, conflicts_after_ms)
    finally:
        _rollback(conn)


def _walk_candle_series(conn: sqlite3.Connection) -> tuple[StoredSeries, ...]:
    series: list[StoredSeries] = []
    cursor: tuple[str, str, str, int] = _BEFORE_ALL_ROWS
    while True:
        row = conn.execute(_NEXT_SERIES, cursor).fetchone()
        if row is None:
            return tuple(series)
        venue, symbol, timeframe = row
        newest = conn.execute(_NEWEST, (venue, symbol, timeframe)).fetchone()[0]
        series.append(StoredSeries(venue, symbol, timeframe, newest))
        cursor = (venue, symbol, timeframe, newest)


def _walk_open_interest_series(conn: sqlite3.Connection, conflicts_after_ms: int) -> OpenInterestFacts:
    series: list[StoredOpenInterestSeries] = []
    conflicted: set[tuple[str, str, str]] = set()
    cursor: tuple[str, str, str, int] = _BEFORE_ALL_ROWS
    while True:
        row = conn.execute(_NEXT_OI_SERIES, cursor).fetchone()
        if row is None:
            return OpenInterestFacts(tuple(series), frozenset(conflicted))
        venue, symbol, period = row
        newest = conn.execute(_NEWEST_OI, (venue, symbol, period)).fetchone()[0]
        series.append(StoredOpenInterestSeries(venue, symbol, period, newest))
        if conn.execute(_CONFLICT_PROBE, (venue, symbol, period, conflicts_after_ms)).fetchone() is not None:
            conflicted.add((venue, symbol, period))
        cursor = (venue, symbol, period, newest)


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass  # a broken connection must not mask the original error; the caller closes it
