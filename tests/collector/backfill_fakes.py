"""A test environment for the backfill worker: a range-serving source on ``ManualTime``, a real writer on a temp db."""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.backfill import BackfillConfig, Backfiller, SliceReport
from quasar_market_data.collector.binance_klines import VENUE_BINANCE_USDM, parse_klines
from quasar_market_data.collector.candle_writer import CandleWriter, WriteReport
from quasar_market_data.collector.collection_loop import IdleWindow, LoopConfig
from quasar_market_data.collector.fetch_once import CandleSink
from quasar_market_data.domain.server_time import ServerTimeObservation
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.validation import ValidatedCandle, require_valid
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.fetch_fakes import kline_row
from tests.collector.manual_time import NS_PER_MS, ManualTime

FIVE = Timeframe("5m")
TF = FIVE.milliseconds
DAY = 86_400_000
AT = 1_792_677_000_000
SYMBOL = "A"
ROOM_MS = 279_000  # the idle time ManualTime has at its start


@dataclass(frozen=True)
class Request:
    symbol: str
    start: int | None
    end: int | None
    limit: int
    mono_ns: int


class RangeSource:
    """Serves ``series[symbol]`` filtered by the requested range and limit; the server clock is monotonic-based."""

    def __init__(self, t: ManualTime, series: dict[str, set[int]] | None = None, *, skew_ms: int = 1_000) -> None:
        self.t = t
        self.clock: Any = t  # stamps the server-time observation; a test may swap in a clock of its own
        self.series = series if series is not None else {}
        self.requests: list[Request] = []
        self.time_calls = 0
        self.errors: dict[str, BaseException] = {}
        self.rtt_ms = 0
        self.time_error: BaseException | None = None
        self.override: Callable[[str, int, int, int], list[list[Any]]] | None = None
        self.row_values: dict[int, dict[str, str]] = {}
        self.on_klines: Callable[[Request], Awaitable[None]] | None = None
        self._server_base = t.wall_ms() + skew_ms
        self._mono_base = t.monotonic_ns()

    def server_ms(self) -> int:
        return self._server_base + (self.t.monotonic_ns() - self._mono_base) // NS_PER_MS

    async def fetch_server_time(self) -> ServerTimeObservation:
        self.time_calls += 1
        if self.time_error is not None:
            raise self.time_error
        value, sent_ns = self.server_ms(), self.clock.monotonic_ns()
        self.t._mono_ns += self.rtt_ms * NS_PER_MS
        self.t._wall_ms += self.rtt_ms
        return ServerTimeObservation(value, sent_ns, self.clock.monotonic_ns(), self.clock.wall_ms())

    async def fetch_klines(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> Sequence[object]:
        request = Request(symbol, start_time_ms, end_time_ms, limit, self.t.monotonic_ns())
        self.requests.append(request)
        if self.on_klines is not None:
            await self.on_klines(request)
        if symbol in self.errors:
            raise self.errors[symbol]
        lo = start_time_ms if start_time_ms is not None else 0
        hi = end_time_ms if end_time_ms is not None else 1 << 62
        if self.override is not None:
            return self.override(symbol, lo, hi, limit)
        times = [o for o in sorted(self.series.get(symbol, ())) if lo <= o <= hi][:limit]
        return [kline_row(o, timeframe.milliseconds, **self.row_values.get(o, {})) for o in times]

    async def close(self) -> None:
        pass


class RecordingSink:
    """Wraps the real writer; counts writes, can run a hook after a write or fake a report."""

    def __init__(self, inner: CandleSink) -> None:
        self.inner = inner
        self.writes = 0
        self.after_write: Callable[[int], Awaitable[None]] | None = None
        self.raises: dict[int, BaseException] = {}
        self.raises_after_commit: dict[int, BaseException] = {}
        self.fake_report: WriteReport | None = None

    async def write(self, candles: Sequence[ValidatedCandle], *, ingested_at_ms: int) -> WriteReport:
        self.writes += 1
        if self.writes in self.raises:
            raise self.raises[self.writes]
        if self.fake_report is not None:
            return self.fake_report
        report = await self.inner.write(candles, ingested_at_ms=ingested_at_ms)
        if self.writes in self.raises_after_commit:
            raise self.raises_after_commit[self.writes]  # committed, but the caller learns nothing
        if self.after_write is not None:
            await self.after_write(self.writes)
        return report


class Env:
    def __init__(self, tmp_path: Path, symbols: tuple[str, ...] = (SYMBOL,), days: int = 1) -> None:
        self.db = tmp_path / "market.db"
        conn = open_writer(self.db)
        migrate(conn, applied_at_ms=AT)
        conn.close()
        self.t = ManualTime()
        self.source = RangeSource(self.t)
        self.symbols = symbols
        self.days = days
        self.stop = asyncio.Event()
        self.slices: list[SliceReport] = []
        self.suspended: list[tuple[int, int]] = []
        self.loop_config = LoopConfig(symbols=symbols, timeframe=FIVE, limit=100)
        self.writer: AsyncCandleWriter
        self.sink: RecordingSink
        now = self.t.wall_ms()
        self.hs = now // DAY * DAY - days * DAY
        self.forming = now // TF * TF

    async def open(self) -> "Env":
        self.writer = await AsyncCandleWriter.open(self.db)
        self.sink = RecordingSink(self.writer)
        return self

    async def close(self) -> None:
        await self.writer.close()
        conn = sqlite3.connect(self.db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM unavailable_candles").fetchone()[0] == 0  # INV-054
        finally:
            conn.close()

    def slot(self, i: int) -> int:
        return self.hs + i * TF

    def backfiller(self, **kwargs: Any) -> Backfiller:
        return Backfiller(
            BackfillConfig(self.days),
            self.loop_config,
            self.db,
            kwargs.get("clock", self.t),
            self.t.sleep,
            self.slices.append,
            lambda consecutive, overrun: self.suspended.append((consecutive, overrun)),
        )

    def window(self, room_ms: int = ROOM_MS, *, deadline_in_ms: int | None = None, source: Any = None) -> IdleWindow:
        due = self.t.monotonic_ns() + room_ms * NS_PER_MS
        deadline = (
            due - 15_000 * NS_PER_MS if deadline_in_ms is None else self.t.monotonic_ns() + deadline_in_ms * NS_PER_MS
        )
        return IdleWindow(due, deadline, source or self.source, lambda symbol: self.sink, self.stop)

    def store(self, times: Sequence[int], symbol: str = SYMBOL, *, timeframe: Timeframe = FIVE, **values: str) -> None:
        rows = [kline_row(o, timeframe.milliseconds, **values) for o in sorted(times)]
        candles = [require_valid(c) for c in parse_klines(rows, symbol, timeframe)]
        conn = open_writer(self.db)
        try:
            CandleWriter(conn).write(candles, ingested_at_ms=AT)
        finally:
            conn.close()

    def stored(self, symbol: str = SYMBOL) -> list[int]:
        return [r[0] for r in self.query("SELECT open_time_ms FROM candles WHERE symbol = ? ORDER BY 1", (symbol,))]

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def counts(self) -> tuple[int, int, int]:
        return tuple(
            self.query(f"SELECT COUNT(*) FROM {table}")[0][0]
            for table in ("candles", "candle_conflicts", "unavailable_candles")
        )


VENUE = VENUE_BINANCE_USDM
