"""Stamping and confirming write transactions, shared by the three writers (ADR-0005 P-2, P-3).

Every write transaction that stores rows is registered under a stamp taken after the write lock is held. When its
``COMMIT`` ends within ``READABLE_BOUND_MS`` of the stamp (monotonic clock), the transaction is confirmed in a short
transaction of its own. Without a confirmation the API serves its rows as unconfirmed, so every failure here fails
closed: a slow commit, a stop between commit and confirmation, a failed confirmation write.

A failed confirmation is kept in memory and inserted inside the writer's next committed data transaction (a data
transaction that is rolled back carries it on); it is lost when the process stops. A failed confirmation is never a
write failure: the data is committed.

Used only on the writer's own thread, inside the writer's own transactions.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from quasar_market_data.domain.readability import READABLE_BOUND_MS

COMMIT_MARGIN_MS: Final = 500  # M: the part of B reserved for COMMIT; not part of the contract
CANDLE_STAMP_ATTEMPTS: Final = 3
EXTENSION_STAMP_ATTEMPTS: Final = 1  # one data transaction plus its confirmation per job (ADR-0005 P-6)
_NS_PER_MS: Final = 1_000_000

type Dataset = Literal["candles", "open_interest", "funding_rates"]

_REGISTER: Final = "INSERT INTO write_transactions (table_name, stamp_ms) VALUES (?, ?)"
_CONFIRM: Final = (
    "INSERT INTO write_confirmations (table_name, stamp_ms, confirmed_at_ms) VALUES (?, ?, ?) ON CONFLICT DO NOTHING"
)


@dataclass(frozen=True)
class Stamp:
    stamp_ms: int
    t0_ns: int  # monotonic, read after the write lock was held


class TransactionStamper:
    def __init__(
        self,
        conn: sqlite3.Connection,
        dataset: Dataset,
        *,
        wall_ms: Callable[[], int],
        monotonic_ns: Callable[[], int],
        logger: logging.Logger | None = None,
    ) -> None:
        self._conn = conn
        self._dataset = dataset
        self._wall_ms = wall_ms
        self._monotonic_ns = monotonic_ns
        self._logger = logger or logging.getLogger(__name__)
        row = conn.execute("SELECT cutover_ms FROM readability_cutover WHERE id = 1").fetchone()
        if row is None:
            raise sqlite3.DatabaseError("readability_cutover has no row")
        self._cutover_ms: int = row[0]
        self._pending: list[int] = []

    @property
    def pending(self) -> tuple[int, ...]:
        return tuple(self._pending)

    def register(self) -> Stamp:
        """Inside ``BEGIN IMMEDIATE``: take and register the stamp, and insert the pending confirmations."""
        t0 = self._monotonic_ns()
        wall = self._wall_ms()
        last = self._conn.execute(
            "SELECT max(stamp_ms) FROM write_transactions WHERE table_name = ?", (self._dataset,)
        ).fetchone()[0]
        stamp = max(wall, self._cutover_ms + 1, (last or 0) + 1)
        if stamp > wall + READABLE_BOUND_MS:
            self._logger.warning(
                "readability stamp ahead of the wall clock: table=%s stamp_ms=%d wall_ms=%d (a forward clock step "
                "or a stamp of another process delays availability)",
                self._dataset,
                stamp,
                wall,
            )
        self._conn.execute(_REGISTER, (self._dataset, stamp))
        for pending in self._pending:
            self._conn.execute(_CONFIRM, (self._dataset, pending, wall))
        return Stamp(stamp, t0)

    def expired(self, stamp: Stamp) -> bool:
        """Whether the transaction is too old to be committed within the bound (restamp instead)."""
        return self._monotonic_ns() - stamp.t0_ns > (READABLE_BOUND_MS - COMMIT_MARGIN_MS) * _NS_PER_MS

    def committed(self, stamp: Stamp, describe: Callable[[], str], roll_back: Callable[[], None]) -> bool:
        """After a successful ``COMMIT``: confirm when it ended within the bound. Never raises an ``sqlite3.Error``.

        Returns whether the transaction is confirmed now. ``roll_back`` is the writer's own rollback, which makes the
        writer unusable when the rollback of a failed confirmation cannot be confirmed.
        """
        self._pending.clear()  # the pending confirmations were inserted by the committed transaction
        elapsed_ns = self._monotonic_ns() - stamp.t0_ns
        if elapsed_ns > READABLE_BOUND_MS * _NS_PER_MS:
            self._logger.error(
                "readability bound exceeded, rows stay unconfirmed: table=%s stamp_ms=%d duration_ms=%d %s",
                self._dataset,
                stamp.stamp_ms,
                elapsed_ns // _NS_PER_MS,
                describe(),
            )
            return False
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(_CONFIRM, (self._dataset, stamp.stamp_ms, self._wall_ms()))
            self._conn.execute("COMMIT")
        except sqlite3.Error as error:
            roll_back()
            self._pending.append(stamp.stamp_ms)
            self._logger.warning(
                "readability confirmation not written, kept for the next transaction: table=%s stamp_ms=%d: %s",
                self._dataset,
                stamp.stamp_ms,
                error,
            )
            return False
        return True
