"""The seam between the fetch-once use case and an exchange adapter.

A source returns the exchange's raw kline rows untouched (decimal text stays text) and a server time observation;
it never decides what is closed. It reads a clock for one purpose only: to stamp the server-time request with the
instants it was sent and answered (ADR-0001 P-4). Those must be taken at the network call itself, after any queue
in front of it (the rate limiter), so waiting for a slot is never counted as round-trip time. Every failure to
obtain data is a ``SourceError``.
"""

from collections.abc import Sequence
from typing import Protocol

from quasar_market_data.domain.server_time import ServerTimeObservation
from quasar_market_data.domain.timeframe import Timeframe


class SourceError(Exception):
    """The exchange could not supply the requested data, or the adapter refused the request."""


class KlineSource(Protocol):
    async def fetch_server_time(self) -> ServerTimeObservation: ...

    async def fetch_klines(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> Sequence[object]: ...

    async def close(self) -> None: ...
