"""The backfill worker (S16): fill holes of the stored history in the idle time between collection rounds.

Every candle reaches the writer through ``fetch_once`` with a ``PagePlan`` (INV-053): the page is judged against its
request before the closed-candle selection, and this module never writes, never judges a page itself and writes
nothing to ``unavailable_candles``. What it reports is kept in three separate statements: what the exchange served
(``served_*``), what the writer reported (``stored``, ``duplicates``, ...) and what was recovered, which is only
ever a difference of two read-backs of the stored open times, attributed to this slice (``attribute_recovery``).

All time inside a window is monotonic and relative to the ``IdleWindow`` the loop hands in; a wall-clock step can
neither extend nor cut a window, and is never counted as an overrun. Back-off, exhaustion and suspension live in
memory: losing them costs requests, never data (S16 D7).
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from quasar_market_data.collector.binance_klines import VENUE_BINANCE_USDM, KlineParseError
from quasar_market_data.collector.candle_writer import CandleWriteOutcomeUnknown
from quasar_market_data.collector.clock import Clock, Sleep
from quasar_market_data.collector.collection_loop import IdleWindow, LoopConfig
from quasar_market_data.collector.deadline_source import DeadlineSource
from quasar_market_data.collector.fetch_once import MAX_LIMIT, FetchOnceConfig, fetch_once
from quasar_market_data.collector.hole_reader import HoleReadError, read_holes, read_present
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.backfill import (
    Attribution,
    BackfillTarget,
    PageViolation,
    TargetResult,
    attribute_recovery,
    judge_target,
    max_pages,
    plan_page,
    plan_targets,
)

REQUEST_TIMEOUT_MS: Final = 5_000  # below CCXT's 10 s; covers the limiter wait and the request
WRITE_RESERVE_MS: Final = 5_000  # one SQLite lock wait: an assumption, not a bound
PAGE_BUDGET_MS: Final = 2 * REQUEST_TIMEOUT_MS + WRITE_RESERVE_MS
RESCAN_CYCLES: Final = 12
MAX_BACKOFF_CYCLES: Final = 12
UNPROVEN_RETRY_CYCLES: Final = 288  # one day of 5-minute cycles
OVERRUN_LIMIT: Final = 2
_NS_PER_MS: Final = 1_000_000
_DAY_MS: Final = 86_400_000


@dataclass(frozen=True)
class BackfillConfig:
    backfill_days: int = 30

    def __post_init__(self) -> None:
        if type(self.backfill_days) is not int:
            raise TypeError(f"backfill_days must be int, got {type(self.backfill_days).__name__}")
        if not 1 <= self.backfill_days <= 365:
            raise ValueError(f"backfill_days must be in 1..365, got {self.backfill_days}")


class SliceStatus(StrEnum):
    NO_TARGETS = "no_targets"
    PROGRESS = "progress"
    BACKOFF = "backoff"
    TIME_UNTRUSTED = "time_untrusted"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class SliceReport:
    """One symbol's slice of one window. ``None`` is *unverified* (a read-back failed or could not attribute)."""

    symbol: str
    pages: int
    served_all: int
    served_some: int
    served_none: int
    stored: int
    duplicates: int
    conflicts: int
    invalid: int
    pending: int
    new_in_targets: int | None
    recovered: int | None
    missing_in_targets: int | None
    attribution: Attribution
    status: SliceStatus


@dataclass
class _Scan:
    cycle: int
    targets: tuple[BackfillTarget, ...]
    dirty: bool = False


