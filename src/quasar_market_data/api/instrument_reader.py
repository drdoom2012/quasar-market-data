"""Read-only facts for ``GET /v1/instruments`` (contract api-v1 section 6.4).

One ``MAX(open_time_ms)`` seek per configured instrument on the primary key
``(venue, symbol, timeframe, open_time_ms)``, all inside one ``BEGIN`` so the schema check and every read see one
snapshot. Stored series that are not configured are never read (INV-067); nothing is scanned.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from quasar_market_data.config.settings import Instrument
from quasar_market_data.storage.schema import verify_schema

_NEWEST = "SELECT MAX(open_time_ms) FROM candles WHERE venue = ? AND symbol = ? AND timeframe = ?"


@dataclass(frozen=True)
class InstrumentNewest:
    instrument: Instrument
    newest_open_time_ms: int | None


def read_instrument_newest(conn: sqlite3.Connection, universe: Sequence[Instrument]) -> tuple[InstrumentNewest, ...]:
    """The newest stored open time per instrument (``None`` when nothing is stored), in the order given."""
    conn.execute("BEGIN")
    try:
        verify_schema(conn)
        return tuple(
            InstrumentNewest(
                instrument,
                conn.execute(_NEWEST, (instrument.venue, instrument.symbol, instrument.timeframe)).fetchone()[0],
            )
            for instrument in universe
        )
    finally:
        _rollback(conn)


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass  # a broken connection must not mask the original error; the caller closes it
