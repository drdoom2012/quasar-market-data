"""Fakes and fixtures shared by the fetch-once use-case and CLI tests."""

import asyncio
import json
from collections.abc import Callable, Sequence
from functools import cache
from pathlib import Path
from typing import Any

from quasar_market_data.collector.candle_writer import WriteReport
from quasar_market_data.domain.server_time import ServerTimeObservation
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.validation import ValidatedCandle

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


@cache
def _template_row() -> tuple[Any, ...]:
    return tuple(closed_rows()[0])


def kline_row(open_ms: int, tf_ms: int, **values: str) -> list[Any]:
    """The first closed fixture row moved to ``open_ms``; ``values`` replaces ``close`` (index 4) and friends."""
    row = list(_template_row())
    row[0], row[6] = open_ms, open_ms + tf_ms - 1
    for name, value in values.items():
        row[{"open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}[name]] = value
    return row


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
        *,
        honour_limit: bool = False,
        server_ahead_ms: int = 0,
        series: Sequence[int] | None = None,
    ) -> None:
        self.series = series  # open times the exchange holds; served filtered by range and limit when set
        self.honour_limit = honour_limit  # True: ``limit`` rows ending at the server's forming slot; ``rows`` unused
        self.server_ahead_ms = server_ahead_ms  # honour_limit only: the server clock is the local one plus this
        self.clock = clock
        self.rows = rows
        self.server_time_ms = server_time_ms
        self.rtt_ms = rtt_ms
        self.time_error = time_error
        self.klines_error = klines_error
        self.on_klines = on_klines
        self.time_calls = 0
        self.kline_calls: list[tuple[str, Timeframe, int]] = []
        self.kline_ranges: list[tuple[int | None, int | None]] = []
        self.closed = False

    async def fetch_server_time(self) -> ServerTimeObservation:
        self.time_calls += 1
        sent_ns = self.clock.monotonic_ns()
        self.clock.advance(self.rtt_ms)
        if self.time_error is not None:
            raise self.time_error
        server_time_ms = self.clock.wall_ms() + self.server_ahead_ms if self.honour_limit else self.server_time_ms
        return ServerTimeObservation(server_time_ms, sent_ns, self.clock.monotonic_ns(), self.clock.wall_ms())

    def limited_rows(self, limit: int, tf_ms: int) -> list[list[Any]]:
        """``limit`` rows ending at the server's forming slot, all with the closed fixture's first row values."""
        forming = (self.clock.wall_ms() + self.server_ahead_ms) // tf_ms * tf_ms
        return [kline_row(open_ms, tf_ms) for open_ms in range(forming - (limit - 1) * tf_ms, forming + tf_ms, tf_ms)]

    async def fetch_klines(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> Sequence[object]:
        self.kline_calls.append((symbol, timeframe, limit))
        self.kline_ranges.append((start_time_ms, end_time_ms))
        if self.on_klines is not None:
            self.on_klines()
        if self.klines_error is not None:
            raise self.klines_error
        if self.honour_limit:
            return self.limited_rows(limit, timeframe.milliseconds)
        if self.series is not None:
            tf_ms = timeframe.milliseconds
            lo = start_time_ms if start_time_ms is not None else 0
            hi = end_time_ms if end_time_ms is not None else 1 << 62
            return [kline_row(t, tf_ms) for t in sorted(self.series) if lo <= t <= hi][:limit]
        return self.rows

    async def close(self) -> None:
        self.closed = True


class AdvancingSleep:
    """A ``Sleep`` that advances a ``FakeClock`` at once and records the waits.

    Only for tests with at most one waiter at a time: a second sleep starting while one is in progress raises, so
    it can never silently serialise a concurrency test (use ``ManualTime`` for those).
    """

    def __init__(self, clock: FakeClock, on_sleep: Callable[[int], None] | None = None) -> None:
        self.clock = clock
        self.on_sleep = on_sleep
        self.calls: list[int] = []
        self._active = 0

    async def __call__(self, ms: int) -> None:
        if self._active:
            raise AssertionError("AdvancingSleep supports one waiter at a time; use ManualTime")
        self._active += 1
        try:
            self.calls.append(ms)
            self.clock.advance(ms)
            if self.on_sleep is not None:
                self.on_sleep(ms)
            await asyncio.sleep(0)
        finally:
            self._active -= 1


class ControlledSink:
    """A ``CandleSink`` the test controls: scripted failures per call, optional blocking inside ``write``."""

    def __init__(self, raises: Sequence[BaseException | None] = (), *, block: bool = False) -> None:
        self.raises = list(raises)
        self.block = block
        self.calls: list[tuple[ValidatedCandle, ...]] = []
        self.write_started = asyncio.Event()
        self.release = asyncio.Event()

    async def write(self, candles: Sequence[ValidatedCandle], *, ingested_at_ms: int) -> WriteReport:
        index = len(self.calls)
        self.calls.append(tuple(candles))
        self.write_started.set()
        if self.block:
            await self.release.wait()
        if index < len(self.raises) and self.raises[index] is not None:
            raise self.raises[index]  # type: ignore[misc]
        return WriteReport(len(candles), 0, ())

    @property
    def write_calls(self) -> int:
        return len(self.calls)
