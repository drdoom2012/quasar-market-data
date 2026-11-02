"""Read-only facts about one stored open interest series, for restart reconcile and catch-up (ADR-0003 P-12).

Same discipline as ``hole_reader``: a read-only connection, one ``BEGIN`` snapshot, ``verify_schema``, bound
statements, one series per call (a short read transaction, never held across an ``await``). It runs on a worker
thread of the default executor, never on the candle writer's thread. Only ``time_ms`` is read, never values. Any
failure is an ``OpenInterestReadError``; the task treats that as fatal for itself (fail closed) and never for candles.

``with_history=True`` adds the two figures the start-up summary needs: the series' first stored ``time_ms`` and the
number of rows older than ``oldest_ms``, from which the slots that are no longer recoverable are counted.
"""

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import StorageError, open_reader
from quasar_market_data.storage.schema import verify_schema

_SERIES: Final = "venue = :venue AND symbol = :symbol AND period = :period"
STORED_SQL: Final = (
    f"SELECT time_ms FROM open_interest WHERE {_SERIES} AND time_ms BETWEEN :oldest AND :newest ORDER BY time_ms"
)
CONFLICTED_SQL: Final = (
    "SELECT DISTINCT time_ms FROM open_interest_conflicts"
    f" WHERE {_SERIES} AND time_ms BETWEEN :oldest AND :newest ORDER BY time_ms"
)
FIRST_SQL: Final = f"SELECT MIN(time_ms) FROM open_interest WHERE {_SERIES}"
OLDER_SQL: Final = f"SELECT COUNT(*) FROM open_interest WHERE {_SERIES} AND time_ms < :oldest"


class OpenInterestReadError(StorageError):
    """The stored facts could not be read."""


@dataclass(frozen=True)
class SeriesFacts:
    stored: tuple[int, ...]
    conflicted: tuple[int, ...]
    first_stored_ms: int | None  # only with ``with_history``
    stored_older: int | None  # rows older than ``oldest_ms``; only with ``with_history``


def _read(
    db: Path, venue: str, symbol: str, period: str, oldest_ms: int, newest_ms: int, with_history: bool
) -> SeriesFacts:
    params = {"venue": venue, "symbol": symbol, "period": period}
    window = {**params, "oldest": oldest_ms, "newest": newest_ms}
    conn = open_reader(db)
    try:
        conn.execute("BEGIN")
        try:
            verify_schema(conn)
            stored = tuple(row[0] for row in conn.execute(STORED_SQL, window))
            conflicted = tuple(row[0] for row in conn.execute(CONFLICTED_SQL, window))
            first: int | None = None
            older: int | None = None
            if with_history:
                first = conn.execute(FIRST_SQL, params).fetchone()[0]
                older = conn.execute(OLDER_SQL, {**params, "oldest": oldest_ms}).fetchone()[0]
            return SeriesFacts(stored, conflicted, first, older)
        finally:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # a broken connection must not mask the original error; it is closed below
    finally:
        conn.close()


async def read_series(
    db: Path,
    *,
    venue: str,
    symbol: str,
    period: Timeframe,
    oldest_ms: int,
    newest_ms: int,
    with_history: bool = False,
) -> SeriesFacts:
    try:
        return await asyncio.to_thread(_read, db, venue, symbol, period.label, oldest_ms, newest_ms, with_history)
    except Exception as error:
        raise OpenInterestReadError(f"reading open interest facts failed: {type(error).__name__}: {error}") from error
