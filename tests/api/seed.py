"""Databases and rows for the API tests. Only tests write; the API under test only reads."""

import json
import sqlite3
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from quasar_market_data.collector.binance_klines import parse_klines
from quasar_market_data.collector.candle_writer import CandleWriter
from quasar_market_data.config.settings import Instrument
from quasar_market_data.domain.timed_coverage import ExpectationRule
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
SAMPLE_RULE = ExpectationRule(15_000, 60_000)
SAMPLE_UNIVERSE: tuple[Instrument, ...] = (Instrument(VENUE, SYMBOL, TIMEFRAME),)


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


def open_interest_values(time_ms: int) -> tuple[str, str]:
    """Deterministic canonical decimal text: ``open_interest`` (base asset) and ``open_interest_value`` (USDT)."""
    n = time_ms // _TF_MS
    return f"{90000 + n % 997}.125", f"{7_000_000_000 + n % 100_000}.5"


def insert_open_interest(
    conn: sqlite3.Connection,
    time_ms: int,
    *,
    symbol: str = SYMBOL,
    venue: str = VENUE,
    period: str = TIMEFRAME,
    open_interest: str | None = None,
    open_interest_value: str | None = None,
) -> None:
    """A stored open interest row; ``time_ms`` is the period END."""
    default_oi, default_value = open_interest_values(time_ms)
    conn.execute(
        "INSERT INTO open_interest (venue, symbol, period, time_ms, open_interest, open_interest_value, ingested_at_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (venue, symbol, period, time_ms, open_interest or default_oi, open_interest_value or default_value, AT),
    )


def insert_open_interest_conflict(
    conn: sqlite3.Connection,
    time_ms: int,
    variant: int,
    *,
    symbol: str = SYMBOL,
    venue: str = VENUE,
    period: str = TIMEFRAME,
) -> None:
    """Record a conflicting observation; a different ``variant`` is a different observation."""
    conn.execute(
        "INSERT INTO open_interest_conflicts (venue, symbol, period, time_ms, open_interest, open_interest_value,"
        " first_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (venue, symbol, period, time_ms, f"{91000 + variant}.5", f"{8_000_000_000 + variant}.5", AT),
    )


def make_database(
    path: Path,
    *,
    candles: Iterable[int] = (),
    unavailable: Iterable[int] = (),
    conflicts: Iterable[int] = (),
    symbol: str = SYMBOL,
    open_interest: Iterable[int] = (),
    open_interest_conflicts: Iterable[int] = (),
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
        for time_ms in open_interest:
            insert_open_interest(conn, time_ms, symbol=symbol)
        for time_ms in open_interest_conflicts:
            insert_open_interest_conflict(conn, time_ms, 0, symbol=symbol)
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
