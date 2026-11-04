"""Fetch the newest eligible open interest rows of one symbol once and store them (ADR-0003 P-4, P-5, P-16 OI-3).

Order of work: observe server time through the open interest source (its own client, counted in the open
interest budget), evaluate the conservative lower bound at the instant the history request is sent, derive the
range ``[B - (periods - 1) * 5 min, B]`` where ``B`` is the newest period end at or before that bound, fetch the
raw rows, parse them all-or-nothing (OI-1), keep only the rows whose period end plus the publication delay is at
or before the bound (OI-1), and write those (OI-2).

With untrusted time **no request is made**: nothing could be stored, and an eligible-looking row must never be
inferred from a clock that cannot be trusted. The report then carries the reason. A row that is not yet eligible
is pending and is not stored; it is not an error. Absence of a row is not an error either (P-7).

Failures propagate: ``SourceError`` (also ``ExtensionPaused`` and ``ExtensionStopped``, which on the time
request are re-raised instead of being read as "no observation"),
``OpenInterestParseError`` and any writer error, including ``OpenInterestPageError``, reach the caller unchanged.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from quasar_market_data.collector.async_open_interest_writer import OpenInterestWriteReport
from quasar_market_data.collector.binance_open_interest import parse_open_interest_hist
from quasar_market_data.collector.clock import Clock
from quasar_market_data.collector.extension_pause import ExtensionPaused, ExtensionStopped
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.open_interest import (
    DEFAULT_OPEN_INTEREST_PUBLICATION_DELAY_MS,
    OpenInterest,
    OpenInterestSelection,
    select_eligible_open_interest,
)
from quasar_market_data.domain.open_interest_request import MAX_PERIOD_ENDS
from quasar_market_data.domain.server_time import (
    DEFAULT_TIME_TRUST,
    ServerTimeObservation,
    TimeTrustConfig,
    TrustedTime,
    server_lower_bound,
)
from quasar_market_data.domain.timeframe import Timeframe

MAX_PERIODS: Final = MAX_PERIOD_ENDS
DEFAULT_PERIODS: Final = 3
_PERIOD: Final = Timeframe("5m")


class OpenInterestOnceSource(Protocol):
    """``BinanceUsdmOpenInterestSource`` satisfies it."""

    def market_id(self, symbol: str) -> str: ...

    async def fetch_server_time(self) -> ServerTimeObservation: ...

    async def fetch_open_interest_hist(
        self, symbol: str, start_time_ms: int, end_time_ms: int, *, as_of_ms: int
    ) -> Sequence[object]: ...

    async def close(self) -> None: ...


class OpenInterestSink(Protocol):
    """``AsyncOpenInterestWriter`` satisfies it."""

    async def write(self, rows: Sequence[OpenInterest], *, ingested_at_ms: int) -> OpenInterestWriteReport: ...


@dataclass(frozen=True)
class FetchOpenInterestOnceConfig:
    symbol: str
    periods: int = DEFAULT_PERIODS
    publication_delay_ms: int = DEFAULT_OPEN_INTEREST_PUBLICATION_DELAY_MS
    time_trust: TimeTrustConfig = DEFAULT_TIME_TRUST

    def __post_init__(self) -> None:
        if type(self.symbol) is not str:
            raise TypeError(f"symbol must be str, got {type(self.symbol).__name__}")
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if type(self.periods) is not int:
            raise TypeError(f"periods must be int, got {type(self.periods).__name__}")
        if not 1 <= self.periods <= MAX_PERIODS:
            raise ValueError(f"periods must be in 1..{MAX_PERIODS}, got {self.periods}")
        if type(self.publication_delay_ms) is not int:
            raise TypeError(f"publication_delay_ms must be int, got {type(self.publication_delay_ms).__name__}")
        if self.publication_delay_ms < 0:
            raise ValueError(f"publication_delay_ms must be >= 0, got {self.publication_delay_ms}")


@dataclass(frozen=True)
class FetchOpenInterestOnceReport:
    fetched: int
    selection: OpenInterestSelection
    write: OpenInterestWriteReport | None
    start_ms: int | None  # the requested range; None when no request was made (untrusted time)
    end_ms: int | None


async def fetch_open_interest_once(
    source: OpenInterestOnceSource, writer: OpenInterestSink, config: FetchOpenInterestOnceConfig, clock: Clock
) -> FetchOpenInterestOnceReport:
    try:
        observation = await source.fetch_server_time()
    except (ExtensionPaused, ExtensionStopped):
        raise  # a 429/418 on the time request is not "untrusted time": say what happened
    except SourceError:
        observation = None  # a failed time request means no observation, not a failed run

    request_ns, request_wall_ms = clock.monotonic_ns(), clock.wall_ms()
    trust = server_lower_bound(observation, request_ns, request_wall_ms, None, config.time_trust)
    if type(trust) is not TrustedTime:
        return FetchOpenInterestOnceReport(
            0, select_eligible_open_interest((), config.publication_delay_ms, trust), None, None, None
        )

    step = _PERIOD.milliseconds
    end = trust.lower_bound_ms // step * step
    start = end - (config.periods - 1) * step
    response = await source.fetch_open_interest_hist(config.symbol, start, end, as_of_ms=trust.lower_bound_ms)
    rows = parse_open_interest_hist(
        response,
        market_id=source.market_id(config.symbol),
        symbol=config.symbol,
        period=_PERIOD,
        start_time_ms=start,
        end_time_ms=end,
    )
    selection = select_eligible_open_interest(rows, config.publication_delay_ms, trust)
    write = None
    if selection.eligible:
        write = await writer.write(selection.eligible, ingested_at_ms=clock.wall_ms())
    return FetchOpenInterestOnceReport(len(rows), selection, write, start, end)
