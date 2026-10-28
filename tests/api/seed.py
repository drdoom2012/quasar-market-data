"""Databases and rows for the API tests. Only tests write; the API under test only reads."""

import json
import sqlite3
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from quasar_market_data.collector.binance_klines import parse_klines
from quasar_market_data.collector.candle_writer import CandleWriter
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.validation import require_valid
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate

VENUE = "binance-usdm"
SYMBOL = "BTC/USDT:USDT"
TIMEFRAME = "5m"
AT = 1_792_677_000_000
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "binance" / "btcusdt_5m_closed_raw.json"
_TF_MS = 300_000


def candle_values(open_time_ms: int) -> tuple[str, str, str, str, str, str | None]:
    """Deterministic canonical decimal text: open, high, low, close, volume, turnover (None on odd slots)."""
    n = open_time_ms // _TF_MS
    turnover = None if n % 2 == 1 else f"{(n % 100 + 1) * 80000}.5"
    return (
        f"{80000 + n % 997}.5",
        f"{80100 + n % 997}.25",
        f"{79900 + n % 997}.75",
        f"{80000 + n % 5000}",
        f"{n % 100 + 1}.125",
        turnover,
    )


def insert_candle(
    conn: sqlite3.Connection,
    open_time_ms: int,
    *,
    symbol: str = SYMBOL,
    venue: str = VENUE,
    timeframe: str = TIMEFRAME,
    close: str | None = None,
    high: str | None = None,
    low: str | None = None,
) -> None:
    values = candle_values(open_time_ms)
    if close is not None:
        open_, volume, turnover = values[0], values[4], values[5]
        derived_high = max(open_, close, key=Decimal)
        derived_low = min(open_, close, key=Decimal)
        values = (open_, high or derived_high, low or derived_low, close, volume, turnover)
    conn.execute(
        "INSERT INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover,"
        " ingested_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (venue, symbol, timeframe, open_time_ms, *values, AT),
    )


def insert_conflict(
    conn: sqlite3.Connection,
    open_time_ms: int,
    variant: int,
    *,
    symbol: str = SYMBOL,
    venue: str = VENUE,
    timeframe: str = TIMEFRAME,
) -> None:
    """Record a conflicting observation; a different ``variant`` is a different observation."""
    open_, high, low, _close, volume, turnover = candle_values(open_time_ms)
    conn.execute(
        "INSERT INTO candle_conflicts (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
        " turnover, first_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (venue, symbol, timeframe, open_time_ms, open_, high, low, f"{90000 + variant}.5", volume, turnover, AT),
    )


def insert_unavailable(
    conn: sqlite3.Connection, open_time_ms: int, *, symbol: str = SYMBOL, venue: str = VENUE, timeframe: str = TIMEFRAME
) -> None:
    conn.execute(
        "INSERT INTO unavailable_candles (venue, symbol, timeframe, open_time_ms, reason, recorded_at_ms)"
        " VALUES (?, ?, ?, ?, 'EXCHANGE_NO_DATA', ?)",
        (venue, symbol, timeframe, open_time_ms, AT),
    )


def make_database(
    path: Path,
    *,
    candles: Iterable[int] = (),
    unavailable: Iterable[int] = (),
    conflicts: Iterable[int] = (),
    symbol: str = SYMBOL,
) -> Path:
    conn = open_writer(path)
    try:
        migrate(conn, applied_at_ms=AT)
        for open_time_ms in candles:
            insert_candle(conn, open_time_ms, symbol=symbol)
        for open_time_ms in unavailable:
            insert_unavailable(conn, open_time_ms, symbol=symbol)
        for open_time_ms in conflicts:
            insert_conflict(conn, open_time_ms, 0, symbol=symbol)
    finally:
        conn.close()
    return path


def make_database_from_fixture(path: Path) -> Path:
    """The 100 closed candles of the committed Binance fixture, through the Collector's own writer."""
    rows = json.loads(FIXTURE.read_text())
    candles = [require_valid(candle) for candle in parse_klines(rows, SYMBOL, Timeframe(TIMEFRAME))]
    conn = open_writer(path)
    try:
        migrate(conn, applied_at_ms=AT)
        writer = CandleWriter(conn)
        writer.write(candles, ingested_at_ms=AT)
    finally:
        conn.close()
    return path