class Backfiller:
    def __init__(
        self,
        config: BackfillConfig,
        loop_config: LoopConfig,
        db: Path,
        clock: Clock,
        sleep: Sleep,
        on_slice: Callable[[SliceReport], None],
        on_suspended: Callable[[int, int], None],
    ) -> None:
        self._config = config
        self._loop = loop_config
        self._db = db
        self._clock = clock
        self._sleep = sleep
        self._on_slice = on_slice
        self._on_suspended = on_suspended
        self._tf = loop_config.timeframe.milliseconds
        self._cycle = 0
        self._cursor = 0
        self._failures: dict[str, int] = {}
        self._backoff_until: dict[str, int] = {}
        self._exhausted_until: dict[tuple[str, int], int] = {}
        self._scans: dict[str, _Scan] = {}
        self._overruns = 0
        self._suspended = False

    async def run_idle(self, window: IdleWindow) -> None:
        if self._suspended:
            return
        self._cycle += 1
        self._prune()
        await self._run(window)
        overrun_ms = max(0, (self._clock.monotonic_ns() - window.due_mono_ns) // _NS_PER_MS)
        self._overruns = self._overruns + 1 if overrun_ms > 0 else 0
        if self._overruns >= OVERRUN_LIMIT:
            self._suspended = True
            self._on_suspended(self._overruns, overrun_ms)

    def _prune(self) -> None:
        """Forget expired entries: the head key changes every UTC day and would otherwise accumulate."""
        self._exhausted_until = {k: v for k, v in self._exhausted_until.items() if v > self._cycle}
        self._backoff_until = {k: v for k, v in self._backoff_until.items() if v >= self._cycle}

    def _room(self, window: IdleWindow) -> bool:
        return (
            not window.stop.is_set()
            and self._clock.monotonic_ns() + PAGE_BUDGET_MS * _NS_PER_MS <= window.deadline_mono_ns
        )

    async def _run(self, window: IdleWindow) -> None:
        symbols = self._loop.symbols
        for step in range(len(symbols)):
            if not self._room(window):
                return
            index = (self._cursor + step) % len(symbols)
            symbol = symbols[index]
            if self._backoff_until.get(symbol, 0) >= self._cycle:
                continue
            self._cursor = (index + 1) % len(symbols)
            await self._slice(window, symbol)

    def _fail(self, symbol: str) -> None:
        failures = self._failures.get(symbol, 0) + 1
        self._failures[symbol] = failures
        self._backoff_until[symbol] = self._cycle + min(1 << (failures - 1), MAX_BACKOFF_CYCLES)
        self._scans.pop(symbol, None)

    async def _targets(self, symbol: str) -> tuple[tuple[BackfillTarget, ...], bool]:
        """The symbol's targets that are not exhausted, and whether a fresh scan found no hole at all."""
        scan = self._scans.get(symbol)
        fresh = scan is None or scan.dirty or self._cycle - scan.cycle >= RESCAN_CYCLES
        if scan is None or fresh:
            now = self._clock.wall_ms()
            horizon_start = now // _DAY_MS * _DAY_MS - self._config.backfill_days * _DAY_MS
            holes = await read_holes(
                self._db,
                venue=VENUE_BINANCE_USDM,
                symbol=symbol,
                timeframe=self._loop.timeframe,
                horizon_start_ms=horizon_start,
            )
            targets = plan_targets(
                horizon_start_ms=horizon_start,
                forming_open_ms=now // self._tf * self._tf,
                oldest_stored_ms=holes.oldest_stored_ms,
                interior_runs=holes.interior_runs,
                timeframe_ms=self._tf,
            )
            scan = self._scans[symbol] = _Scan(self._cycle, targets)
        open_targets = tuple(
            t for t in scan.targets if self._exhausted_until.get((symbol, t.first_ms), 0) <= self._cycle
        )
        return open_targets, fresh and not scan.targets

    async def _slice(self, window: IdleWindow, symbol: str) -> None:
        try:
            targets, nothing_found = await self._targets(symbol)
        except HoleReadError:
            self._fail(symbol)
            self._emit(symbol, SliceStatus.BACKOFF, Attribution.READBACK_FAILED)
            return
        if not targets:
            if nothing_found:
                self._emit(symbol, SliceStatus.NO_TARGETS, Attribution.OK, new=0, recovered=0, missing=0)
            return
        try:
            before: tuple[frozenset[int], ...] | None = await read_present(
                self._db, venue=VENUE_BINANCE_USDM, symbol=symbol, timeframe=self._loop.timeframe, targets=targets
            )
        except HoleReadError:
            self._fail(symbol)
            self._emit(symbol, SliceStatus.BACKOFF, Attribution.READBACK_FAILED)
            return

        counts = {"pages": 0, "all": 0, "some": 0, "none": 0, "stored": 0, "dup": 0, "conf": 0, "inv": 0, "pend": 0}
        sent: set[int] = set()
        inserted: int | None = 0
        status = SliceStatus.PROGRESS
        failed = False
        for target in targets:
            result = await self._walk(window, symbol, target, counts, sent)
            if result.inserted is None:
                inserted = None
            elif inserted is not None:
                inserted += result.inserted
            if result.served is not None:
                counts[{"served_all": "all", "served_some": "some", "served_none": "none"}[result.served.value]] += 1
                if result.served is not TargetResult.SERVED_ALL or not result.inserted:
                    self._exhausted_until[(symbol, target.first_ms)] = self._cycle + UNPROVEN_RETRY_CYCLES
            if result.failed:
                failed = True
                break
            if result.untrusted:
                status = SliceStatus.TIME_UNTRUSTED
                break
            if result.stopped:
                break

        try:
            after: tuple[frozenset[int], ...] | None = await read_present(
                self._db, venue=VENUE_BINANCE_USDM, symbol=symbol, timeframe=self._loop.timeframe, targets=targets
            )
        except HoleReadError:
            after = None
        recovery = attribute_recovery(targets, before, after, frozenset(sent), inserted, self._tf)
        if failed:
            status = SliceStatus.BACKOFF
            self._fail(symbol)
        elif status is SliceStatus.PROGRESS and counts["pages"]:
            self._failures.pop(symbol, None)
        if counts["stored"] or (recovery.new_in_targets or 0):
            self._scans.pop(symbol, None)  # something changed: the next window re-derives the holes
        self._on_slice(
            SliceReport(
                symbol,
                counts["pages"],
                counts["all"],
                counts["some"],
                counts["none"],
                counts["stored"],
                counts["dup"],
                counts["conf"],
                counts["inv"],
                counts["pend"],
                recovery.new_in_targets,
                recovery.recovered,
                recovery.missing_in_targets,
                recovery.attribution,
                status,
            )
        )

    def _emit(
        self,
        symbol: str,
        status: SliceStatus,
        attribution: Attribution,
        *,
        new: int | None = None,
        recovered: int | None = None,
        missing: int | None = None,
    ) -> None:
        self._on_slice(SliceReport(symbol, 0, 0, 0, 0, 0, 0, 0, 0, 0, new, recovered, missing, attribution, status))

    async def _walk(
        self, window: IdleWindow, symbol: str, target: BackfillTarget, counts: dict[str, int], sent: set[int]
    ) -> "_Walk":
        start = target.first_ms
        rows_total = 0
        inserted: int | None = 0
        pages_left = max_pages(target, self._tf, MAX_LIMIT)
        while True:
            if not self._room(window):
                return _Walk(None, inserted, stopped=True)
            if pages_left <= 0:  # a page cap exceeded is a bug in the walk or a lying page: fail the slice
                return _Walk(None, inserted, failed=True)
            pages_left -= 1
            page = plan_page(target, start, self._tf, MAX_LIMIT)
            source = DeadlineSource(
                window.source,
                timeout_ms=REQUEST_TIMEOUT_MS,
                deadline_mono_ns=window.deadline_mono_ns - WRITE_RESERVE_MS * _NS_PER_MS,
                clock=self._clock,
                sleep=self._sleep,
                stop=window.stop,
            )
            config = FetchOnceConfig(
                symbol=symbol,
                timeframe=self._loop.timeframe,
                limit=page.limit,
                publication_delay_ms=self._loop.publication_delay_ms,
                time_trust=self._loop.time_trust,
                page=page,
            )
            try:
                report = await fetch_once(source, window.sink_for(symbol), config, self._clock)
            except SourceError:
                if window.stop.is_set():
                    return _Walk(None, inserted, stopped=True)
                return _Walk(None, inserted, failed=True)
            except (KlineParseError, PageViolation):
                return _Walk(None, inserted, failed=True)
            except CandleWriteOutcomeUnknown:
                return _Walk(None, None, failed=True)  # the watch has marked the symbol uncertain
            counts["pages"] += 1
            rows_total += report.fetched
            sent.update(c.key.open_time_ms for c in report.selection.eligible)
            counts["inv"] += len(report.selection.invalid)
            counts["pend"] += len(report.selection.pending)
            if report.write is not None:
                counts["stored"] += report.write.inserted
                counts["dup"] += report.write.duplicates
                counts["conf"] += len(report.write.conflicts)
                inserted = (inserted or 0) + report.write.inserted
            if report.selection.untrusted_reason is not None:
                return _Walk(None, inserted, untrusted=True)
            outcome = report.page
            if outcome is None:  # fetch_once with a page always reports one
                return _Walk(None, inserted, failed=True)
            if outcome.next_start_ms is None:
                return _Walk(judge_target(target, self._tf, rows_total), inserted)
            start = outcome.next_start_ms


@dataclass(frozen=True)
class _Walk:
    served: TargetResult | None
    inserted: int | None
    failed: bool = False
    untrusted: bool = False
    stopped: bool = False
