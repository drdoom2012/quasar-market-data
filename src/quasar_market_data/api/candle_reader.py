"""One request's reads of the candle store, in a single read transaction (contract api-v1 sections 3, 5.1-5.3, 5.6).

The connection is in autocommit mode, so ``BEGIN`` is what makes the readiness check, the series lookup, the
candle read and the coverage reads see one snapshot. Every statement goes through ``conn.execute`` with bound
parameters, and each result is consumed before the next statement is issued.
"""

import sqlite3
from dataclasses import dataclass

from quasar_market_data.api.params import MAX_WINDOW_SLOTS, CandlesRequest
from quasar_market_data.domain.span_coverage import SpanCoverage, classify_span
from quasar_market_data.storage.schema import verify_schema


@dataclass(frozen=True)
class StoredCandle:
    open_time_ms: int
    open: str
    high: str
    low: str
    close: str
    volume: str
    turnover: str | None
    conflicted: bool


@dataclass(frozen=True)
class CandlesPage:
    venue: str
    symbol: str
    timeframe: str
    start: int
    end: int
    oldest_open_time_ms: int
    newest_open_time_ms: int
    candles: tuple[StoredCandle, ...]
    next: int | None
    span_start: int
    span_end: int
    coverage: SpanCoverage


class UnknownSeries(Exception):
    """No candle of the requested series has ever been stored."""


def read_candles_page(conn: sqlite3.Connection, request: CandlesRequest) -> CandlesPage:
    conn.execute("BEGIN")
    try:
        return _read(conn, request)
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # a broken connection must not mask the original error; the caller closes it


def _read(conn: sqlite3.Connection, request: CandlesRequest) -> CandlesPage:
    verify_schema(conn)
    series = (request.venue, request.symbol, request.timeframe.label)
    tf = request.timeframe.milliseconds

    oldest, newest = conn.execute(
        "SELECT MIN(open_time_ms), MAX(open_time_ms) FROM candles WHERE venue = ? AND symbol = ? AND timeframe = ?",
        series,
    ).fetchone()
    if oldest is None:
        raise UnknownSeries

    start = request.start
    if request.end is not None:
        end = request.end
    elif start <= newest:
        end = min(newest + tf, start + MAX_WINDOW_SLOTS * tf)  # plain integer arithmetic: no timestamp guard
    else:
        end = start + tf

    rows = conn.execute(
        "SELECT open_time_ms, open, high, low, close, volume, turnover FROM candles"
        " WHERE venue = ? AND symbol = ? AND timeframe = ? AND open_time_ms >= ? AND open_time_ms < ?"
        " ORDER BY open_time_ms LIMIT ?",
        (*series, start, end, request.limit + 1),
    ).fetchall()
    next_open_time = rows[request.limit][0] if len(rows) > request.limit else None
    rows = rows[: request.limit]
    span_end = next_open_time if next_open_time is not None else end

    window = (*series, start, span_end)
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

    coverage = classify_span(
        start=start,
        end=span_end,
        timeframe_ms=tf,
        candles={row[0] for row in rows},
        conflicts=conflicts,
        unavailable=unavailable,
        newest_open_time_ms=newest,
    )
    candles = tuple(
        StoredCandle(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[0] in coverage.conflicted_open_times)
        for row in rows
    )
    return CandlesPage(
        venue=request.venue,
        symbol=request.symbol,
        timeframe=request.timeframe.label,
        start=start,
        end=end,
        oldest_open_time_ms=oldest,
        newest_open_time_ms=newest,
        candles=candles,
        next=next_open_time,
        span_start=start,
        span_end=span_end,
        coverage=coverage,
    )
