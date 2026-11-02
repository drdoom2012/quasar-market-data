"""Fakes shared by the fetch-oi-once use-case and CLI tests."""

from collections.abc import Callable, Sequence
from typing import Any

from quasar_market_data.collector.open_interest_writer import OpenInterestWriteError
from quasar_market_data.domain.open_interest import OpenInterest
from quasar_market_data.domain.server_time import ServerTimeObservation
from tests.collector.fetch_fakes import FakeClock

STEP = 300_000
MARKET_ID = "BTCUSDT"


def default_values(time_ms: int) -> tuple[str, str]:
    return f"{1000 + time_ms // STEP % 1000}.5", f"{(1000 + time_ms // STEP % 1000) * 100}.25"


class FakeOiSource:
    """Answers like Binance: one row per period end of the requested range, the newest ``limit`` of them."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        rtt_ms: int = 100,
        time_error: Exception | None = None,
        hist_error: BaseException | None = None,
        values: Callable[[int], tuple[str, str]] = default_values,
        symbol_in_rows: str = MARKET_ID,
    ) -> None:
        self.clock = clock
        self.rtt_ms = rtt_ms
        self.time_error = time_error
        self.hist_error = hist_error
        self.values = values
        self.symbol_in_rows = symbol_in_rows
        self.time_calls = 0
        self.hist_calls: list[tuple[str, int, int, int]] = []  # symbol, start, end, as_of
        self.closed = False

    def market_id(self, symbol: str) -> str:
        return MARKET_ID

    async def fetch_server_time(self) -> ServerTimeObservation:
        self.time_calls += 1
        sent_ns = self.clock.monotonic_ns()
        self.clock.advance(self.rtt_ms)
        if self.time_error is not None:
            raise self.time_error
        return ServerTimeObservation(self.clock.wall_ms(), sent_ns, self.clock.monotonic_ns(), self.clock.wall_ms())

    async def fetch_open_interest_hist(
        self, symbol: str, start_time_ms: int, end_time_ms: int, *, as_of_ms: int
    ) -> Sequence[object]:
        self.hist_calls.append((symbol, start_time_ms, end_time_ms, as_of_ms))
        if self.hist_error is not None:
            raise self.hist_error
        rows: list[dict[str, Any]] = []
        for end in range(-(-start_time_ms // STEP) * STEP, end_time_ms + 1, STEP):
            open_interest, value = self.values(end)
            rows.append(
                {
                    "symbol": self.symbol_in_rows,
                    "sumOpenInterest": open_interest,
                    "sumOpenInterestValue": value,
                    "CMCCirculatingSupply": "19000.0",
                    "timestamp": end,
                }
            )
        return rows

    async def close(self) -> None:
        self.closed = True


class RecordingSink:
    def __init__(self, error: OpenInterestWriteError | None = None) -> None:
        self.error = error
        self.writes: list[tuple[tuple[OpenInterest, ...], int]] = []

    async def write(self, rows: Sequence[OpenInterest], *, ingested_at_ms: int) -> Any:
        if self.error is not None:
            raise self.error
        self.writes.append((tuple(rows), ingested_at_ms))
        from quasar_market_data.collector.async_open_interest_writer import OpenInterestWriteReport

        return OpenInterestWriteReport(len(rows), 0, (), (), (), 0, 0)
