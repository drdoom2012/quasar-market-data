"""Run one planned funding rate page: fetch, parse, select, write (ADR-0004 P-5, P-7, P-8).

``execute_plan`` is one request of the scheduler's plan (one page) and nothing else: no scheduling, no retry, no
state beyond the result. Order of work: the source's raw page, the all-or-nothing parser (FR-1), the page coverage
(FR-1), for the forward kinds ``S``, ``R`` and ``C`` the eligibility selection with the round's bound (a pending
row fails closed before any write), then the FR-2 writer.

The verified span is claimed only through the writer's ``merge`` in the page's final transaction (P-7):

* ``S``, ``R``, ``C``: every page claims its own covered interval, so a truncated page claims ``[a, last - 1]`` and
  the next page (class ``C``) continues at ``last``;
* ``H``: a chunk may need several pages. Earlier pages store their rows **without** a ``merge``; the last page claims
  the whole chunk ``[chunk.from_ms, chunk.through_ms]``. A chunk page passes the series' **current**
  ``verified_at_ms`` so a history merge never makes a series look freshly verified (selection rule 1 orders by it).

A failed page raises: ``FundingRateExecutionError`` wraps a ``FundingRatePageError`` of the writer together with the
late events its ``partial`` report already stored, classified with the span the caller planned with (the span the
writer saw is only known for the transaction that carried the ``merge``). Every other failure propagates unchanged.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from quasar_market_data.collector.async_funding_rate_writer import FundingRatePageError
from quasar_market_data.collector.binance_funding_rate import parse_funding_rate_history
from quasar_market_data.collector.clock import Clock
from quasar_market_data.collector.fetch_funding_rate_once import FundingRateSelectionError, FundingRateSink
from quasar_market_data.collector.funding_rate_writer import FundingRateConflict, SpanMerge
from quasar_market_data.domain.funding_rate import (
    FundingRateKey,
    count_off_hour_rows,
    select_eligible_funding_rates,
)
from quasar_market_data.domain.funding_rate_capacity import RequestClass
from quasar_market_data.domain.funding_rate_scheduler import Plan
from quasar_market_data.domain.funding_rate_span import Interval, page_coverage
from quasar_market_data.domain.server_time import TrustedTime


class FundingRatePageSource(Protocol):
    """``BinanceUsdmFundingRateSource`` satisfies it."""

    def market_id(self, symbol: str) -> str: ...

    async def fetch_funding_rate_history(
        self, symbol: str, start_time_ms: int, end_time_ms: int, *, request_class: RequestClass
    ) -> Sequence[object]: ...


@dataclass(frozen=True)
class PageResult:
    symbol: str
    plan: Plan
    rows: int
    inserted: int
    duplicates: int
    conflicts: tuple[FundingRateConflict, ...]
    late_events: tuple[FundingRateKey, ...]
    off_hour_rows: int
    span_after: Interval | None  # None when this page carried no merge (an earlier page of a history chunk)
    truncated_next_start: int | None  # where the range continues, when the page was full
    durations_ns: tuple[int, ...]
    request_ns: int  # from the start of the request until its answer arrived
    verified_at_ms: int | None  # the value passed to the merge, None without a merge


class FundingRateExecutionError(Exception):
    """A page failed in the writer; ``late_events`` are the late events its committed part already stored."""

    def __init__(self, cause: FundingRatePageError, late_events: tuple[FundingRateKey, ...], rows: int) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.late_events = late_events
        self.rows = rows  # the page's rows, to tell its 100-row transactions apart in the measurements


def _late(keys: Sequence[FundingRateKey], span: Interval | None) -> tuple[FundingRateKey, ...]:
    if span is None:
        return ()
    return tuple(key for key in keys if span.from_ms <= key.funding_time_ms <= span.through_ms)


async def execute_plan(
    plan: Plan,
    *,
    source: FundingRatePageSource,
    writer: FundingRateSink,
    venue: str,
    delay_ms: int,
    b_now_ms: int | None,
    series_verified_at_ms: int | None,
    span_planned: Interval | None,
    clock: Clock,
) -> PageResult:
    forward = plan.kind != "H"
    started_ns = clock.monotonic_ns()
    response = await source.fetch_funding_rate_history(
        plan.symbol, plan.start_ms, plan.end_ms, request_class=plan.request_class
    )
    request_ns = clock.monotonic_ns() - started_ns
    rows = parse_funding_rate_history(
        response,
        market_id=source.market_id(plan.symbol),
        symbol=plan.symbol,
        start_time_ms=plan.start_ms,
        end_time_ms=plan.end_ms,
    )
    coverage = page_coverage(rows, start_ms=plan.start_ms, end_ms=plan.end_ms)
    if forward:
        if b_now_ms is None:
            raise FundingRateSelectionError("a forward page needs a trusted bound")
        selection = select_eligible_funding_rates(rows, delay_ms, TrustedTime(b_now_ms + delay_ms))
        if selection.pending:
            raise FundingRateSelectionError(
                f"{len(selection.pending)} row(s) of the page are not yet eligible although the request ended at the"
                " eligible bound; nothing was written"
            )

    now_ms = clock.wall_ms()
    merge: SpanMerge | None
    if forward:
        merge = SpanMerge(venue, plan.symbol, coverage.covered, now_ms)
    elif coverage.next_start_ms is None and plan.chunk is not None:
        merge = SpanMerge(
            venue,
            plan.symbol,
            Interval(plan.chunk.from_ms, plan.chunk.through_ms),
            series_verified_at_ms if series_verified_at_ms is not None else now_ms,
        )
    else:
        merge = None  # an earlier page of a history chunk: its rows are stored, the chunk is claimed by the last page
    try:
        report = await writer.write(rows, ingested_at_ms=now_ms, merge=merge)
    except FundingRatePageError as error:
        late = _late(error.partial.inserted_keys, span_planned) if forward else ()
        raise FundingRateExecutionError(error, late, len(rows)) from error
    late_events = _late(report.inserted_keys, report.span_before) if forward else ()
    return PageResult(
        symbol=plan.symbol,
        plan=plan,
        rows=len(rows),
        inserted=report.inserted,
        duplicates=report.duplicates,
        conflicts=report.conflicts,
        late_events=late_events,
        off_hour_rows=count_off_hour_rows(rows),
        span_after=report.span_after,
        truncated_next_start=coverage.next_start_ms,
        durations_ns=report.durations_ns,
        request_ns=request_ns,
        verified_at_ms=None if merge is None else merge.verified_at_ms,
    )
