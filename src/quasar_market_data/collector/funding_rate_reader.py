"""Read-only facts about one stored funding rate series, for the restart reconcile and the re-read (ADR-0004 P-7, P-11).

Same discipline as ``open_interest_reader``: a read-only connection, one ``BEGIN`` snapshot, ``verify_schema``,
bound statements, **one series per call** (a short read transaction, never held across an ``await`` and never over
all series at once). It runs on a worker thread of the default executor, never on the funding writer's thread. Any
failure is a ``FundingRateReadError``; the task treats that as fatal for itself (fail closed) and never for candles.
"""

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from quasar_market_data.domain.funding_rate_span import Interval
from quasar_market_data.storage.connection import StorageError, open_reader
from quasar_market_data.storage.schema import verify_schema

_SERIES: Final = "venue = :venue AND symbol = :symbol"
SPAN_SQL: Final = f"SELECT from_ms, through_ms, verified_at_ms FROM funding_rate_verified WHERE {_SERIES}"
ROWS_SQL: Final = f"SELECT COUNT(*) FROM funding_rates WHERE {_SERIES}"
CONFLICTED_SQL: Final = f"SELECT COUNT(DISTINCT funding_time_ms) FROM funding_rate_conflicts WHERE {_SERIES}"


class FundingRateReadError(StorageError):
    """The stored facts could not be read."""


@dataclass(frozen=True)
class SeriesFacts:
    symbol: str
    span: Interval | None
    verified_at_ms: int | None
    stored_rows: int
    conflicted_events: int


def _span(conn: sqlite3.Connection, params: dict[str, str]) -> tuple[Interval | None, int | None]:
    row = conn.execute(SPAN_SQL, params).fetchone()
    return (None, None) if row is None else (Interval(row[0], row[1]), row[2])


def _read(db: Path, venue: str, symbol: str, with_counts: bool) -> SeriesFacts:
    params = {"venue": venue, "symbol": symbol}
    conn = open_reader(db)
    try:
        conn.execute("BEGIN")
        try:
            verify_schema(conn)
            span, verified_at = _span(conn, params)
            rows = conflicted = 0
            if with_counts:
                rows = conn.execute(ROWS_SQL, params).fetchone()[0]
                conflicted = conn.execute(CONFLICTED_SQL, params).fetchone()[0]
            return SeriesFacts(symbol, span, verified_at, rows, conflicted)
        finally:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # a broken connection must not mask the original error; it is closed below
    finally:
        conn.close()


async def read_series_facts(db: Path, venue: str, symbol: str) -> SeriesFacts:
    """The span, the stored row count and the number of conflicted events of one series."""
    try:
        return await asyncio.to_thread(_read, db, venue, symbol, True)
    except Exception as error:
        raise FundingRateReadError(f"reading funding rate facts failed: {type(error).__name__}: {error}") from error


async def read_span(db: Path, venue: str, symbol: str) -> tuple[Interval | None, int | None]:
    """The span and its ``verified_at_ms`` of one series, for the re-read after an unknown write outcome."""
    try:
        facts = await asyncio.to_thread(_read, db, venue, symbol, False)
    except Exception as error:
        raise FundingRateReadError(f"reading the funding rate span failed: {type(error).__name__}: {error}") from error
    return facts.span, facts.verified_at_ms
