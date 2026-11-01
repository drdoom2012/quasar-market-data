"""Fetch the most recent klines of one symbol once and store only candles confirmed closed (ADR-0001).

Order of work: observe server time (the source stamps the send and the answer at the network call), evaluate the conservative lower bound at the instant the klines request
is sent, fetch the raw rows, parse them all-or-nothing, (for a backfill page) judge the page against its request, select the closed candles, write only those.

Eligibility is judged at the send time of the klines request, not after the response: the response is a
snapshot taken after the request arrives, so the server time at the snapshot is at least the bound at send
time, whereas a bound taken after a slow response could confirm a candle the exchange had not yet settled when
the snapshot was taken (ADR-0001 P-6). The rows are requested even when time is untrusted so that
``select_closed_candles`` stays the single gate (every candle then stays pending with the reason reported).

Failures propagate: ``SourceError``, ``KlineParseError`` and any writer error, including
``CandleWriteOutcomeUnknown``, reach the caller unchanged.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from quasar_market_data.collector.binance_klines import parse_klines
from quasar_market_data.collector.candle_writer import WriteReport
from quasar_market_data.collector.clock import Clock
from quasar_market_data.collector.kline_source import KlineSource, SourceError
from quasar_market_data.domain.backfill import PageOutcome, PagePlan, judge_page
from quasar_market_data.domain.closed_candles import (
    DEFAULT_PUBLICATION_DELAY_MS,
    ClosedCandleSelection,
    select_closed_candles,
)
from quasar_market_data.domain.server_time import DEFAULT_TIME_TRUST, TimeTrustConfig, server_lower_bound
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.validation import ValidatedCandle

MAX_LIMIT: Final = 1000


class CandleSink(Protocol):
    """Where ``fetch_once`` sends the confirmed closed candles; ``AsyncCandleWriter`` satisfies it."""

    async def write(self, candles: Sequence[ValidatedCandle], *, ingested_at_ms: int) -> WriteReport: ...


@dataclass(frozen=True)
class FetchOnceConfig:
    symbol: str
    timeframe: Timeframe
    limit: int
    publication_delay_ms: int = DEFAULT_PUBLICATION_DELAY_MS
    time_trust: TimeTrustConfig = DEFAULT_TIME_TRUST
    page: PagePlan | None = None

    def __post_init__(self) -> None:
        if type(self.symbol) is not str:
            raise TypeError(f"symbol must be str, got {type(self.symbol).__name__}")
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if type(self.timeframe) is not Timeframe:
            raise TypeError(f"timeframe must be Timeframe, got {type(self.timeframe).__name__}")
        if type(self.limit) is not int:
            raise TypeError(f"limit must be int, got {type(self.limit).__name__}")
        if not 1 <= self.limit <= MAX_LIMIT:
            raise ValueError(f"limit must be in 1..{MAX_LIMIT}, got {self.limit}")
        if self.page is not None:
            if type(self.page) is not PagePlan:
                raise TypeError(f"page must be PagePlan, got {type(self.page).__name__}")
            if self.limit != self.page.limit:
                raise ValueError(f"limit {self.limit} must equal page.limit {self.page.limit}")


@dataclass(frozen=True)
class FetchOnceReport:
    fetched: int
    selection: ClosedCandleSelection
    write: WriteReport | None
    page: PageOutcome | None = None


async def fetch_once(source: KlineSource, writer: CandleSink, config: FetchOnceConfig, clock: Clock) -> FetchOnceReport:
    try:
        observation = await source.fetch_server_time()
    except SourceError:
        observation = None  # a failed time request means no observation, not a failed run

    request_ns, request_wall_ms = clock.monotonic_ns(), clock.wall_ms()
    trust = server_lower_bound(observation, request_ns, request_wall_ms, None, config.time_trust)

    page = config.page
    if page is None:
        rows = await source.fetch_klines(config.symbol, config.timeframe, config.limit)
    else:
        rows = await source.fetch_klines(
            config.symbol, config.timeframe, config.limit, start_time_ms=page.start_ms, end_time_ms=page.end_ms
        )
    candles = parse_klines(rows, config.symbol, config.timeframe)
    # A page is judged against its request before anything is selected or written (S16 D4): a violation stores nothing.
    outcome = (
        None if page is None else judge_page(page, [c.key.open_time_ms for c in candles], config.timeframe.milliseconds)
    )
    selection = select_closed_candles(candles, config.publication_delay_ms, trust)

    write = None
    if selection.eligible:
        write = await writer.write(selection.eligible, ingested_at_ms=clock.wall_ms())
    return FetchOnceReport(fetched=len(candles), selection=selection, write=write, page=outcome)
