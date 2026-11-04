"""Idempotent, atomic persistence of funding rate rows into schema v3 (ADR-0004 P-6, P-7, P-8, P-9).

What this writer guarantees: a row is inserted once; re-delivering the same row is a no-op; a re-delivered row
whose values differ (a different rate, a different mark price, NULL against a price) is never written over, is
reported back and is recorded in ``funding_rate_conflicts`` (idempotently, in the same transaction). A Regular
and a Special row of one ``fundingTime`` are two rows (``rate_type`` is part of the key) and never conflict. One
call is one transaction of at most ``MAX_ROWS_PER_TRANSACTION`` rows (P-9).

What it does NOT guarantee: that a row is eligible (``select_eligible_funding_rates``); the caller passes only
eligible rows (FR-4).

``FundingRateConflict.newly_recorded`` is ``True`` only when the conflict row was inserted by this call, so a
caller can log each new conflict once (P-8). After an outcome-unknown ``COMMIT`` that recorded a conflict, a retry
reports ``newly_recorded=False`` for it: the row exists, but this call did not insert it.

The connection check and the failure classification are copied from ``collector/open_interest_writer.py``, which
copied them from ``collector/candle_writer.py`` (keep the three in step). A writer whose rollback cannot be
confirmed **closes its connection at once**, so a connection stuck in a transaction cannot keep the write lock and
stall candle writes (P-9).
"""

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from quasar_market_data.domain.funding_rate import FundingRate, FundingRateKey
from quasar_market_data.domain.funding_rate_span import Interval
from quasar_market_data.domain.timestamps import ensure_timestamp_ms
from quasar_market_data.storage.connection import StorageError
from quasar_market_data.storage.schema import verify_schema

MAX_ROWS_PER_TRANSACTION: Final = 100

INSERT_FUNDING_RATE: Final = (
    "INSERT INTO funding_rates (venue, symbol, funding_time_ms, rate_type, funding_rate, mark_price, ingested_at_ms)"
    " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
)
RECORD_CONFLICT: Final = (
    "INSERT INTO funding_rate_conflicts (venue, symbol, funding_time_ms, rate_type, funding_rate, mark_price,"
    " first_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
)
_SELECT_STORED: Final = (
    "SELECT funding_rate, mark_price FROM funding_rates"
    " WHERE venue = ? AND symbol = ? AND funding_time_ms = ? AND rate_type = ?"
)


class FundingRateWriteError(StorageError):
    """A transaction could not be written, or the writer cannot be used."""


class FundingRateWriteOutcomeUnknown(FundingRateWriteError):
    """``COMMIT`` failed in a way that leaves it unknown whether the (atomic) transaction is stored.

    It is stored completely or not at all; re-sending the same rows is safe.
    """


class FundingRateWriteBusy(FundingRateWriteError):
    """The database was locked longer than the busy timeout; rolled back, nothing was written."""


class FundingSpanNotAdjacentError(FundingRateWriteError):
    """The ``merge`` interval does not touch the verified span; the transaction was rolled back."""


@dataclass(frozen=True)
class FundingRateConflict:
    incoming: FundingRate
    stored: FundingRate
    newly_recorded: bool


@dataclass(frozen=True)
class FundingRateBatchReport:
    inserted: int
    duplicates: int
    conflicts: tuple[FundingRateConflict, ...]
    lock_wait_ns: int  # from the start of BEGIN IMMEDIATE until the write lock was held
    duration_ns: int  # from holding the lock until the end of COMMIT: what a candle write can wait for
    inserted_keys: tuple[FundingRateKey, ...] = ()  # the rows this call inserted (late events, P-8)
    span_before: Interval | None = None  # the verified span inside the transaction before the merge
    span_after: Interval | None = None  # and after it; both None without a merge


