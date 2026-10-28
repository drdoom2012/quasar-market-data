"""Read-only discovery of holes and read-back of stored open times for the backfill (S16, D4, D9).

Holes are derived from the stored candles on every call (S15 D1); nothing is remembered. A slot that has a
``candle_conflicts`` or an ``unavailable_candles`` record is a recorded fact, not a hole, and is never fetched.
Same discipline as ``reconcile_reader``: a read-only connection, one ``BEGIN`` snapshot, bound statements, the
shared SQL imported rather than copied. Only open times are read, never values. Nothing here writes.
"""

import asyncio
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from quasar_market_data.collector.reconcile_reader import (
    _PAIRS,
    BOUNDS_SQL,
    CANDLES_SQL,
    CONFLICTS_SQL,
    UNAVAILABLE_SQL,
)
from quasar_market_data.domain.backfill import BackfillTarget
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import StorageError, open_reader
from quasar_market_data.storage.schema import verify_schema

MAX_RUNS: Final = 20

RUNS_SQL: Final = (
    f"SELECT prev + :tf, open_time_ms FROM ({_PAIRS}) WHERE open_time_ms - prev > :tf AND open_time_ms > :hs"
    " ORDER BY open_time_ms DESC"
)
PRESENT_SQL: Final = f"{CANDLES_SQL} ORDER BY open_time_ms"


class HoleReadError(StorageError):
    """The stored facts could not be read."""


@dataclass(frozen=True)
class SeriesHoles:
    oldest_stored_ms: int | None
    interior_runs: tuple[tuple[int, int], ...]  # raw [start, end), newest first, clipped to the horizon


def _read_holes(db: Path, venue: str, symbol: str, timeframe: Timeframe, horizon_start_ms: int) -> SeriesHoles:
    tf = timeframe.milliseconds
    series = {"venue": venue, "symbol": symbol, "timeframe": timeframe.label}
    conn = open_reader(db)
    try:
        conn.execute("BEGIN")
        try:
            verify_schema(conn)
            oldest = conn.execute(BOUNDS_SQL, series).fetchone()[0]
            runs: list[tuple[int, int]] = []
            if oldest is not None:
                cursor = conn.execute(RUNS_SQL, {**series, "tf": tf, "hs": horizon_start_ms})
                for start, end in cursor:
                    start = max(start, horizon_start_ms)
                    if start >= end:
                        continue
                    bounds = {**series, "start": start, "end": end}
                    marked = {r[0] for r in conn.execute(CONFLICTS_SQL, bounds).fetchall()}
                    marked |= {r[0] for r in conn.execute(UNAVAILABLE_SQL, bounds).fetchall()}
                    stop = end
                    for slot in sorted(marked, reverse=True):
                        if slot + tf < stop:
                            runs.append((slot + tf, stop))
                        stop = slot
                    if start < stop:
                        runs.append((start, stop))
                    if len(runs) >= MAX_RUNS:
                        break
            return SeriesHoles(oldest, tuple(runs[:MAX_RUNS]))
        finally:
            _rollback(conn)
    finally:
        conn.close()


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass  # a broken connection must not mask the original error; it is closed by the caller


def _read_present(
    db: Path, venue: str, symbol: str, timeframe: Timeframe, targets: Sequence[BackfillTarget]
) -> tuple[frozenset[int], ...]:
    series = {"venue": venue, "symbol": symbol, "timeframe": timeframe.label}
    conn = open_reader(db)
    try:
        conn.execute("BEGIN")
        try:
            verify_schema(conn)
            return tuple(
                frozenset(
                    row[0]
                    for row in conn.execute(
                        PRESENT_SQL, {**series, "start": target.first_ms, "end": target.end_ms}
                    ).fetchall()
                )
                for target in targets
            )
        finally:
            _rollback(conn)
    finally:
        conn.close()


async def read_holes(db: Path, *, venue: str, symbol: str, timeframe: Timeframe, horizon_start_ms: int) -> SeriesHoles:
    try:
        return await asyncio.to_thread(_read_holes, db, venue, symbol, timeframe, horizon_start_ms)
    except Exception as error:
        raise HoleReadError(f"reading holes failed: {error}") from error


async def read_present(
    db: Path, *, venue: str, symbol: str, timeframe: Timeframe, targets: Sequence[BackfillTarget]
) -> tuple[frozenset[int], ...]:
    """The stored open times per target, in one snapshot: the only source of recovery facts."""
    try:
        return await asyncio.to_thread(_read_present, db, venue, symbol, timeframe, tuple(targets))
    except Exception as error:
        raise HoleReadError(f"reading stored slots failed: {error}") from error
