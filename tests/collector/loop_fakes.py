"""A scripted ``KlineSource`` for the collection-loop tests, driven by ``ManualTime``.

Rows are generated from the manual clock, so the newest closed slot is whatever the clock says; nothing here uses
a worker thread or real waiting. The reported server time is ``wall + skew_ms``: a positive skew makes a slot
confirmable at the instant it is expected, which keeps the scheduling tests free of incidental retries.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from quasar_market_data.domain.timeframe import Timeframe
from tests.collector.fetch_fakes import closed_rows
from tests.collector.manual_time import ManualTime

DELAY_MS = 15_000
PRECISION_MARGIN_MS = 50


class ScriptedSource:
    def __init__(self, t: ManualTime, *, skew_ms: int = 1_000) -> None:
        self.t = t
        self.skew_ms = skew_ms
        self.klines_calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.gate: asyncio.Event | None = None  # parks every symbol's klines call until set
        self.gates: dict[str, asyncio.Event] = {}  # parks one symbol's klines call
        self.errors: dict[str, BaseException] = {}  # raised on every klines call of the symbol
        self.script: dict[str, list[BaseException | None]] = {}  # one entry per klines call, consumed in order
        self.fail_after: dict[str, tuple[asyncio.Event, BaseException]] = {}  # parks, then raises
        self.windows: dict[str, list[int]] = {}  # eligible rows per klines call, consumed in order (default 3)
        self.delays: list[int] = []  # ms of manual time each klines call takes, consumed in order
        self.closed = False

    async def fetch_server_time_ms(self) -> int:
        return self.t.wall_ms() + self.skew_ms

    def rows(self, rows: int, tf_ms: int) -> list[list[Any]]:
        bound = self.t.wall_ms() + self.skew_ms - PRECISION_MARGIN_MS - DELAY_MS
        newest_open = bound // tf_ms * tf_ms - tf_ms
        template = closed_rows()[0]
        out: list[list[Any]] = []
        for open_ms in range(newest_open - (rows - 1) * tf_ms, newest_open + 2 * tf_ms, tf_ms):  # + one forming
            row = list(template)
            row[0], row[6] = open_ms, open_ms + tf_ms - 1
            out.append(row)
        return out

    async def fetch_klines(self, symbol: str, timeframe: Timeframe, limit: int) -> Sequence[object]:
        self.klines_calls.append(symbol)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            gate = self.gates.get(symbol, self.gate)
            if gate is not None:
                await gate.wait()
            if symbol in self.fail_after:
                event, error = self.fail_after[symbol]
                await event.wait()
                raise error
            if self.delays:
                await self.t.sleep(self.delays.pop(0))
            scripted = self.script.get(symbol)
            if scripted:
                error_or_none = scripted.pop(0)
                if error_or_none is not None:
                    raise error_or_none
            if symbol in self.errors:
                raise self.errors[symbol]
            window = self.windows.get(symbol)
            return self.rows(window.pop(0) if window else 3, timeframe.milliseconds)
        finally:
            self.active -= 1

    async def close(self) -> None:
        self.closed = True
