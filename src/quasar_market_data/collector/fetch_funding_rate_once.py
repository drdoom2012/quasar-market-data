"""Fetch one page of funding rate history of one symbol once and store the eligible rows (ADR-0004 P-4, P-5, P-16 FR-3).

Order of work: observe server time through the funding source (its own client, a class ``R`` request of its own
limiter), evaluate the conservative lower bound ``L`` before the history call (the source's waits can only make it
older), take ``end = L - publication_delay`` (``forward_end_bound_ms``, FR-1: the latest settlement that is
eligible, so no row of the answer can be pending) and ``start = end - hours * 1 h``, fetch **one** page of class
``R`` over ``[start, end]``, parse it all-or-nothing (FR-1), keep the rows the eligibility rule accepts (FR-1) and
write them (FR-2) **without** a ``merge``: the command stores rows and claims no coverage. The verified span is
the funding task's business (FR-4); a row outside or inside a span claims nothing (P-7).

With untrusted time **no request is made**: nothing could be stored, and an eligible-looking row must never be
inferred from a clock that cannot be trusted. The report then carries the reason. A page of exactly 1 000 rows is
reported as ``full_page``: it is valid, but older rows of the range were not requested, and the command never says
the range is complete. An empty answer is valid and stores nothing. The end bound equals ``L - delay`` and the
parser rejects any later row, so the selection cannot find a pending row; if it ever does, the command fails
closed before any write (``FundingRateSelectionError``).

Failures propagate: ``SourceError`` (also ``ExtensionPaused`` and ``ExtensionStopped``, which on the time request
are re-raised instead of being read as "no observation"), ``FundingRateParseError`` and any writer error,
including ``FundingRatePageError``, reach the caller unchanged.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from quasar_market_data.collector.async_funding_rate_writer import FundingRateWriteReport
from quasar_market_data.collector.binance_funding_rate import parse_funding_rate_history
from quasar_market_data.collector.clock import Clock
from quasar_market_data.collector.extension_pause import ExtensionPaused, ExtensionStopped
from quasar_market_data.collector.funding_rate_limiter import RequestClass
from quasar_market_data.collector.funding_rate_writer import SpanMerge
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.funding_rate import (
    DEFAULT_FUNDING_RATE_PUBLICATION_DELAY_MS,
    FUNDING_RATE_PAGE_LIMIT,
    HOUR_MS,
    FundingRate,
    FundingRateSelection,
    count_off_hour_rows,
    forward_end_bound_ms,
    select_eligible_funding_rates,
)
from quasar_market_data.domain.server_time import (
    DEFAULT_TIME_TRUST,
    ServerTimeObservation,
    TimeTrustConfig,
    TrustedTime,
    server_lower_bound,
)

MAX_HOURS: Final = 500
DEFAULT_HOURS: Final = 24
MAX_PUBLICATION_DELAY_MS: Final = 595_000  # ADR-0004 P-12: round_offset_ms <= 600 000 and at least delay + 5 000


class FundingRateSelectionError(SourceError):
    """The eligibility selection found a pending row in an answer to a request that ends at the eligible bound."""


class FundingRateOnceSource(Protocol):
    """``BinanceUsdmFundingRateSource`` satisfies it."""

    def market_id(self, symbol: str) -> str: ...

    async def fetch_server_time(self, request_class: RequestClass = RequestClass.R) -> ServerTimeObservation: ...

    async def fetch_funding_rate_history(
        self, symbol: str, start_time_ms: int, end_time_ms: int, *, request_class: RequestClass
    ) -> Sequence[object]: ...

    async def close(self) -> None: ...


class FundingRateSink(Protocol):
    """``AsyncFundingRateWriter`` satisfies it."""

    async def write(
        self, rows: Sequence[FundingRate], *, ingested_at_ms: int, merge: SpanMerge | None = None
    ) -> FundingRateWriteReport: ...


@dataclass(frozen=True)
class FetchFundingRateOnceConfig:
    symbol: str
    hours: int = DEFAULT_HOURS
    publication_delay_ms: int = DEFAULT_FUNDING_RATE_PUBLICATION_DELAY_MS
    time_trust: TimeTrustConfig = DEFAULT_TIME_TRUST

    def __post_init__(self) -> None:
        if type(self.symbol) is not str:
            raise TypeError(f"symbol must be str, got {type(self.symbol).__name__}")
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if type(self.hours) is not int:
            raise TypeError(f"hours must be int, got {type(self.hours).__name__}")
        if not 1 <= self.hours <= MAX_HOURS:
            raise ValueError(f"hours must be in 1..{MAX_HOURS}, got {self.hours}")
        if type(self.publication_delay_ms) is not int:
            raise TypeError(f"publication_delay_ms must be int, got {type(self.publication_delay_ms).__name__}")
        if not 0 <= self.publication_delay_ms <= MAX_PUBLICATION_DELAY_MS:
            raise ValueError(
                f"publication_delay_ms must be in 0..{MAX_PUBLICATION_DELAY_MS}, got {self.publication_delay_ms}"
            )


@dataclass(frozen=True)
class FetchFundingRateOnceReport:
    fetched: int
    selection: FundingRateSelection
    write: FundingRateWriteReport | None
    start_ms: int | None  # the requested range; None when no request was made (untrusted time)
    end_ms: int | None
    off_hour_rows: int  # FR-1 heuristic, counted only (P-3)
    full_page: bool  # exactly the page limit: older rows of the range were not requested


async def fetch_funding_rate_once(
    source: FundingRateOnceSource, writer: FundingRateSink, config: FetchFundingRateOnceConfig, clock: Clock
) -> FetchFundingRateOnceReport:
    try:
        observation = await source.fetch_server_time(request_class=RequestClass.R)
    except (ExtensionPaused, ExtensionStopped):
        raise  # a 429/418 on the time request is not "untrusted time": say what happened
    except SourceError:
        observation = None  # a failed time request means no observation, not a failed run

    request_ns, request_wall_ms = clock.monotonic_ns(), clock.wall_ms()
    trust = server_lower_bound(observation, request_ns, request_wall_ms, None, config.time_trust)
    if type(trust) is not TrustedTime:
        selection = select_eligible_funding_rates((), config.publication_delay_ms, trust)
        return FetchFundingRateOnceReport(0, selection, None, None, None, 0, False)

    end = forward_end_bound_ms(config.publication_delay_ms, trust.lower_bound_ms)
    start = end - config.hours * HOUR_MS
    response = await source.fetch_funding_rate_history(config.symbol, start, end, request_class=RequestClass.R)
    rows = parse_funding_rate_history(
        response,
        market_id=source.market_id(config.symbol),
        symbol=config.symbol,
        start_time_ms=start,
        end_time_ms=end,
    )
    selection = select_eligible_funding_rates(rows, config.publication_delay_ms, trust)
    if selection.pending:
        raise FundingRateSelectionError(
            f"{len(selection.pending)} row(s) of the answer are not yet eligible although the request ended at the"
            " eligible bound; nothing was written"
        )
    write = None
    if selection.eligible:
        write = await writer.write(selection.eligible, ingested_at_ms=clock.wall_ms())
    return FetchFundingRateOnceReport(
        len(rows), selection, write, start, end, count_off_hour_rows(rows), len(rows) == FUNDING_RATE_PAGE_LIMIT
    )
