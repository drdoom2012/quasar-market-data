"""Idempotent, atomic persistence of validated candles into schema v1 (ADR-0002).

What this writer guarantees: a candle that passes ``validate_candle`` is inserted once; re-delivering the
same candle is a no-op; a re-delivered candle whose values differ is never written over and is reported
back (recording it in ``candle_conflicts`` is S8); a batch is one transaction.

What it does NOT guarantee: that a candle is closed. ``ValidatedCandle`` proves validity only, so a
validated but still-forming candle is persisted like any other. Passing only eligible candles
(``select_closed_candles``) is the caller's job and is wired and tested end to end in S9.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from quasar_market_data.domain.candle import Candle
from quasar_market_data.domain.timestamps import ensure_timestamp_ms
from quasar_market_data.domain.validation import ValidatedCandle
from quasar_market_data.storage.connection import StorageError
from quasar_market_data.storage.schema import verify_schema

_INSERT = (
    "INSERT INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover,"
    " ingested_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
)
_SELECT_STORED = (
    "SELECT open, high, low, close, volume, turnover FROM candles"
    " WHERE venue = ? AND symbol = ? AND timeframe = ? AND open_time_ms = ?"
)


class CandleWriteError(StorageError):
    """A batch could not be written, or the writer cannot be used."""


@dataclass(frozen=True)
class CandleConflict:
    """A re-delivered candle whose values differ from the stored one; nothing was written for it."""

    incoming: Candle
    stored: Candle


@dataclass(frozen=True)
class WriteReport:
    inserted: int
    duplicates: int
    conflicts: tuple[CandleConflict, ...]


def _require_writer_connection(conn: sqlite3.Connection) -> None:
    if conn.isolation_level is not None:
        raise CandleWriteError("connection must use isolation_level=None (explicit transactions)")
    if conn.in_transaction:
        raise CandleWriteError("connection is already inside a transaction")
    if conn.execute("PRAGMA query_only").fetchone()[0] != 0:
        raise CandleWriteError("connection is read-only")
    if conn.execute("PRAGMA recursive_triggers").fetchone()[0] != 1:
        raise CandleWriteError("recursive_triggers is off: INSERT OR REPLACE would bypass the immutability triggers")
    if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        raise CandleWriteError("database is not in WAL mode")


class CandleWriter:
    """Owns one writer connection; ``write`` runs each batch in a single ``BEGIN IMMEDIATE`` transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        _require_writer_connection(conn)
        verify_schema(conn)
        self._conn = conn
        self._closed = False

    def close(self) -> None:
        self._closed = True
        self._conn.close()

    def write(self, candles: Sequence[ValidatedCandle], *, ingested_at_ms: int) -> WriteReport:
        if self._closed:
            raise CandleWriteError("writer is closed")
        ensure_timestamp_ms(ingested_at_ms, "ingested_at_ms")
        for index, candle in enumerate(candles):
            if type(candle) is not Candle:
                raise TypeError(f"candles[{index}] must be a Candle, got {type(candle).__name__}")
        if not candles:
            return WriteReport(0, 0, ())

        inserted = duplicates = 0
        conflicts: list[CandleConflict] = []
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for candle in candles:
                key = candle.key
                slot = (key.venue, key.symbol, key.timeframe.label, key.open_time_ms)
                values = (candle.open, candle.high, candle.low, candle.close, candle.volume, candle.turnover)
                if self._conn.execute(_INSERT, (*slot, *values, ingested_at_ms)).rowcount == 1:
                    inserted += 1
                    continue
                stored_values = self._conn.execute(_SELECT_STORED, slot).fetchone()
                if tuple(stored_values) == values:
                    duplicates += 1
                else:
                    stored = Candle(key, *stored_values)
                    conflicts.append(CandleConflict(incoming=candle, stored=stored))
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        return WriteReport(inserted, duplicates, tuple(conflicts))
