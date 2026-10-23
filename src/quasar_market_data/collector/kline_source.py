"""The seam between the fetch-once use case and an exchange adapter.

A source returns the exchange's raw kline rows untouched (decimal text stays text) and a server time reading;
it never decides what is closed and never reads a clock. Every failure to obtain data is a ``SourceError``.
"""

from collections.abc import Sequence
from typing import Protocol

from quasar_market_data.domain.timeframe import Timeframe


class SourceError(Exception):
    """The exchange could not supply the requested data, or the adapter refused the request."""


class KlineSource(Protocol):
    async def fetch_server_time_ms(self) -> int: ...

    async def fetch_klines(self, symbol: str, timeframe: Timeframe, limit: int) -> Sequence[object]: ...

    async def close(self) -> None: ...
