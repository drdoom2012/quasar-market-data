"""One refresh of the overview, shared by every caller (spec 5.2, 5.9, 5.10).

At most one refresh runs at a time; a finished snapshot, a page error included, is served for ``min_interval_s``.
Nothing runs in the background: a refresh happens only when somebody asks.
"""

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from tools.symbol_overview.fetch import Fetch, RawResponse, TransportError, coverage_path
from tools.symbol_overview.judge import build_overview, judge_symbol, page_error
from tools.symbol_overview.model import (
    TIMEFRAME,
    VENUE,
    Coverage,
    InstrumentRow,
    Invalid,
    SymbolReport,
)
from tools.symbol_overview.validate import coverage_result, health_result, instruments_result
from tools.symbol_overview.window import window_for


@dataclass(frozen=True)
class Snapshot:
    status: int  # 200, or 502 when the API cannot be trusted at page level
    body: dict[str, object]


class Overviewer:
    def __init__(
        self,
        fetch: Fetch,
        *,
        concurrency: int = 4,
        deadline_s: float = 60.0,
        min_interval_s: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._concurrency = concurrency
        self._deadline_s = deadline_s
        self._min_interval_s = min_interval_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cached: tuple[float, Snapshot] | None = None
        self._inflight: Future[Snapshot] | None = None

    def get(self) -> Snapshot:
        """The current snapshot: the cached one if young enough, else the result of one (shared) refresh."""
        with self._lock:
            if self._cached is not None and self._monotonic() - self._cached[0] < self._min_interval_s:
                return self._cached[1]
            leader = self._inflight is None
            if leader:
                self._inflight = Future()
            future = self._inflight
        assert future is not None
        if not leader:
            return future.result()
        try:
            snapshot = self._refresh()
        except BaseException as error:  # a bug: nothing is cached, followers see the same failure
            with self._lock:
                self._inflight = None
            future.set_exception(error)
            raise
        with self._lock:
            self._cached = (self._monotonic(), snapshot)
            self._inflight = None
        future.set_result(snapshot)
        return snapshot

    def _call(self, path: str) -> RawResponse | Invalid:
        try:
            return self._fetch(path)
        except TransportError as error:
            return Invalid("transport", error.code)

    def _refresh(self) -> Snapshot:
        started = self._monotonic()

        raw = self._call("/health/ingest")
        health = raw if isinstance(raw, Invalid) else health_result(raw.status, raw.content_type, raw.body)
        if isinstance(health, Invalid):
            return Snapshot(502, page_error(health))
        raw = self._call("/v1/instruments")
        instruments = raw if isinstance(raw, Invalid) else instruments_result(raw.status, raw.content_type, raw.body)
        if isinstance(instruments, Invalid):
            return Snapshot(502, page_error(instruments))
        try:
            window = window_for(health.as_of_ms)
        except ValueError:
            return Snapshot(502, page_error(Invalid("schema", "window")))

        deadline_hit = threading.Event()

        def one(instrument: InstrumentRow) -> Coverage | Invalid | None:
            if instrument.newest_open_time_ms is None:
                return None
            if instrument.venue != VENUE or instrument.timeframe != TIMEFRAME:
                return Invalid("schema", "unsupported_series")
            if self._monotonic() - started > self._deadline_s:
                deadline_hit.set()
                return Invalid("deadline")
            answer = self._call(coverage_path(instrument, window))
            if isinstance(answer, Invalid):
                return answer
            return coverage_result(
                answer.status,
                answer.content_type,
                answer.body,
                instrument=instrument,
                window=window,
                health=health,
            )

        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            outcomes = list(pool.map(one, instruments))
        reports: list[SymbolReport] = [
            judge_symbol(instrument, outcome, window) for instrument, outcome in zip(instruments, outcomes, strict=True)
        ]
        duration_ms = int((self._monotonic() - started) * 1000)
        return Snapshot(
            200, build_overview(health, window, reports, duration_ms=duration_ms, deadline_hit=deadline_hit.is_set())
        )
