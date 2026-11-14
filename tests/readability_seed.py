"""Registration for test and benchmark seeders that insert rows directly (ADR-0005 P-2).

Since schema 4 every row and conflict record must belong to a registered write transaction with a stamp above the
cut-over. A seeder that writes plain SQL registers its stamp first; ``confirmed`` also stores the confirmation, so
the API serves the rows as confirmed (``readable_by_unconfirmed: false``).
"""

import sqlite3
from typing import Final

DATASETS: Final = ("candles", "open_interest", "funding_rates")


def register(conn: sqlite3.Connection, dataset: str, stamp: int, *, confirmed: bool = True) -> None:
    """Register the write transaction ``stamp`` of ``dataset`` (idempotent) and, unless told not to, confirm it."""
    conn.execute(
        "INSERT INTO write_transactions (table_name, stamp_ms) VALUES (?, ?) ON CONFLICT DO NOTHING", (dataset, stamp)
    )
    if confirmed:
        conn.execute(
            "INSERT INTO write_confirmations (table_name, stamp_ms, confirmed_at_ms) VALUES (?, ?, ?)"
            " ON CONFLICT DO NOTHING",
            (dataset, stamp, stamp),
        )


def register_all(conn: sqlite3.Connection, stamp: int, *, confirmed: bool = True) -> None:
    """``register`` for the three datasets."""
    for dataset in DATASETS:
        register(conn, dataset, stamp, confirmed=confirmed)
