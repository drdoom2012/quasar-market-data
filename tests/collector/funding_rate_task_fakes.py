"""Fakes for the funding rate task tests: a CCXT-level exchange on ``ManualTime`` and a rig around the REAL source.

The scheduling scheme of the task depends on ``BinanceUsdmFundingRateSource`` reaching the limiter's group lock with
no ``await`` before it, so the task is always driven through the real source on ``FakeFundingExchange``, never through
a hand-written source that awaits first.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quasar_market_data.collector.async_funding_rate_writer import AsyncFundingRateWriter, FundingRateWriteReport
from quasar_market_data.collector.binance_usdm_funding_rate import BinanceUsdmFundingRateSource
from quasar_market_data.collector.candle_write_activity import CandleWriteActivity
from quasar_market_data.collector.extension_pause import ExtensionPause
from quasar_market_data.collector.funding_rate_limiter import FundingRateLimiter, RequestClass
from quasar_market_data.collector.funding_rate_writer import FundingRateConflict, SpanMerge
from quasar_market_data.domain.funding_rate import FundingRate, FundingRateKey
from quasar_market_data.domain.funding_rate_span import Interval, merge_span
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.manual_time import NS_PER_MS, ManualTime

HOUR = 3_600_000
VENUE = "binance-usdm"
OFFSET_MS = 3  # a settlement is reported 3 ms after the whole period


def symbols_of(count: int) -> tuple[str, ...]:
    return tuple(f"S{index:04d}/USDT:USDT" for index in range(count))


def market_id_of(symbol: str) -> str:
    return symbol.split("/")[0] + "USDT"


def market(symbol: str) -> dict[str, Any]:
    return {
        "id": market_id_of(symbol),
        "symbol": symbol,
        "swap": True,
        "linear": True,
        "settle": "USDT",
        "quote": "USDT",
        "expiry": None,
        "active": True,
    }


@dataclass
class Served:
    symbol: str
    start_ms: int
    end_ms: int
    at_ns: int
    wall_ms: int


class FakeFundingExchange:
    """Answers like Binance: the OLDEST ``limit`` published settlements of the series inside ``[start, end]``.

    A settlement ``T = k * period + 3 ms`` is published ``publish_lag_ms`` after ``T`` on the server clock, which is
    the ``ManualTime`` wall clock. ``hidden`` settlements are not published (yet): a test removes them later to model a
    late event. ``request_ms`` is the time every request takes on ``ManualTime``.
    """

    id = "binanceusdm"

    def __init__(
        self,
        t: ManualTime,
        symbols: Sequence[str],
        *,
        period_ms: int = HOUR,
        publish_lag_ms: int = 5_000,
        listing_ms: int = 0,
        request_ms: int = 0,
    ) -> None:
        self.t = t
        self.markets = {symbol: market(symbol) for symbol in symbols}
        self.urls: dict[str, Any] = {"api": {"fapiPublic": "https://fapi.binance.com/fapi/v1"}}
        self.last_response_headers: dict[str, str] = {}
        self.period_ms = period_ms
        self.periods: dict[str, int] = {}
        self.publish_lag_ms = publish_lag_ms
        self.listing_ms = listing_ms
        self.listings: dict[str, int] = {}
        self.request_ms = request_ms
        self.time_request_ms: int | None = None  # None: server-time requests take ``request_ms`` as well
        self.hidden: set[tuple[str, int]] = set()
        self.rate_override: dict[tuple[str, int], str] = {}
        self.offset_ms = OFFSET_MS
        self.errors: list[BaseException | None] = []  # one entry per funding request, consumed in order
        self.symbol_errors: dict[str, BaseException] = {}  # raised on every request of the symbol
        self.error_always: BaseException | None = None
        self.time_error: BaseException | None = None
        self.hold: asyncio.Event | None = None  # when set, funding requests wait for it
        self.hang = False
        self.hang_time = False  # when set, server-time requests never answer
        self.hang_symbols: set[str] = set()
        self.mutate: Callable[[list[dict[str, Any]]], object] | None = None
        self.served: list[Served] = []
        self.time_calls: list[int] = []
        self.active = 0
        self.max_active = 0
        self.active_by_symbol: dict[str, int] = {}
        self.max_active_by_symbol = 0
        self.closed = False

    async def load_markets(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    def period_of(self, symbol: str) -> int:
        return self.periods.get(symbol, self.period_ms)

    def settlements(self, symbol: str, start_ms: int, end_ms: int, limit: int) -> list[int]:
        period = self.period_of(symbol)
        listing = self.listings.get(symbol, self.listing_ms)
        server_now = self.t.wall_ms()
        first = -(-(max(start_ms, listing) - self.offset_ms) // period)
        times: list[int] = []
        k = first
        while len(times) < limit:
            when = k * period + self.offset_ms
            if when > end_ms or when + self.publish_lag_ms > server_now:
                break
            if (symbol, when) not in self.hidden:
                times.append(when)
            k += 1
        return times

    async def fapiPublicGetFundingRate(self, params: dict[str, Any]) -> object:
        symbol = next(s for s, m in self.markets.items() if m["id"] == params["symbol"])
        self.served.append(
            Served(symbol, params["startTime"], params["endTime"], self.t.monotonic_ns(), self.t.wall_ms())
        )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.active_by_symbol[symbol] = self.active_by_symbol.get(symbol, 0) + 1
        self.max_active_by_symbol = max(self.max_active_by_symbol, self.active_by_symbol[symbol])
        try:
            if self.request_ms:
                await self.t.sleep(self.request_ms)
            if self.hang or symbol in self.hang_symbols:
                await asyncio.Event().wait()
            if self.hold is not None:
                await self.hold.wait()
            if self.errors:
                error = self.errors.pop(0)
                if error is not None:
                    raise error
            if symbol in self.symbol_errors:
                raise self.symbol_errors[symbol]
            if self.error_always is not None:
                raise self.error_always
            times = self.settlements(symbol, params["startTime"], params["endTime"], params["limit"])
            rows = [
                {
                    "symbol": params["symbol"],
                    "fundingTime": when,
                    "fundingRate": self.rate_override.get((symbol, when), "0.00010000"),
                    "markPrice": "67123.45000000",
                    "rateType": "Regular",
                }
                for when in times
            ]
            return self.mutate(rows) if self.mutate is not None else rows
        finally:
            self.active -= 1
            self.active_by_symbol[symbol] -= 1

    async def fapiPublicGetTime(self) -> object:
        self.time_calls.append(self.t.monotonic_ns())
        delay = self.request_ms if self.time_request_ms is None else self.time_request_ms
        if delay:
            await self.t.sleep(delay)
        if self.hang_time:
            await asyncio.Event().wait()
        if self.time_error is not None:
            raise self.time_error
        return {"serverTime": self.t.wall_ms()}


class RecordingLimiter(FundingRateLimiter):
    """Records ``(request_class, monotonic ns)`` of every start the dispatcher's requests were granted."""

    def __init__(self, budget_per_5min: int, t: ManualTime) -> None:
        super().__init__(budget_per_5min, t, t.sleep)
        self._t = t
        self.grants: list[tuple[RequestClass, int]] = []

    @asynccontextmanager
    async def grant(self, request_class: RequestClass) -> AsyncIterator[None]:
        async with super().grant(request_class):
            self.grants.append((request_class, self._t.monotonic_ns()))
            yield


