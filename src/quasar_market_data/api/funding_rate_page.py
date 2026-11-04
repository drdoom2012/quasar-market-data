"""One request's reads of the funding rate store, in a single read transaction (contract api-v1 6.7, 5.6).

Same discipline as ``open_interest_page``: the connection is in autocommit mode, so ``BEGIN`` makes the schema
check, the series bounds, the span, the rows and the coverage counts see one snapshot. Every statement is bound
and consumed before the next. The coverage describes the **whole window** ``[start, end]`` (two range counts and
the span row), never the page: paging changes ``rows``, ``next`` and the per-row ``conflicted`` only.
"""

import sqlite3
from dataclasses import dataclass

from quasar_market_data.api.candle_reader import UnknownSeries
from quasar_market_data.api.params import FundingRatesRequest
from quasar_market_data.domain.funding_rate_coverage import WindowState, is_complete, window_state
from quasar_market_data.domain.funding_rate_span import Interval
from quasar_market_data.storage.schema import verify_schema


@dataclass(frozen=True)
class StoredFundingRate:
    funding_time_ms: int  # an instant, not an interval (contract 6.7.1)
    rate_type: str
    funding_rate: str  # signed canonical decimal text, per settlement
    mark_price: str | None
    conflicted: bool


@dataclass(frozen=True)
class FundingRatePage:
    venue: str
    symbol: str
    start: int
    end: int
    oldest_ms: int | None  # rows of the whole series; None when the series has a span but no row
    newest_ms: int | None
    rows: tuple[StoredFundingRate, ...]
    next: int | None
    span: Interval | None
    verified_at_ms: int | None
    events_in_window: int
    conflicted_events: int
    window_state: WindowState
    complete: bool


def read_funding_rate_page(
    conn: sqlite3.Connection, request: FundingRatesRequest, *, end: int, as_of_ms: int
) -> FundingRatePage:
    """``end`` is the effective window end (the handler resolves a defaulted one); ``as_of_ms`` is for the caller only."""
    if request.limit < 2:
        raise ValueError(f"limit must be at least 2, got {request.limit}")
    conn.execute("BEGIN")
    try:
        return _read(conn, request, end)
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # a broken connection must not mask the original error; the caller closes it


def _read(conn: sqlite3.Connection, request: FundingRatesRequest, end: int) -> FundingRatePage:
    verify_schema(conn)
    series = (request.venue, request.symbol)
    start, limit = request.start, request.limit

    span_row = conn.execute(
        "SELECT from_ms, through_ms, verified_at_ms FROM funding_rate_verified WHERE venue = ? AND symbol = ?", series
    ).fetchone()
    # Two scalar subqueries: SQLite seeks the primary key for a lone MIN or MAX, but walks the series for both at once.
    oldest, newest = conn.execute(
        "SELECT (SELECT MIN(funding_time_ms) FROM funding_rates WHERE venue = ? AND symbol = ?),"
        " (SELECT MAX(funding_time_ms) FROM funding_rates WHERE venue = ? AND symbol = ?)",
        (*series, *series),
    ).fetchone()
    if span_row is None and oldest is None:
        raise UnknownSeries

    fetched = conn.execute(
        "SELECT funding_time_ms, rate_type, funding_rate, mark_price FROM funding_rates"
        " WHERE venue = ? AND symbol = ? AND funding_time_ms >= ? AND funding_time_ms <= ?"
        " ORDER BY funding_time_ms, rate_type LIMIT ?",
        (*series, start, end, limit + 1),
    ).fetchall()
    # A page never splits the (at most two) rows of one millisecond: an integer ``next`` then repeats and skips none.
    if len(fetched) > limit:
        cut = limit - 1 if fetched[limit][0] == fetched[limit - 1][0] else limit
        page_rows, next_ms = fetched[:cut], fetched[cut][0]
    else:
        page_rows, next_ms = fetched, None

    conflicted_keys: set[tuple[int, str]] = set()
    if page_rows:
        conflicted_keys = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT DISTINCT funding_time_ms, rate_type FROM funding_rate_conflicts"
                " WHERE venue = ? AND symbol = ? AND funding_time_ms >= ? AND funding_time_ms <= ?",
                (*series, page_rows[0][0], page_rows[-1][0]),
            ).fetchall()
        }

    events_in_window = conn.execute(
        "SELECT COUNT(*) FROM funding_rates WHERE venue = ? AND symbol = ? AND funding_time_ms >= ?"
        " AND funding_time_ms <= ?",
        (*series, start, end),
    ).fetchone()[0]
    conflicted_events = conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT funding_time_ms, rate_type FROM funding_rate_conflicts"
        " WHERE venue = ? AND symbol = ? AND funding_time_ms >= ? AND funding_time_ms <= ?)",
        (*series, start, end),
    ).fetchone()[0]

    span = None if span_row is None else Interval(span_row[0], span_row[1])
    state = window_state(span, start, end)
    return FundingRatePage(
        venue=request.venue,
        symbol=request.symbol,
        start=start,
        end=end,
        oldest_ms=oldest,
        newest_ms=newest,
        rows=tuple(StoredFundingRate(r[0], r[1], r[2], r[3], (r[0], r[1]) in conflicted_keys) for r in page_rows),
        next=next_ms,
        span=span,
        verified_at_ms=None if span_row is None else span_row[2],
        events_in_window=events_in_window,
        conflicted_events=conflicted_events,
        window_state=state,
        complete=is_complete(state, conflicted_events),
    )
