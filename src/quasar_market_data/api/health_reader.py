"""Read-only facts for the health endpoints (contract api-v1 section 6.2).

Same discipline as ``coverage_reader``: ``BEGIN`` makes the schema check and every read see one snapshot, every
statement is bound and consumed before the next. A series is a ``(venue, symbol, timeframe)`` with at least one
row in ``candles``; rows in other tables never create one. The work is one next-series seek and one ``MAX`` seek
per series, both on the primary key ``(venue, symbol, timeframe, open_time_ms)``, so it depends on the number of
series and not on the number of rows in each (a ``GROUP BY`` would scan every row; a recursive-CTE skip scan was
measured as quadratic).
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


@dataclass(frozen=True)
class StoredSeries:
    venue: str
    symbol: str
    timeframe: str
    newest_open_time_ms: int


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
    finally:
        _rollback(conn)


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass  # a broken connection must not mask the original error; the caller closes it
