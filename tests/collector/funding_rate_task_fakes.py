"""Fakes for the funding rate task tests: a CCXT-level exchange on ``ManualTime`` and a rig around the REAL source.

The scheduling scheme of the task depends on ``BinanceUsdmFundingRateSource`` reaching the limiter's group lock with
no ``await`` before it, so the task is always driven through the real source on ``FakeFundingExchange``, never through
a hand-written source that awaits first.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quasar_market_data.collector.async_funding_rate_writer import AsyncFundingRateWriter, FundingRateWriteReport
from quasar_market_data.collector.binance_usdm_funding_rate import BinanceUsdmFundingRateSource
from quasar_market_data.collector.candle_write_activity import CandleWriteActivity
from quasar_market_data.collector.extension_pause import ExtensionPause
from quasar_market_data.collector.funding_rate_limiter import FundingRateLimiter
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
        self.hidden: set[tuple[str, int]] = set()
        self.rate_override: dict[tuple[str, int], str] = {}
        self.offset_ms = OFFSET_MS
        self.errors: list[BaseException | None] = []  # one entry per funding request, consumed in order
        self.symbol_errors: dict[str, BaseException] = {}  # raised on every request of the symbol
        self.error_always: BaseException | None = None
        self.time_error: BaseException | None = None
        self.hold: asyncio.Event | None = None  # when set, funding requests wait for it
        self.hang = False
        self.hang_symbols: set[str] = set()
        self.mutate: Callable[[list[dict[str, Any]]], object] | None = None
        self.served: list[Served] = []
        self.time_calls: list[int] = []
        self.active = 0
        self.max_active = 0
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

    async def fapiPublicGetTime(self) -> object:
        self.time_calls.append(self.t.monotonic_ns())
        if self.request_ms:
            await self.t.sleep(self.request_ms)
        if self.time_error is not None:
            raise self.time_error
        return {"serverTime": self.t.wall_ms()}


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
    limiter: FundingRateLimiter
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
    limiter = FundingRateLimiter(budget_per_5min, t, t.sleep)
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
