"""One request's reads for the open interest coverage endpoint, in a single read transaction (contract api-v1 6.6).

Same discipline as ``open_interest_page``: ``BEGIN`` makes the schema check, the series bounds and the two time reads
see one snapshot, every statement is bound and consumed before the next. Only ``time_ms`` is read, never a value.
The clock (``as_of_ms``) and the rule are inputs; nothing here reads them from anywhere.
"""

import sqlite3
from dataclasses import dataclass

from quasar_market_data.api.candle_reader import UnknownSeries
from quasar_market_data.api.params import OpenInterestCoverageRequest
from quasar_market_data.domain.open_interest_coverage import (
    OpenInterestRule,
    OpenInterestRunCoverage,
    classify_open_interest_runs,
)
from quasar_market_data.storage.schema import verify_schema


@dataclass(frozen=True)
class OpenInterestCoverageReport:
    venue: str
    symbol: str
    period: str
    start: int
    end: int
    oldest_time_ms: int
    newest_time_ms: int
    runs: OpenInterestRunCoverage


def read_open_interest_coverage_report(
    conn: sqlite3.Connection, request: OpenInterestCoverageRequest, *, as_of_ms: int, rule: OpenInterestRule
) -> OpenInterestCoverageReport:
    conn.execute("BEGIN")
    try:
        return _read(conn, request, as_of_ms, rule)
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # a broken connection must not mask the original error; the caller closes it


def _read(
    conn: sqlite3.Connection, request: OpenInterestCoverageRequest, as_of_ms: int, rule: OpenInterestRule
) -> OpenInterestCoverageReport:
    verify_schema(conn)
    series = (request.venue, request.symbol, request.period.label)

    # Two scalar subqueries: SQLite seeks the primary key for a lone MIN or MAX, but walks the series for both at once.
    oldest, newest = conn.execute(
        "SELECT (SELECT MIN(time_ms) FROM open_interest WHERE venue = ? AND symbol = ? AND period = ?),"
        " (SELECT MAX(time_ms) FROM open_interest WHERE venue = ? AND symbol = ? AND period = ?)",
        (*series, *series),
    ).fetchone()
    if oldest is None:
        raise UnknownSeries  # a series with only conflict records is unknown (contract 6.6.5)

    window = (*series, request.start, request.end)
    stored = {
        row[0]
        for row in conn.execute(
            "SELECT time_ms FROM open_interest"
            " WHERE venue = ? AND symbol = ? AND period = ? AND time_ms >= ? AND time_ms < ?",
            window,
        ).fetchall()
    }
    conflicts = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT time_ms FROM open_interest_conflicts"
            " WHERE venue = ? AND symbol = ? AND period = ? AND time_ms >= ? AND time_ms < ?",
            window,
        ).fetchall()
    }
    runs = classify_open_interest_runs(
        start=request.start,
        end=request.end,
        stored=stored,
        conflicts=conflicts,
        newest_time_ms=newest,
        as_of_ms=as_of_ms,
        rule=rule,
    )
    return OpenInterestCoverageReport(
        venue=request.venue,
        symbol=request.symbol,
        period=request.period.label,
        start=request.start,
        end=request.end,
        oldest_time_ms=oldest,
        newest_time_ms=newest,
        runs=runs,
    )
