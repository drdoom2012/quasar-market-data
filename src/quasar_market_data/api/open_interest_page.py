"""One request's reads of the open interest store, in a single read transaction (contract api-v1 6.5, 5.6).

Same discipline as ``candle_reader``: the connection is in autocommit mode, so ``BEGIN`` makes the schema check, the
series bounds, the rows and the conflict reads see one snapshot. Every statement is bound and consumed before the
next. The clock (``as_of_ms``) and the rule are inputs; nothing here reads them from anywhere.
"""

import sqlite3
from dataclasses import dataclass

from quasar_market_data.api.candle_reader import UnknownSeries
from quasar_market_data.api.params import MAX_WINDOW_SLOTS, OpenInterestRequest
from quasar_market_data.domain.open_interest_coverage import (
    OpenInterestCoverage,
    OpenInterestRule,
    classify_open_interest_span,
)
from quasar_market_data.storage.schema import verify_schema


@dataclass(frozen=True)
class StoredOpenInterest:
    time_ms: int  # the END of the period (ADR-0003 P-3)
    open_interest: str
    open_interest_value: str
    conflicted: bool


@dataclass(frozen=True)
class OpenInterestPage:
    venue: str
    symbol: str
    period: str
    start: int
    end: int
    oldest_time_ms: int
    newest_time_ms: int
    rows: tuple[StoredOpenInterest, ...]
    next: int | None
    span_start: int
    span_end: int
    coverage: OpenInterestCoverage


def read_open_interest_page(
    conn: sqlite3.Connection, request: OpenInterestRequest, *, as_of_ms: int, rule: OpenInterestRule
) -> OpenInterestPage:
    conn.execute("BEGIN")
    try:
        return _read(conn, request, as_of_ms, rule)
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # a broken connection must not mask the original error; the caller closes it


def _read(
    conn: sqlite3.Connection, request: OpenInterestRequest, as_of_ms: int, rule: OpenInterestRule
) -> OpenInterestPage:
    verify_schema(conn)
    series = (request.venue, request.symbol, request.period.label)
    step = request.period.milliseconds

    # Two scalar subqueries: SQLite seeks the primary key for a lone MIN or MAX, but walks the series for both at once.
    oldest, newest = conn.execute(
        "SELECT (SELECT MIN(time_ms) FROM open_interest WHERE venue = ? AND symbol = ? AND period = ?),"
        " (SELECT MAX(time_ms) FROM open_interest WHERE venue = ? AND symbol = ? AND period = ?)",
        (*series, *series),
    ).fetchone()
    if oldest is None:
        raise UnknownSeries

    start = request.start
    if request.end is not None:
        end = request.end
    elif start <= newest:
        end = min(newest + step, start + MAX_WINDOW_SLOTS * step)  # plain integer arithmetic: no timestamp guard
    else:
        end = start + step

    rows = conn.execute(
        "SELECT time_ms, open_interest, open_interest_value FROM open_interest"
        " WHERE venue = ? AND symbol = ? AND period = ? AND time_ms >= ? AND time_ms < ?"
        " ORDER BY time_ms LIMIT ?",
        (*series, start, end, request.limit + 1),
    ).fetchall()
    next_time = rows[request.limit][0] if len(rows) > request.limit else None
    rows = rows[: request.limit]
    span_end = next_time if next_time is not None else end

    conflicts = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT time_ms FROM open_interest_conflicts"
            " WHERE venue = ? AND symbol = ? AND period = ? AND time_ms >= ? AND time_ms < ?",
            (*series, start, span_end),
        ).fetchall()
    }

    coverage = classify_open_interest_span(
        start=start,
        end=span_end,
        stored={row[0] for row in rows},
        conflicts=conflicts,
        newest_time_ms=newest,
        as_of_ms=as_of_ms,
        rule=rule,
    )
    return OpenInterestPage(
        venue=request.venue,
        symbol=request.symbol,
        period=request.period.label,
        start=start,
        end=end,
        oldest_time_ms=oldest,
        newest_time_ms=newest,
        rows=tuple(StoredOpenInterest(row[0], row[1], row[2], row[0] in coverage.conflicted_times) for row in rows),
        next=next_time,
        span_start=start,
        span_end=span_end,
        coverage=coverage,
    )
