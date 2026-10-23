"""Fakes and fixtures shared by the fetch-once use-case and CLI tests."""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from quasar_market_data.domain.timeframe import Timeframe

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "binance"
SYMBOL = "BTC/USDT:USDT"
FIVE_MINUTES = Timeframe("5m")
FORMING_SERVER_TIME_MS = 1792677035725  # recorded server time of the forming fixture
ROW0_END_MS = 1792677000000  # end of the closed row of the forming fixture
WALL_START_MS = 1792677036000
MONO_START_NS = 18_053_000_000_000
NS_PER_MS = 1_000_000


def forming_rows() -> list[list[Any]]:
    rows: list[list[Any]] = json.loads((FIXTURES / "btcusdt_5m_forming_raw.json").read_text())
    return rows


def closed_rows() -> list[list[Any]]:
    rows: list[list[Any]] = json.loads((FIXTURES / "btcusdt_5m_closed_raw.json").read_text())
    return rows


class FakeClock:
    def __init__(self) -> None:
        self._mono_ns = MONO_START_NS
        self._wall_ms = WALL_START_MS

    def advance(self, ms: int) -> None:
        self._mono_ns += ms * NS_PER_MS
        self._wall_ms += ms

    def monotonic_ns(self) -> int:
        return self._mono_ns

    def wall_ms(self) -> int:
        return self._wall_ms


class FakeSource:
    def __init__(
        self,
        clock: FakeClock,
        rows: Sequence[object],
        server_time_ms: int = FORMING_SERVER_TIME_MS,
        rtt_ms: int = 270,
        time_error: Exception | None = None,
        klines_error: BaseException | None = None,
        on_klines: Callable[[], None] | None = None,
    ) -> None:
        self.clock = clock
        self.rows = rows
        self.server_time_ms = server_time_ms
        self.rtt_ms = rtt_ms
        self.time_error = time_error
        self.klines_error = klines_error
        self.on_klines = on_klines
        self.time_calls = 0
        self.kline_calls: list[tuple[str, Timeframe, int]] = []
        self.closed = False

    async def fetch_server_time_ms(self) -> int:
        self.time_calls += 1
        self.clock.advance(self.rtt_ms)
        if self.time_error is not None:
            raise self.time_error
        return self.server_time_ms

    async def fetch_klines(self, symbol: str, timeframe: Timeframe, limit: int) -> Sequence[object]:
        self.kline_calls.append((symbol, timeframe, limit))
        if self.on_klines is not None:
            self.on_klines()
        if self.klines_error is not None:
            raise self.klines_error
        return self.rows

    async def close(self) -> None:
        self.closed = True
