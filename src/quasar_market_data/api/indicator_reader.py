"""The stored inputs of one indicator window, read in a single read transaction (contract api-v1 section 6.3.3).

Same discipline as ``coverage_reader``: ``BEGIN`` makes the readiness check, the series lookup and the three fact
reads see one snapshot, every statement is bound and consumed before the next. Closes are returned as the stored
decimal text; the conversion to float belongs to the engine. Nothing here reads a clock.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from quasar_market_data.api.candle_reader import UnknownSeries
from quasar_market_data.api.params import IndicatorsRequest
from quasar_market_data.storage.schema import verify_schema


@dataclass(frozen=True)
class IndicatorInputs:
    venue: str
    symbol: str
    timeframe: str
    start: int
    end: int
    read_start: int
    oldest_open_time_ms: int
    newest_open_time_ms: int
    closes: Mapping[int, str]
    conflicts: frozenset[int]
    unavailable: frozenset[int]


def read_indicator_inputs(
    conn: sqlite3.Connection, request: IndicatorsRequest, *, end: int, read_start: int
) -> IndicatorInputs:
    conn.execute("BEGIN")
    try:
        return _read(conn, request, end, read_start)
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # a broken connection must not mask the original error; the caller closes it


def _read(conn: sqlite3.Connection, request: IndicatorsRequest, end: int, read_start: int) -> IndicatorInputs:
    verify_schema(conn)
    series = (request.venue, request.symbol, request.timeframe.label)

    # Two scalar subqueries: SQLite seeks the primary key for a lone MIN or MAX, but walks the series for both at once.
    oldest, newest = conn.execute(
        "SELECT (SELECT MIN(open_time_ms) FROM candles WHERE venue = ? AND symbol = ? AND timeframe = ?),"
        " (SELECT MAX(open_time_ms) FROM candles WHERE venue = ? AND symbol = ? AND timeframe = ?)",
        (*series, *series),
    ).fetchone()
    if oldest is None:
        raise UnknownSeries

    window = (*series, read_start, end)
    closes = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT open_time_ms, close FROM candles"
            " WHERE venue = ? AND symbol = ? AND timeframe = ? AND open_time_ms >= ? AND open_time_ms < ?",
            window,
        ).fetchall()
    }
    conflicts = frozenset(
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT open_time_ms FROM candle_conflicts"
            " WHERE venue = ? AND symbol = ? AND timeframe = ? AND open_time_ms >= ? AND open_time_ms < ?",
            window,
        ).fetchall()
    )
    unavailable = frozenset(
        row[0]
        for row in conn.execute(
            "SELECT open_time_ms FROM unavailable_candles"
            " WHERE venue = ? AND symbol = ? AND timeframe = ? AND open_time_ms >= ? AND open_time_ms < ?",
            window,
        ).fetchall()
    )
    return IndicatorInputs(
        venue=request.venue,
        symbol=request.symbol,
        timeframe=request.timeframe.label,
        start=request.start,
        end=end,
        read_start=read_start,
        oldest_open_time_ms=oldest,
        newest_open_time_ms=newest,
        closes=closes,
        conflicts=conflicts,
        unavailable=unavailable,
    )