class InMemorySink:
    """A ``FundingRateSink`` for simulations: applies ``merge_span`` and reports like the real writer (writes are instant)."""

    def __init__(self) -> None:
        self.rows: dict[FundingRateKey, FundingRate] = {}
        self.spans: dict[str, Interval] = {}
        self.verified: dict[str, int] = {}
        self.writes = 0
        self.fail_next: list[BaseException] = []

    async def write(
        self, rows: Sequence[FundingRate], *, ingested_at_ms: int, merge: SpanMerge | None = None
    ) -> FundingRateWriteReport:
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.writes += 1
        inserted: list[FundingRateKey] = []
        conflicts: list[FundingRateConflict] = []
        duplicates = 0
        for row in rows:
            stored = self.rows.get(row.key)
            if stored is None:
                self.rows[row.key] = row
                inserted.append(row.key)
            elif (stored.funding_rate, stored.mark_price) == (row.funding_rate, row.mark_price):
                duplicates += 1
            else:
                conflicts.append(FundingRateConflict(row, stored, True))
        before = after = None
        if merge is not None:
            before = self.spans.get(merge.symbol)
            after = merge.interval if before is None else merge_span(before, merge.interval)
            self.spans[merge.symbol] = after
            self.verified[merge.symbol] = max(self.verified.get(merge.symbol, 0), merge.verified_at_ms)
        return FundingRateWriteReport(
            len(inserted),
            duplicates,
            tuple(conflicts),
            (0,) * (-(-len(rows) // 100)),
            (0,) * (-(-len(rows) // 100)),
            0,
            0,
            tuple(inserted),
            before,
            after,
        )


@dataclass
class Rig:
    t: ManualTime
    exchange: FakeFundingExchange
    source: BinanceUsdmFundingRateSource
    limiter: RecordingLimiter
    pause: ExtensionPause
    activity: CandleWriteActivity
    db: Path
    writer: Any
    closers: list[Callable[[], Any]] = field(default_factory=list)


def migrated_db(tmp_path: Path, wall_ms: int) -> Path:
    path = tmp_path / "market.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=wall_ms)
    conn.close()
    return path


async def make_rig(
    tmp_path: Path,
    *,
    symbols: Sequence[str] = ("BTC/USDT:USDT", "ETH/USDT:USDT"),
    budget_per_5min: int = 150,
    period_ms: int = HOUR,
    publish_lag_ms: int = 5_000,
    request_ms: int = 0,
    sink: Any = None,
    connect: Any = None,
    busy_timeout_ms: int | None = None,
    t: ManualTime | None = None,
    listing_ms: int = 0,
) -> Rig:
    t = t or ManualTime()
    db = migrated_db(tmp_path, t.wall_ms())
    exchange = FakeFundingExchange(
        t, symbols, period_ms=period_ms, publish_lag_ms=publish_lag_ms, request_ms=request_ms, listing_ms=listing_ms
    )
    limiter = RecordingLimiter(budget_per_5min, t)
    pause = ExtensionPause(t)
    source = await BinanceUsdmFundingRateSource.open(limiter, pause, t, exchange)
    activity = CandleWriteActivity()
    if sink is not None:
        writer = sink
    else:
        kwargs: dict[str, Any] = {"activity": activity, "connect": connect}
        if busy_timeout_ms is not None:
            kwargs["busy_timeout_ms"] = busy_timeout_ms
        writer = await AsyncFundingRateWriter.open(db, **kwargs)
        t.track_executor(writer._executor)
    return Rig(t, exchange, source, limiter, pause, activity, db, writer)


def ns(ms: int) -> int:
    return ms * NS_PER_MS


# ---- a harness around the REAL task ----------------------------------------------------------------------------

from quasar_market_data.collector.funding_rate_measurements import FundingRateMeasurements
from quasar_market_data.collector.funding_rate_task import FundingRateTaskConfig, run_funding_rate_task


@dataclass
class Harness:
    rig: Rig
    config: FundingRateTaskConfig
    lines: list[str]
    stop: asyncio.Event
    measurements: FundingRateMeasurements
    task: "asyncio.Task[None]"

    @property
    def t(self) -> ManualTime:
        return self.rig.t

    def lines_starting(self, prefix: str) -> list[str]:
        return [line for line in self.lines if line.startswith(prefix)]

    async def finish(self) -> None:
        self.stop.set()
        await self.t.settle()
        await asyncio.wait({self.task}, timeout=5)
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def run_exact(self, ms: int) -> None:
        """Advance exactly ``ms`` by jumping from one registered timer to the next, draining the loop at each stop.

        ``ManualTime.settle`` gives a woken task 100 loop hops; a dispatcher that starts a group of 25 requests needs
        more, so a coarse step would let the clock run on while the group is still being started and the recorded
        starts would drift. Stopping at every timer deadline and settling several times keeps the starts exact.
        """
        target = self.t.monotonic_ns() + ms * NS_PER_MS
        while True:
            for _ in range(6):
                await self.t.settle()
            now = self.t.monotonic_ns()
            waiting = self.t.waiting
            if not waiting or waiting[0] > target:
                await self.t.advance_ns(target - now)
                for _ in range(6):
                    await self.t.settle()
                return
            await self.t.advance_ns(max(0, waiting[0] - now))

    async def run_for(self, ms: int, step_ms: int = 1_000) -> None:
        """Advance the manual clock in steps so that the task's timers fire in order."""
        remaining = ms
        while remaining > 0:
            step = min(step_ms, remaining)
            await self.t.advance(step)
            remaining -= step


def task_config(
    symbols: Sequence[str],
    *,
    budget_per_5min: int = 150,
    delay_ms: int = 30_000,
    offset_ms: int = 60_000,
    backfill_days: int = 365,
    recheck_ms: int = 86_400_000,
) -> FundingRateTaskConfig:
    return FundingRateTaskConfig(
        venue=VENUE,
        symbols=tuple(symbols),
        publication_delay_ms=delay_ms,
        round_offset_ms=offset_ms,
        budget_per_5min=budget_per_5min,
        backfill_days=backfill_days,
        recheck_ms=recheck_ms,
    )


async def start_task(
    tmp_path: Path,
    *,
    symbols: Sequence[str] = ("BTC/USDT:USDT", "ETH/USDT:USDT"),
    budget_per_5min: int = 150,
    backfill_days: int = 365,
    recheck_ms: int = 86_400_000,
    offset_ms: int = 60_000,
    rig: Rig | None = None,
    sink: Any = None,
    **rig_options: Any,
) -> Harness:
    rig = rig or await make_rig(tmp_path, symbols=symbols, budget_per_5min=budget_per_5min, sink=sink, **rig_options)
    symbols = tuple(rig.exchange.markets)  # the task collects exactly the symbols the exchange lists
    config = task_config(
        symbols,
        budget_per_5min=budget_per_5min,
        backfill_days=backfill_days,
        recheck_ms=recheck_ms,
        offset_ms=offset_ms,
    )
    lines: list[str] = []
    stop = asyncio.Event()
    measurements = FundingRateMeasurements()
    task = asyncio.ensure_future(
        run_funding_rate_task(
            rig.source,
            rig.writer,
            rig.db,
            config,
            rig.limiter,
            rig.pause,
            rig.t,
            rig.t.sleep,
            stop,
            lines.append,
            measurements,
        )
    )
    await rig.t.settle()
    return Harness(rig, config, lines, stop, measurements, task)


async def reopen(previous: Rig, *, sink: Any = None) -> Rig:
    """The restart: a fresh source, limiter, pause and writer on the same exchange, clock and database file."""
    limiter = RecordingLimiter(previous.limiter._rounds.cap + previous.limiter._other.cap, previous.t)
    pause = ExtensionPause(previous.t)
    source = await BinanceUsdmFundingRateSource.open(limiter, pause, previous.t, previous.exchange)
    activity = CandleWriteActivity()
    if sink is not None:
        writer = sink
    else:
        writer = await AsyncFundingRateWriter.open(previous.db, activity=activity)
        previous.t.track_executor(writer._executor)
    return Rig(previous.t, previous.exchange, source, limiter, pause, activity, previous.db, writer)


def record_plans(monkeypatch: Any) -> list[Any]:
    """Records every plan the task executes (kind, symbol, start, end) by wrapping ``execute_plan``."""
    from quasar_market_data.collector.funding_rate_execution import execute_plan as original

    plans: list[Any] = []

    async def recording(plan: Any, **kwargs: Any) -> Any:
        plans.append(plan)
        return await original(plan, **kwargs)

    monkeypatch.setattr("quasar_market_data.collector.funding_rate_task.execute_plan", recording)
    return plans


def seed_state(rig: Rig, symbol: str, span: Interval, verified_at_ms: int = 1_792_600_000_000) -> None:
    """Stored state for a test that runs the task on an ``InMemorySink``: the task reads the DATABASE at start (the
    reconcile), the sink must hold the same span so that its merges are adjacent."""
    conn = open_writer(rig.db)
    try:
        conn.execute(
            "INSERT INTO funding_rate_verified VALUES (?, ?, ?, ?, ?)",
            (VENUE, symbol, span.from_ms, span.through_ms, verified_at_ms),
        )
    finally:
        conn.close()
    if isinstance(rig.writer, InMemorySink):
        rig.writer.spans[symbol] = span
        rig.writer.verified[symbol] = verified_at_ms
