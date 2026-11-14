"""The row-readability reads shared by the three row endpoints (contract api-v1 section 4.1, ADR-0005 P-4).

A row query joins its confirmation by the primary key of ``write_confirmations`` (no statement per row), and the
request reads the cut-over once, by key. ``domain.readability.readable_by`` turns the facts into the served fields.
"""

import sqlite3
from typing import Final, Literal

type Dataset = Literal["candles", "open_interest", "funding_rates"]

_CUTOVER: Final = "SELECT cutover_ms FROM readability_cutover WHERE id = 1"


def read_cutover(conn: sqlite3.Connection) -> int:
    """The cut-over instant, inside the caller's read transaction; a missing row is a broken database."""
    row = conn.execute(_CUTOVER).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("readability_cutover has no row")
    cutover: int = row[0]
    return cutover


def confirmation_columns(table: Dataset) -> str:
    """Two columns to append to a row query of ``table``: the stamp and whether it is confirmed."""
    return f", {table}.ingested_at_ms, w.stamp_ms IS NOT NULL"


def confirmation_join(table: Dataset) -> str:
    """The join that goes after ``FROM <table>`` of a row query (a primary-key search per row)."""
    return f" LEFT JOIN write_confirmations w ON w.table_name = '{table}' AND w.stamp_ms = {table}.ingested_at_ms"
