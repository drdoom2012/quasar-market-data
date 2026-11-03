"""Fakes for the open interest task tests: a source that answers like Binance on ``ManualTime``, and a harness."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quasar_market_data.collector.async_open_interest_writer import AsyncOpenInterestWriter, OpenInterestWriteReport
from quasar_market_data.collector.candle_write_activity import CandleWriteActivity
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.open_interest_budget import OpenInterestBudget
from quasar_market_data.collector.open_interest_measurements import OpenInterestMeasurements
from quasar_market_data.collector.open_interest_task import OpenInterestTaskConfig, run_open_interest_task
from quasar_market_data.collector.open_interest_writer import OpenInterestWriter
from quasar_market_data.domain.open_interest import OpenInterest, OpenInterestKey
from quasar_market_data.domain.server_time import ServerTimeObservation
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.manual_time import NS_PER_MS, ManualTime

P5 = Timeframe("5m")
STEP = P5.milliseconds
VENUE = "binance-usdm"
SKEW_MS = 1_000


def symbols_of(count: int) -> tuple[str, ...]:
    return tuple(f"S{index:03d}/USDT:USDT" for index in range(count))


def values_for(time_ms: int) -> tuple[str, str]:
    base = 1000 + time_ms // STEP % 1000
    return f"{base}.5", f"{base * 100}.25"


@dataclass
class HistCall:
    symbol: str
    start_ms: int
    end_ms: int
    as_of_ms: int
    at_ns: int


class ManualOiSource:
    """Rows exist for a slot once ``slot + publish_lag_ms`` is past the server time; ``never`` slots are never served."""

    def __init__(
        self, t: ManualTime, *, budget: OpenInterestBudget | None = None, publish_lag_ms: int = 60_000
    ) -> None:
        self.t = t
        self.budget = budget
        self.publish_lag_ms = publish_lag_ms
        self.skew_ms = SKEW_MS
        self.rtt_ms = 0
        self.hist_calls: list[HistCall] = []
        self.time_calls: list[int] = []  # monotonic ns of every time request
        self.never: set[tuple[str, int]] = set()  # (symbol, slot) Binance answers without a row
        self.late: dict[tuple[str, int], int] = {}  # (symbol, slot) -> server ms from which it is published
        self.errors: list[BaseException | None] = []  # one entry per hist call, consumed in order (None: succeed)
        self.symbol_errors: dict[str, BaseException] = {}  # raised on every hist call of the symbol
        self.time_error: BaseException | None = None
        self.hold: asyncio.Event | None = None  # when set (an Event), hist calls wait for it
        self.hang_hist = False
        self.hang_after_calls: int | None = None  # hist calls beyond this number hang for ever
        self.hist_delay_ms = 0
        self.hang_time = False
        self.active = 0
        self.max_active = 0
        self.closed = False
        self.values: Callable[[int], tuple[str, str]] = values_for
        self.value_override: dict[tuple[str, int], tuple[str, str]] = {}
        self.page_error: tuple[str, BaseException] | None = None  # raised for the symbol's catch-up pages only
        self.page_script: list[bool] = []  # per catch-up page call, in order: True fails it (then pages succeed)
        self.fail_first = False  # the first request of every (symbol, range end) raises a SourceError
        self._seen: set[tuple[str, int]] = set()
        self.mutate_rows: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None

    def market_id(self, symbol: str) -> str:
        return symbol.split("/")[0] + "USDT"

    def server_now_ms(self) -> int:
        return self.t.wall_ms() + self.skew_ms

    async def _count(self) -> None:
        if self.budget is not None:
            await self.budget.acquire()

    async def fetch_server_time(self) -> ServerTimeObservation:
        await self._count()
        self.time_calls.append(self.t.monotonic_ns())
        sent_ns = self.t.monotonic_ns()
        if self.hang_time:
            await asyncio.Event().wait()
        if self.rtt_ms:
            await self.t.sleep(self.rtt_ms)
        if self.time_error is not None:
            raise self.time_error
        return ServerTimeObservation(self.server_now_ms(), sent_ns, self.t.monotonic_ns(), self.t.wall_ms())

    async def fetch_open_interest_hist(
        self, symbol: str, start_time_ms: int, end_time_ms: int, *, as_of_ms: int
    ) -> Sequence[object]:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await self._count()
            self.hist_calls.append(HistCall(symbol, start_time_ms, end_time_ms, as_of_ms, self.t.monotonic_ns()))
            if self.hang_hist or (self.hang_after_calls is not None and len(self.hist_calls) > self.hang_after_calls):
                await asyncio.Event().wait()
            if self.hist_delay_ms and end_time_ms - start_time_ms != 2 * STEP:  # catch-up pages only
                await self.t.sleep(self.hist_delay_ms)
            if self.hold is not None:
                await self.hold.wait()
            if self.fail_first and (symbol, end_time_ms) not in self._seen:
                self._seen.add((symbol, end_time_ms))
                raise SourceError("scripted first-attempt failure")
            if self.page_script and end_time_ms - start_time_ms != 2 * STEP and self.page_script.pop(0):
                raise SourceError("scripted page failure")
            if self.page_error is not None and symbol == self.page_error[0] and end_time_ms - start_time_ms != 2 * STEP:
                raise self.page_error[1]
            if self.errors:
                error = self.errors.pop(0)
                if error is not None:
                    raise error
            if symbol in self.symbol_errors:
                raise self.symbol_errors[symbol]
            rows: list[dict[str, Any]] = []
            for end in range(-(-start_time_ms // STEP) * STEP, end_time_ms + 1, STEP):
                key = (symbol, end)
                if key in self.never:
                    continue
                published_from = self.late.get(key, end + self.publish_lag_ms)
                if published_from > self.server_now_ms():
                    continue
                oi, value = self.value_override.get(key) or self.values(end)
                rows.append(
                    {
                        "symbol": self.market_id(symbol),
                        "sumOpenInterest": oi,
                        "sumOpenInterestValue": value,
                        "CMCCirculatingSupply": "1.0",
                        "timestamp": end,
                    }
                )
            if self.mutate_rows is not None:
                rows = self.mutate_rows(rows)
            return rows[-500:]
        finally:
            self.active -= 1

    async def close(self) -> None:
        self.closed = True


class RecordingWriter:
    """An ``OpenInterestSink`` that records instead of storing; ``errors`` are raised in order (``None``: succeed)."""

    def __init__(self, t: ManualTime) -> None:
        self.t = t
        self.writes: list[tuple[tuple[OpenInterest, ...], int]] = []
        self.errors: list[BaseException | None] = []
        self.stored: dict[tuple[str, int], int] = {}  # (symbol, time_ms) -> ingested_at_ms of the first write

    async def write(self, rows: Sequence[OpenInterest], *, ingested_at_ms: int) -> OpenInterestWriteReport:
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        self.writes.append((tuple(rows), ingested_at_ms))
        new = 0
        for row in rows:
            key = (row.key.symbol, row.key.time_ms)
            if key not in self.stored:
                self.stored[key] = ingested_at_ms
                new += 1
        return OpenInterestWriteReport(new, len(rows) - new, (), (1,), (1_000_000,), 0, 0)


def migrated_db(tmp_path: Path, at_ms: int, name: str = "market.db") -> Path:
    path = tmp_path / name
    conn = open_writer(path)
    migrate(conn, applied_at_ms=at_ms)
    conn.close()
    return path


def prefill(
    db: Path,
    symbols: Sequence[str],
    slots: Sequence[int],
    at_ms: int,
    *,
    values: Callable[[int], tuple[str, str]] = values_for,
) -> None:
    """Store ``slots`` for every symbol through the sync writer, 100 rows per transaction."""
    conn = open_writer(db)
    writer = OpenInterestWriter(conn)
    try:
        for symbol in symbols:
            rows = [OpenInterest(OpenInterestKey(VENUE, symbol, P5, slot), *values(slot)) for slot in slots]
            for start in range(0, len(rows), 100):
                writer.write_transaction(rows[start : start + 100], ingested_at_ms=at_ms)
    finally:
        writer.close()


def stored_times(db: Path, symbol: str | None = None) -> list[int]:
    conn = open_reader(db)
    try:
        sql = "SELECT time_ms FROM open_interest"
        args: tuple[str, ...] = ()
        if symbol is not None:
            sql += " WHERE symbol = ?"
            args = (symbol,)
        return [row[0] for row in conn.execute(sql + " ORDER BY symbol, time_ms", args)]
    finally:
        conn.close()


def table_rows(db: Path, table: str = "open_interest") -> list[tuple[Any, ...]]:
    conn = open_reader(db)
    try:
        return conn.execute(f"SELECT * FROM {table} ORDER BY 2, 4").fetchall()
    finally:
        conn.close()


@dataclass
class Rig:
    """A task under test on one ``ManualTime``; ``start`` runs it, ``stop_and_wait`` ends it."""

    t: ManualTime
    config: OpenInterestTaskConfig
    source: ManualOiSource
    writer: Any
    budget: OpenInterestBudget
    activity: CandleWriteActivity
    db: Path
    measurements: OpenInterestMeasurements = field(default_factory=OpenInterestMeasurements)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    lines: list[str] = field(default_factory=list)
    task: "asyncio.Task[None] | None" = None

    def start(self) -> None:
        self.task = asyncio.create_task(
            run_open_interest_task(
                self.source,
                self.writer,
                self.db,
                self.config,
                self.budget,
                self.activity,
                self.t,
                self.t.sleep,
                self.stop,
                self.lines.append,
                self.measurements,
            )
        )

    async def stop_and_wait(self) -> None:
        self.stop.set()
        assert self.task is not None
        await self.t.settle()
        await self.task

    def align_to_next_wake(self) -> None:
        """Before ``start``: move both clocks to the next round wake, so the first round is on time, not late."""
        offset = self.config.round_offset_ms
        wake = ((self.t.wall_ms() - offset) // STEP + 1) * STEP + offset
        delta = wake - self.t.wall_ms()
        self.t._wall_ms += delta
        self.t._mono_ns += delta * NS_PER_MS

    def lines_with(self, prefix: str) -> list[str]:
        return [line for line in self.lines if line.startswith(prefix)]

    async def until_lines(self, prefix: str, count: int) -> None:
        for _ in range(2_000):
            if len(self.lines_with(prefix)) >= count:
                return
            await self.t.settle()
            await asyncio.sleep(0)
        raise AssertionError(f"expected {count} {prefix!r} lines, have {self.lines_with(prefix)}")

    async def advance_to_next_wake(self) -> None:
        """Advance fake time to the next round's wake: the last round's boundary + 5 min + the offset.

        Not "the earliest waiter": while a catch-up window is still running that waiter is the window's deadline.
        """
        await self.t.settle()
        last = int(self.lines_with("oi-round")[-1].split()[1].split("=")[1])
        wake = last + STEP + self.config.round_offset_ms
        await self.t.advance(wake - self.t.wall_ms())


async def make_rig(
    tmp_path: Path,
    *,
    symbols: Sequence[str] = ("BTC/USDT:USDT", "ETH/USDT:USDT"),
    budget_per_5min: int = 400,
    publication_delay_ms: int = 15_000,
    round_offset_ms: int = 200_000,
    real_writer: bool = True,
    sink: Any = None,
    recording: bool = False,
    publish_lag_ms: int = 60_000,
    count_budget_in_source: bool = False,
    db: Path | None = None,
    connect: Any = None,
    t: ManualTime | None = None,
) -> Rig:
    t = t or ManualTime()
    db = db or migrated_db(tmp_path, t.wall_ms())
    activity = CandleWriteActivity()
    budget = OpenInterestBudget(budget_per_5min, t, t.sleep)
    source = ManualOiSource(t, budget=budget if count_budget_in_source else None, publish_lag_ms=publish_lag_ms)
    if recording:
        writer: Any = RecordingWriter(t)
    elif sink is not None:
        writer = sink
    else:
        assert real_writer
        writer = await AsyncOpenInterestWriter.open(db, activity=activity, connect=connect)
        t.track_executor(writer._executor)
    config = OpenInterestTaskConfig(
        venue=VENUE,
        symbols=tuple(symbols),
        period=P5,
        publication_delay_ms=publication_delay_ms,
        round_offset_ms=round_offset_ms,
        budget_per_5min=budget_per_5min,
    )
    return Rig(t, config, source, writer, budget, activity, db)


def ns(ms: int) -> int:
    return ms * NS_PER_MS
