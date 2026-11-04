"""Fakes shared by the fetch-funding-once use-case and CLI tests."""

from collections.abc import Sequence
from typing import Any

from quasar_market_data.collector.async_funding_rate_writer import FundingRateWriteReport
from quasar_market_data.collector.funding_rate_limiter import RequestClass
from quasar_market_data.collector.funding_rate_writer import FundingRateWriteError, SpanMerge
from quasar_market_data.domain.funding_rate import FundingRate
from quasar_market_data.domain.server_time import ServerTimeObservation
from tests.collector.fetch_fakes import FakeClock

HOUR = 3_600_000
MARKET_ID = "BTCUSDT"


def settlement_rows(
    first_hour_ms: int, last_hour_ms: int, *, market_id: str = MARKET_ID, offset_ms: int = 3
) -> list[dict[str, Any]]:
    """One Regular row per whole hour ``first_hour_ms .. last_hour_ms`` (inclusive), ``offset_ms`` after the hour."""
    return [
        {
            "symbol": market_id,
            "fundingTime": hour + offset_ms,
            "fundingRate": "0.00010000",
            "markPrice": "67123.45000000",
            "rateType": "Regular",
        }
        for hour in range(first_hour_ms, last_hour_ms + 1, HOUR)
    ]


class FakeFundingSource:
    """Answers like Binance: the OLDEST ``limit`` rows of the dataset inside ``[start, end]``, ascending."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        dataset: list[dict[str, Any]] | None = None,
        rtt_ms: int = 100,
        time_error: Exception | None = None,
        hist_error: BaseException | None = None,
        response: object | None = None,
    ) -> None:
        self.clock = clock
        hour = clock.wall_ms() // HOUR * HOUR
        self.dataset = settlement_rows(hour - 1_000 * HOUR, hour + 10 * HOUR) if dataset is None else dataset
        self.rtt_ms = rtt_ms
        self.time_error = time_error
        self.hist_error = hist_error
        self.response = response
        self.time_calls: list[RequestClass] = []
        self.hist_calls: list[tuple[str, int, int, RequestClass]] = []
        self.closed = False

    def market_id(self, symbol: str) -> str:
        return MARKET_ID

    async def fetch_server_time(self, request_class: RequestClass = RequestClass.R) -> ServerTimeObservation:
        self.time_calls.append(request_class)
        sent_ns = self.clock.monotonic_ns()
        self.clock.advance(self.rtt_ms)
        if self.time_error is not None:
            raise self.time_error
        return ServerTimeObservation(self.clock.wall_ms(), sent_ns, self.clock.monotonic_ns(), self.clock.wall_ms())

    async def fetch_funding_rate_history(
        self, symbol: str, start_time_ms: int, end_time_ms: int, *, request_class: RequestClass
    ) -> Sequence[object]:
        self.hist_calls.append((symbol, start_time_ms, end_time_ms, request_class))
        if self.hist_error is not None:
            raise self.hist_error
        if self.response is not None:
            return self.response  # type: ignore[return-value]
        inside = [row for row in self.dataset if start_time_ms <= row["fundingTime"] <= end_time_ms]
        return inside[:1_000]

    async def close(self) -> None:
        self.closed = True


class RecordingFundingSink:
    def __init__(self, error: FundingRateWriteError | None = None, *, conflicts: int = 0) -> None:
        self.error = error
        self.conflicts = conflicts
        self.writes: list[tuple[tuple[FundingRate, ...], int, SpanMerge | None]] = []

    async def write(
        self, rows: Sequence[FundingRate], *, ingested_at_ms: int, merge: SpanMerge | None = None
    ) -> FundingRateWriteReport:
        if self.error is not None:
            raise self.error
        self.writes.append((tuple(rows), ingested_at_ms, merge))
        conflicts = tuple(_conflict(row) for row in rows[: self.conflicts])
        return FundingRateWriteReport(len(rows) - len(conflicts), 0, conflicts, (), (), 0, 0, (), None, None)


def _conflict(row: FundingRate) -> Any:
    from quasar_market_data.collector.funding_rate_writer import FundingRateConflict

    return FundingRateConflict(row, row, True)
