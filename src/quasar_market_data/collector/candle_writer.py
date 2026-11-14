"""Idempotent, atomic persistence of validated candles into schema v1 (ADR-0002).

What this writer guarantees: a candle that passes ``validate_candle`` is inserted once; re-delivering the
same candle is a no-op; a re-delivered candle whose values differ is never written over, is reported back
and is recorded in ``candle_conflicts`` (idempotently, in the batch's transaction); a batch is one transaction.

Every committed batch is one registered write transaction, stamped by the writer after the write lock is held and
confirmed when its ``COMMIT`` ended within the readability bound (``collector/readability.py``, ADR-0005 P-3). A batch
that stores nothing new (only duplicates and conflicts already recorded) is rolled back. A batch still open when it
is too old to commit within the bound is rolled back and restamped; after ``CANDLE_STAMP_ATTEMPTS`` it fails with
``CandleWriteNotStored``, which is certain: nothing was written, retry later.

What it does NOT guarantee: that a candle is closed. ``ValidatedCandle`` proves validity only, so a
validated but still-forming candle is persisted like any other. Passing only eligible candles
(``select_closed_candles``) is the caller's job and is wired and tested end to end in S9.
"""

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from quasar_market_data.collector.readability import CANDLE_STAMP_ATTEMPTS, TransactionStamper
from quasar_market_data.domain.candle import Candle
from quasar_market_data.domain.validation import ValidatedCandle, validate_candle
from quasar_market_data.storage.connection import StorageError
from quasar_market_data.storage.schema import verify_schema

_INSERT = (
    "INSERT INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover,"
    " ingested_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
)
_RECORD_CONFLICT = (
    "INSERT INTO candle_conflicts (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover,"
    " first_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
)
_SELECT_STORED = (
    "SELECT open, high, low, close, volume, turnover FROM candles"
    " WHERE venue = ? AND symbol = ? AND timeframe = ? AND open_time_ms = ?"
)


class CandleWriteError(StorageError):
    """A batch could not be written, or the writer cannot be used."""


class CandleWriteOutcomeUnknown(CandleWriteError):
    """``COMMIT`` failed in a way that leaves it unknown whether the (atomic) batch is stored.

    The batch is stored completely or not at all; re-sending the same batch is safe.
    """


class CandleWriteNotStored(CandleWriteError):
    """Every attempt was too old to commit within the readability bound and was rolled back: nothing was written."""


@dataclass(frozen=True)
class CandleConflict:
    """A re-delivered candle whose values differ from the stored one.

    The stored candle is untouched; ``incoming`` is recorded in ``candle_conflicts`` in the batch's transaction.
    """

    incoming: Candle
    stored: Candle


