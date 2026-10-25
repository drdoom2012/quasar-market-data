"""The stored facts of one coverage window, read in a single read transaction (contract api-v1 section 6.1).

Same discipline as ``candle_reader``: ``BEGIN`` makes the readiness check, the series lookup and the three fact
reads see one snapshot, every statement is bound and consumed before the next. Only open times are read, never
values. The clock is an input (``as_of_ms``); nothing here reads it.
"""

import sqlite3
from dataclasses import dataclass

from quasar_market_data.api.candle_reader import UnknownSeries
from quasar_market_data.api.params import CoverageRequest
from quasar_market_data.domain.timed_coverage import ExpectationRule, TimedCoverage, classify_span_at
from quasar_market_data.storage.schema import verify_schema


@dataclass(frozen=True)
class CoverageReport:
    venue: str
    symbol: str
    timeframe: str
    start: int
    end: int
    oldest_open_time_ms: int
    newest_open_time_ms: int
    coverage: TimedCoverage


def read_coverage_report(
    conn: sqlite3.Connection, request: CoverageRequest, *, as_of_ms: int, rule: ExpectationRule
) -> CoverageReport:
    conn.execute("BEGIN")
    try:
        return _read(conn, request, as_of_ms, rule)
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # a broken connection must not mask the original error; the caller closes it


def _read(conn: sqlite3.Connection, request: CoverageRequest, as_of_ms: int, rule: ExpectationRule) -> CoverageReport:
    verify_schema(conn)
    series = (request.venue, request.symbol, request.timeframe.label)

    oldest, newest = conn.execute(
        "SELECT MIN(open_time_ms), MAX(open_time_ms) FROM candles WHERE venue = ? AND symbol = ? AND timeframe = ?",
        series,
    ).fetchone()
    if oldest is None:
        raise UnknownSeries

    window = (*series, request.start, request.end)
    candles = {
        row[0]
        for row in conn.execute(
            "SELECT open_time_ms FROM candles"
            " WHERE venue = ? AND symbol = ? AND timeframe = ? AND open_time_ms >= ? AND open_time_ms < ?",
            window,
        ).fetchall()
    }
    conflicts = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT open_time_ms FROM candle_conflicts"
            " WHERE venue = ? AND symbol = ? AND timeframe = ? AND open_time_ms >= ? AND open_time_ms < ?",
            window,
        ).fetchall()
    }
    unavailable = {
        row[0]
        for row in conn.execute(
            "SELECT open_time_ms FROM unavailable_candles"
            " WHERE venue = ? AND symbol = ? AND timeframe = ? AND open_time_ms >= ? AND open_time_ms < ?",
            window,
        ).fetchall()
    }

    coverage = classify_span_at(
        start=request.start,
        end=request.end,
        timeframe_ms=request.timeframe.milliseconds,
        candles=candles,
        conflicts=conflicts,
        unavailable=unavailable,
        newest_open_time_ms=newest,
        as_of_ms=as_of_ms,
        rule=rule,
    )
    return CoverageReport(
        venue=request.venue,
        symbol=request.symbol,
        timeframe=request.timeframe.label,
        start=request.start,
        end=request.end,
        oldest_open_time_ms=oldest,
        newest_open_time_ms=newest,
        coverage=coverage,
    )
