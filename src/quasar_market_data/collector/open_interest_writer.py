"""Idempotent, atomic persistence of open interest rows into schema v2 (ADR-0003 P-6, P-8, P-9).

What this writer guarantees: a row is inserted once; re-delivering the same row is a no-op; a re-delivered row
whose values differ is never written over, is reported back and is recorded in ``open_interest_conflicts``
(idempotently, in the same transaction). One call is one transaction of at most ``MAX_ROWS_PER_TRANSACTION`` rows
(P-9 rule 1).

What it does NOT guarantee: that a row is eligible (``select_eligible_open_interest``); the caller passes only
eligible rows (OI-4).

``OpenInterestConflict.newly_recorded`` is ``True`` only when the conflict row was inserted by this call, so a
caller can log each new conflict once (P-8). After an outcome-unknown ``COMMIT`` that recorded a conflict, a retry
reports ``newly_recorded=False`` for it: the row exists, but this call did not insert it.

The connection check and the failure classification are copied from ``collector/candle_writer.py`` (keep the two
in step). Unlike the candle writer, a writer whose rollback cannot be confirmed **closes its connection at once**,
so a connection stuck in a transaction cannot keep the write lock and stall candle writes (P-9).
"""

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from quasar_market_data.domain.open_interest import OpenInterest
from quasar_market_data.domain.timestamps import ensure_timestamp_ms
from quasar_market_data.storage.connection import StorageError
from quasar_market_data.storage.schema import verify_schema

MAX_ROWS_PER_TRANSACTION: Final = 100

INSERT_OPEN_INTEREST: Final = (
    "INSERT INTO open_interest (venue, symbol, period, time_ms, open_interest, open_interest_value, ingested_at_ms)"
    " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
)
RECORD_CONFLICT: Final = (
    "INSERT INTO open_interest_conflicts (venue, symbol, period, time_ms, open_interest, open_interest_value,"
    " first_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
)
_SELECT_STORED: Final = (
    "SELECT open_interest, open_interest_value FROM open_interest"
    " WHERE venue = ? AND symbol = ? AND period = ? AND time_ms = ?"
)


class OpenInterestWriteError(StorageError):
    """A transaction could not be written, or the writer cannot be used."""


class OpenInterestWriteOutcomeUnknown(OpenInterestWriteError):
    """``COMMIT`` failed in a way that leaves it unknown whether the (atomic) transaction is stored.

    It is stored completely or not at all; re-sending the same rows is safe.
    """


class OpenInterestWriteBusy(OpenInterestWriteError):
    """The database was locked longer than the busy timeout; rolled back, nothing was written."""


@dataclass(frozen=True)
class OpenInterestConflict:
    incoming: OpenInterest
    stored: OpenInterest
    newly_recorded: bool


@dataclass(frozen=True)
class OpenInterestBatchReport:
    inserted: int
    duplicates: int
    conflicts: tuple[OpenInterestConflict, ...]
    lock_wait_ns: int  # from the start of BEGIN IMMEDIATE until the write lock was held
    duration_ns: int  # from holding the lock until the end of COMMIT: what a candle write can wait for


def _require_writer_connection(conn: sqlite3.Connection) -> None:
    """Copy of ``candle_writer.require_writer_connection`` with this writer's error type."""
    if conn.isolation_level is not None:
        raise OpenInterestWriteError("connection must use isolation_level=None (explicit transactions)")
    if conn.in_transaction:
        raise OpenInterestWriteError("connection is already inside a transaction")
    if conn.execute("PRAGMA query_only").fetchone()[0] != 0:
        raise OpenInterestWriteError("connection is read-only")
    if conn.execute("PRAGMA recursive_triggers").fetchone()[0] != 1:
        raise OpenInterestWriteError(
            "recursive_triggers is off: INSERT OR REPLACE would bypass the immutability triggers"
        )
    if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        raise OpenInterestWriteError("database is not in WAL mode")


def _is_busy(error: sqlite3.Error) -> bool:
    """``SQLITE_BUSY`` or one of its extended codes (``sqlite_errorcode`` reports extended codes)."""
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xFF == sqlite3.SQLITE_BUSY