@dataclass(frozen=True)
class WriteReport:
    inserted: int
    duplicates: int
    conflicts: tuple[CandleConflict, ...]
    stamp_ms: int | None = field(default=None, compare=False)  # the committed transaction's stamp; None if none


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

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        wall_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        stamp_monotonic_ns: Callable[[], int] = time.monotonic_ns,
        stamp_attempts: int = CANDLE_STAMP_ATTEMPTS,
    ) -> None:
        _require_writer_connection(conn)
        verify_schema(conn)
        if type(stamp_attempts) is not int or stamp_attempts < 1:
            raise ValueError(f"stamp_attempts must be an int >= 1, got {stamp_attempts!r}")
        self._conn = conn
        self._stamper = TransactionStamper(conn, "candles", wall_ms=wall_ms, monotonic_ns=stamp_monotonic_ns)
        self._attempts = stamp_attempts
        self._closed = False
        self._unusable = False

    def close(self) -> None:
        self._closed = True
        self._conn.close()

    def _roll_back(self) -> None:
        """Roll back an open transaction; if that fails or the transaction survives, never use the connection again."""
        if self._conn.in_transaction:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                self._unusable = True
                return
        if self._conn.in_transaction:
            self._unusable = True

    def _failure(self, error: sqlite3.Error, commit_issued: bool) -> CandleWriteError:
        """Roll back and classify a failed batch: persistence (stored or not) and connection state are separate facts.

        Before ``COMMIT`` was issued the batch cannot have become durable, even if the rollback cannot be
        confirmed (then only the connection's transaction state is unknown). A ``COMMIT`` that failed with the
        transaction gone, or whose rollback cannot be confirmed, may have taken effect: the outcome is unknown.
        """
        was_open = self._conn.in_transaction
        self._roll_back()
        confirmed = not self._unusable
        if not commit_issued:
            if confirmed:
                return CandleWriteError(f"batch rolled back; nothing was written: {error}")
            return CandleWriteError(
                "batch not committed; rollback could not be confirmed; this connection is unusable and may hold "
                f"the write lock until it is closed: {error}"
            )
        if was_open and confirmed:
            return CandleWriteError(f"COMMIT failed and was rolled back; nothing was written: {error}")
        suffix = "; the writer is now unusable" if self._unusable else ""
        return CandleWriteOutcomeUnknown(
            f"batch outcome unknown: COMMIT failed ({error}); the batch is atomic, so it is either completely stored "
            f"or not stored; re-sending the same batch is safe{suffix}"
        )

    def write(self, candles: Sequence[ValidatedCandle]) -> WriteReport:
        if self._closed:
            raise CandleWriteError("writer is closed")
        if self._unusable:
            raise CandleWriteError("writer is unusable after a failed rollback; close and reopen")
        for index, candle in enumerate(candles):
            if type(candle) is not Candle:
                raise TypeError(f"candles[{index}] must be a Candle, got {type(candle).__name__}")
        for index, candle in enumerate(candles):  # a cast to ValidatedCandle proves nothing at runtime
            violations = validate_candle(candle)
            if violations:
                codes = ", ".join(str(violation.code) for violation in violations)
                raise CandleWriteError(f"candles[{index}] is not valid ({codes}); nothing was written")
        if not candles:
            return WriteReport(0, 0, ())
        for _ in range(self._attempts):
            report = self._attempt(candles)
            if report is not None:
                return report
        raise CandleWriteNotStored(
            f"every one of {self._attempts} attempts was too old to commit within the readability bound and was "
            "rolled back; nothing was written; retry later"
        )

    def _attempt(self, candles: Sequence[ValidatedCandle]) -> WriteReport | None:
        """One transaction: a report, or ``None`` when it expired before ``COMMIT`` and was rolled back."""
        inserted = duplicates = 0
        recorded = False
        conflicts: list[CandleConflict] = []
        commit_issued = False
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            stamp = self._stamper.register()
            for candle in candles:
                key = candle.key
                slot = (key.venue, key.symbol, key.timeframe.label, key.open_time_ms)
                values = (candle.open, candle.high, candle.low, candle.close, candle.volume, candle.turnover)
                if self._conn.execute(_INSERT, (*slot, *values, stamp.stamp_ms)).rowcount == 1:
                    inserted += 1
                    continue
                stored_values = self._conn.execute(_SELECT_STORED, slot).fetchone()
                if tuple(stored_values) == values:
                    duplicates += 1
                else:
                    recorded |= self._conn.execute(_RECORD_CONFLICT, (*slot, *values, stamp.stamp_ms)).rowcount == 1
                    conflicts.append(CandleConflict(incoming=candle, stored=Candle(key, *stored_values)))
            if inserted == 0 and not recorded:
                self._conn.execute("ROLLBACK")  # nothing new: register nothing (ADR-0005 P-2)
                return WriteReport(0, duplicates, tuple(conflicts))
            if self._stamper.expired(stamp):
                self._conn.execute("ROLLBACK")
                return None
            commit_issued = True
            self._conn.execute("COMMIT")
        except sqlite3.Error as error:
            raise self._failure(error, commit_issued) from error
        except BaseException:
            self._roll_back()
            raise
        self._stamper.committed(stamp, lambda: _describe(candles), self._roll_back)
        return WriteReport(inserted, duplicates, tuple(conflicts), stamp.stamp_ms)


def _describe(candles: Sequence[ValidatedCandle]) -> str:
    series = sorted({f"{c.key.venue} {c.key.symbol} {c.key.timeframe.label}" for c in candles})
    times = [c.key.open_time_ms for c in candles]
    return f"series={series} open_time_ms=[{min(times)}, {max(times)}] rows={len(candles)}"