def _require_writer_connection(conn: sqlite3.Connection) -> None:
    """Copy of ``open_interest_writer._require_writer_connection`` with this writer's error type."""
    if conn.isolation_level is not None:
        raise FundingRateWriteError("connection must use isolation_level=None (explicit transactions)")
    if conn.in_transaction:
        raise FundingRateWriteError("connection is already inside a transaction")
    if conn.execute("PRAGMA query_only").fetchone()[0] != 0:
        raise FundingRateWriteError("connection is read-only")
    if conn.execute("PRAGMA recursive_triggers").fetchone()[0] != 1:
        raise FundingRateWriteError(
            "recursive_triggers is off: INSERT OR REPLACE would bypass the immutability triggers"
        )
    if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        raise FundingRateWriteError("database is not in WAL mode")


def _is_busy(error: sqlite3.Error) -> bool:
    """``SQLITE_BUSY`` or one of its extended codes (``sqlite_errorcode`` reports extended codes)."""
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xFF == sqlite3.SQLITE_BUSY


class FundingRateWriter:
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

    def _failure(self, error: sqlite3.Error, commit_issued: bool) -> FundingRateWriteError:
        """Copy of ``OpenInterestWriter._failure``."""
        was_open = self._conn.in_transaction
        self._roll_back()
        confirmed = not self._unusable
        if not commit_issued:
            if confirmed:
                if _is_busy(error):
                    return FundingRateWriteBusy(f"database busy; rolled back, nothing was written: {error}")
                return FundingRateWriteError(f"transaction rolled back; nothing was written: {error}")
            return FundingRateWriteError(
                f"transaction not committed; rollback could not be confirmed; the connection was closed: {error}"
            )
        if was_open and confirmed:
            if _is_busy(error):
                return FundingRateWriteBusy(f"database busy at COMMIT; rolled back, nothing was written: {error}")
            return FundingRateWriteError(f"COMMIT failed and was rolled back; nothing was written: {error}")
        suffix = "; the writer is now unusable" if self._unusable else ""
        return FundingRateWriteOutcomeUnknown(
            f"transaction outcome unknown: COMMIT failed ({error}); it is either completely stored or not stored; "
            f"re-sending the same rows is safe{suffix}"
        )

    def write_transaction(self, rows: Sequence[FundingRate], *, ingested_at_ms: int) -> FundingRateBatchReport:
        if self._closed:
            raise FundingRateWriteError("writer is closed")
        if self._unusable:
            raise FundingRateWriteError("writer is unusable after a failed rollback; open a new one")
        ensure_timestamp_ms(ingested_at_ms, "ingested_at_ms")
        for index, row in enumerate(rows):
            if type(row) is not FundingRate:
                raise TypeError(f"rows[{index}] must be a FundingRate, got {type(row).__name__}")
        if len(rows) > MAX_ROWS_PER_TRANSACTION:
            raise ValueError(f"at most {MAX_ROWS_PER_TRANSACTION} rows per transaction, got {len(rows)}")
        if not rows:
            return FundingRateBatchReport(0, 0, (), 0, 0)

        inserted = duplicates = 0
        inserted_keys: list[FundingRateKey] = []
        conflicts: list[FundingRateConflict] = []
        commit_issued = False
        started = self._monotonic_ns()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            locked = self._monotonic_ns()
            for row in rows:
                key = row.key
                slot = (key.venue, key.symbol, key.funding_time_ms, key.rate_type.value)
                values = (row.funding_rate, row.mark_price)
                if self._conn.execute(INSERT_FUNDING_RATE, (*slot, *values, ingested_at_ms)).rowcount == 1:
                    inserted += 1
                    inserted_keys.append(key)
                    continue
                stored_values = tuple(self._conn.execute(_SELECT_STORED, slot).fetchone())
                if stored_values == values:
                    duplicates += 1
                    continue
                recorded = self._conn.execute(RECORD_CONFLICT, (*slot, *values, ingested_at_ms)).rowcount == 1
                stored_rate, stored_price = stored_values
                conflicts.append(
                    FundingRateConflict(
                        incoming=row,
                        stored=FundingRate(key, stored_rate, stored_price),
                        newly_recorded=recorded,
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
        return FundingRateBatchReport(
            inserted, duplicates, tuple(conflicts), locked - started, finished - locked, tuple(inserted_keys)
        )