class OpenInterestWriter:
    """Owns one writer connection; ``write_transaction`` runs one ``BEGIN IMMEDIATE`` transaction per call."""

    def __init__(self, conn: sqlite3.Connection, *, monotonic_ns: Callable[[], int] = time.monotonic_ns) -> None:
        _require_writer_connection(conn)
        verify_schema(conn)
        self._conn = conn
        self._monotonic_ns = monotonic_ns
        self._closed = False
        self._unusable = False

    @property
    def unusable(self) -> bool:
        return self._unusable

    def close(self) -> None:
        self._closed = True
        self._conn.close()

    def _make_unusable(self) -> None:
        self._unusable = True
        try:
            self._conn.close()  # releases any lock a transaction that could not be rolled back still holds
        except sqlite3.Error:
            pass

    def _roll_back(self) -> None:
        if self._conn.in_transaction:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                self._make_unusable()
                return
        if self._conn.in_transaction:
            self._make_unusable()

    def _failure(self, error: sqlite3.Error, commit_issued: bool) -> OpenInterestWriteError:
        """Copy of ``CandleWriter._failure``, plus ``OpenInterestWriteBusy`` for a rolled-back busy error."""
        was_open = self._conn.in_transaction
        self._roll_back()
        confirmed = not self._unusable
        if not commit_issued:
            if confirmed:
                if _is_busy(error):
                    return OpenInterestWriteBusy(f"database busy; rolled back, nothing was written: {error}")
                return OpenInterestWriteError(f"transaction rolled back; nothing was written: {error}")
            return OpenInterestWriteError(
                f"transaction not committed; rollback could not be confirmed; the connection was closed: {error}"
            )
        if was_open and confirmed:
            if _is_busy(error):
                return OpenInterestWriteBusy(f"database busy at COMMIT; rolled back, nothing was written: {error}")
            return OpenInterestWriteError(f"COMMIT failed and was rolled back; nothing was written: {error}")
        suffix = "; the writer is now unusable" if self._unusable else ""
        return OpenInterestWriteOutcomeUnknown(
            f"transaction outcome unknown: COMMIT failed ({error}); it is either completely stored or not stored; "
            f"re-sending the same rows is safe{suffix}"
        )

    def write_transaction(self, rows: Sequence[OpenInterest], *, ingested_at_ms: int) -> OpenInterestBatchReport:
        if self._closed:
            raise OpenInterestWriteError("writer is closed")
        if self._unusable:
            raise OpenInterestWriteError("writer is unusable after a failed rollback; open a new one")
        ensure_timestamp_ms(ingested_at_ms, "ingested_at_ms")
        for index, row in enumerate(rows):
            if type(row) is not OpenInterest:
                raise TypeError(f"rows[{index}] must be an OpenInterest, got {type(row).__name__}")
        if len(rows) > MAX_ROWS_PER_TRANSACTION:
            raise ValueError(f"at most {MAX_ROWS_PER_TRANSACTION} rows per transaction, got {len(rows)}")
        if not rows:
            return OpenInterestBatchReport(0, 0, (), 0, 0)

        inserted = duplicates = 0
        conflicts: list[OpenInterestConflict] = []
        commit_issued = False
        started = self._monotonic_ns()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            locked = self._monotonic_ns()
            for row in rows:
                key = row.key
                slot = (key.venue, key.symbol, key.period.label, key.time_ms)
                values = (row.open_interest, row.open_interest_value)
                if self._conn.execute(INSERT_OPEN_INTEREST, (*slot, *values, ingested_at_ms)).rowcount == 1:
                    inserted += 1
                    continue
                stored_values = tuple(self._conn.execute(_SELECT_STORED, slot).fetchone())
                if stored_values == values:
                    duplicates += 1
                    continue
                recorded = self._conn.execute(RECORD_CONFLICT, (*slot, *values, ingested_at_ms)).rowcount == 1
                conflicts.append(
                    OpenInterestConflict(
                        incoming=row, stored=OpenInterest(key, *stored_values), newly_recorded=recorded
                    )
                )
            commit_issued = True
            self._conn.execute("COMMIT")
            finished = self._monotonic_ns()
        except sqlite3.Error as error:
            raise self._failure(error, commit_issued) from error
        except BaseException:
            self._roll_back()
            raise
        return OpenInterestBatchReport(inserted, duplicates, tuple(conflicts), locked - started, finished - locked)
