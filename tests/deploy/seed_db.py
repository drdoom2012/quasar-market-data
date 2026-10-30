"""Seed a small valid database, run inside the collector image from stdin: ``python - DB_PATH`` (S21 spec 5.6).

It uses only the package (``tests/`` is not in the image) and never calls the exchange. The row shape copies
``tests/collector/fetch_fakes.kline_row`` (the first closed fixture row, moved in time).
"""

import sys
from pathlib import Path

from quasar_market_data.collector.binance_klines import parse_klines
from quasar_market_data.collector.candle_writer import CandleWriter
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.validation import require_valid
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate

SEED_SYMBOL = "BTC/USDT:USDT"
SEED_START_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
SEED_COUNT = 12
SEED_AT_MS = SEED_START_MS + 86_400_000
FIVE_MINUTES = Timeframe("5m")
STEP_MS = 300_000


def row(open_ms: int) -> list[object]:
    return [
        open_ms,
        "82855.40",
        "82866.10",
        "82768.10",
        "82768.20",
        "369.221",
        open_ms + STEP_MS - 1,
        "30587379.87590",
        5893,
        "123.613",
        "10241743.16440",
        "0",
    ]


def seed(db: Path) -> None:
    conn = open_writer(db)
    try:
        migrate(conn, applied_at_ms=SEED_AT_MS)
        rows = [row(SEED_START_MS + i * STEP_MS) for i in range(SEED_COUNT)]
        candles = [require_valid(candle) for candle in parse_klines(rows, SEED_SYMBOL, FIVE_MINUTES)]
        CandleWriter(conn).write(candles, ingested_at_ms=SEED_AT_MS)
    finally:
        conn.close()


if __name__ == "__main__":
    seed(Path(sys.argv[1]))
