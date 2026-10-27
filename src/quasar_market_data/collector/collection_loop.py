"""The collection loop (S14): collect the newest closed candles of several symbols on every boundary.

Every candle reaches the writer through ``fetch_once`` (INV-007); this module only decides when to look, what a
round must obtain, how often to retry, and how to stop. See ``docs/superpowers/plans/2026-10-26-s14-collection-loop.md``.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from quasar_market_data.collector.binance_klines import KlineParseError
from quasar_market_data.collector.candle_writer import CandleWriteOutcomeUnknown, WriteReport
from quasar_market_data.collector.clock import Clock, Sleep
from quasar_market_data.collector.fetch_once import MAX_LIMIT, CandleSink, FetchOnceConfig, fetch_once
from quasar_market_data.collector.kline_source import KlineSource, SourceError
from quasar_market_data.domain.closed_candles import DEFAULT_PUBLICATION_DELAY_MS
from quasar_market_data.domain.reconcile import attempt_limit
from quasar_market_data.domain.schedule import newest_expected_open_ms, next_wake_ms
from quasar_market_data.domain.server_time import DEFAULT_TIME_TRUST, TimeTrustConfig
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import ensure_timestamp_ms
from quasar_market_data.domain.validation import ValidatedCandle


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 6
    initial_delay_ms: int = 1_000
    max_delay_ms: int = 8_000

    def __post_init__(self) -> None:
        for name in ("max_attempts", "initial_delay_ms", "max_delay_ms"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if self.initial_delay_ms < 0:
            raise ValueError(f"initial_delay_ms must be >= 0, got {self.initial_delay_ms}")
        if self.max_delay_ms < self.initial_delay_ms:
            raise ValueError("max_delay_ms must be >= initial_delay_ms")

    def delay_ms(self, failed_attempts: int) -> int:
        return min(self.initial_delay_ms * (1 << (failed_attempts - 1)), self.max_delay_ms)


class SymbolOutcome(StrEnum):
    COLLECTED = "collected"
    CONFLICTED = "conflicted"
    NOTHING_EXPECTED = "nothing_expected"
    GAVE_UP = "gave_up"
    STOPPED = "stopped"
    FATAL = "fatal"  # set only by the loop's wrapper, never reported


@dataclass(frozen=True)
class SymbolResult:
    """What happened to the round's target slot. ``None`` counts exist only in a result the coordinator built for
    a task it cancelled at grace expiry: unknown, not zero. Write uncertainty is not a field here."""

    symbol: str
    outcome: SymbolOutcome
    attempts: int | None
    inserted: int | None
    last_write: WriteReport | None
    conflict_open_times: tuple[int, ...] | None
    last_problem: str | None


class _WriteWatch:
    """Wraps a sink for a whole process run and remembers that a write's outcome was ever unknown.

    ``uncertain`` is set when ``write`` raises ``CandleWriteOutcomeUnknown`` or is interrupted by cancellation
    (the caller cannot know what the worker did). Nothing resets it: a later write may be a different batch, and
    its success proves nothing about the earlier one.
    """

    def __init__(self, inner: CandleSink) -> None:
        self._inner = inner
        self.uncertain = False

    async def write(self, candles: Sequence[ValidatedCandle], *, ingested_at_ms: int) -> WriteReport:
        try:
            return await self._inner.write(candles, ingested_at_ms=ingested_at_ms)
        except (CandleWriteOutcomeUnknown, asyncio.CancelledError):
            self.uncertain = True
            raise


async def _sleep_or_stop(sleep: Sleep, ms: int, stop: asyncio.Event) -> bool:
    """Sleep ``ms`` unless ``stop`` is set first; ``True`` when stopped. Leaves no task behind."""
    if stop.is_set():
        return True
    sleeper = asyncio.ensure_future(sleep(ms))
    stopper = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (sleeper, stopper):
            task.cancel()
        await asyncio.gather(sleeper, stopper, return_exceptions=True)
    return stop.is_set()


async def collect_symbol(
    source: KlineSource,
    sink: CandleSink,
    config: FetchOnceConfig,
    target_open_ms: int | None,
    retry: RetryPolicy,
    clock: Clock,
    sleep: Sleep,
    stop: asyncio.Event,
    limit_for_attempt: Callable[[], int] | None = None,
    observe_forming: Callable[[int, int], None] | None = None,
) -> SymbolResult:
    attempts = 0
    inserted = 0
    last_write: WriteReport | None = None
    problem: str | None = None

    def result(outcome: SymbolOutcome) -> SymbolResult:
        conflicts = tuple(sorted(c.incoming.key.open_time_ms for c in last_write.conflicts)) if last_write else ()
        return SymbolResult(config.symbol, outcome, attempts, inserted, last_write, conflicts, problem)

    while True:
        if stop.is_set():
            return result(SymbolOutcome.STOPPED)
        attempts += 1
        try:
            attempt_config = config if limit_for_attempt is None else replace(config, limit=limit_for_attempt())
            report = await fetch_once(source, sink, attempt_config, clock)
        except (SourceError, KlineParseError, CandleWriteOutcomeUnknown) as error:
            problem = str(error) or type(error).__name__
            if target_open_ms is None:
                return result(SymbolOutcome.GAVE_UP)
        else:
            if observe_forming is not None:
                returned = [c.key.open_time_ms for c in (*report.selection.eligible, *report.selection.pending)]
                if returned:
                    observe_forming(max(returned), clock.wall_ms())
            if report.write is not None:
                inserted += report.write.inserted
                last_write = report.write
            if target_open_ms is None:
                return result(SymbolOutcome.NOTHING_EXPECTED)
            eligible = {candle.key.open_time_ms for candle in report.selection.eligible}
            if target_open_ms in eligible and report.write is not None:
                conflicted = any(c.incoming.key.open_time_ms == target_open_ms for c in report.write.conflicts)
                return result(SymbolOutcome.CONFLICTED if conflicted else SymbolOutcome.COLLECTED)
            reason = report.selection.untrusted_reason
            if reason is not None:
                problem = f"time untrusted: {reason.value}"
            else:
                problem = (
                    f"target not confirmed (pending={len(report.selection.pending)},"
                    f" invalid={len(report.selection.invalid)})"
                )
        if attempts >= retry.max_attempts:
            return result(SymbolOutcome.GAVE_UP)
        if await _sleep_or_stop(sleep, retry.delay_ms(attempts), stop):
            return result(SymbolOutcome.STOPPED)


@dataclass(frozen=True)
class LoopConfig:
    symbols: tuple[str, ...]
    timeframe: Timeframe
    limit: int
    publication_delay_ms: int = DEFAULT_PUBLICATION_DELAY_MS
    time_trust: TimeTrustConfig = DEFAULT_TIME_TRUST
    max_concurrency: int = 4
    retry: RetryPolicy = RetryPolicy()
    shutdown_grace_ms: int = 10_000

    def __post_init__(self) -> None:
        if type(self.symbols) is not tuple:
            raise TypeError(f"symbols must be a tuple, got {type(self.symbols).__name__}")
        if not self.symbols:
            raise ValueError("at least one symbol is required")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be unique")
        if type(self.max_concurrency) is not int:
            raise TypeError(f"max_concurrency must be int, got {type(self.max_concurrency).__name__}")
        if self.max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {self.max_concurrency}")
        if type(self.shutdown_grace_ms) is not int:
            raise TypeError(f"shutdown_grace_ms must be int, got {type(self.shutdown_grace_ms).__name__}")
        if self.shutdown_grace_ms < 0:
            raise ValueError(f"shutdown_grace_ms must be >= 0, got {self.shutdown_grace_ms}")
        if type(self.retry) is not RetryPolicy:
            raise TypeError(f"retry must be RetryPolicy, got {type(self.retry).__name__}")
        for symbol in self.symbols:
            self.fetch_config(symbol)  # validates the symbol, the timeframe, the limit and the delay types

    def fetch_config(self, symbol: str) -> FetchOnceConfig:
        return FetchOnceConfig(
            symbol=symbol,
            timeframe=self.timeframe,
            limit=self.limit,
            publication_delay_ms=self.publication_delay_ms,
            time_trust=self.time_trust,
        )


@dataclass(frozen=True)
class RoundReport:
    target_open_ms: int | None
    started_wall_ms: int
    results: tuple[SymbolResult, ...]
    uncertain_symbols: tuple[str, ...]


@dataclass(frozen=True)
class LoopExit:
    uncertain_symbols: tuple[str, ...]


class FatalLoopError(Exception):
    def __init__(self, error: Exception, uncertain_symbols: tuple[str, ...]) -> None:
        suffix = f"; write outcome uncertain for: {', '.join(uncertain_symbols)}" if uncertain_symbols else ""
        super().__init__(f"fatal error: {error!r}{suffix}")
        self.uncertain_symbols = uncertain_symbols


async def run_collection_loop(
    source: KlineSource,
    sink: CandleSink,
    config: LoopConfig,
    clock: Clock,
    sleep: Sleep,
    stop: asyncio.Event,
    on_round: Callable[[RoundReport], None],
    *,
    first_round_wanted_from: Mapping[str, int] | None = None,
    after_first_round: Callable[[RoundReport], Awaitable[None]] | None = None,
    observe_forming_slot: Callable[[str, int, int], None] | None = None,
) -> LoopExit:
    """Collect on every boundary until ``stop`` is set; see the S14 plan, Decisions 4 to 9.

    ``stop`` is also set by the loop itself on a fatal error. In round one only, a symbol in
    ``first_round_wanted_from`` requests ``attempt_limit`` rows (computed from the clock at every attempt),
    ``observe_forming_slot`` sees the newest returned row after every successful fetch, and ``after_first_round``
    is awaited after ``on_round`` (S15 Decisions 7, 8 and 12).
    """
    tf_ms = config.timeframe.milliseconds
    wanted = dict(first_round_wanted_from or {})
    for symbol, oldest in wanted.items():
        if symbol not in config.symbols:
            raise ValueError(f"first_round_wanted_from names an unknown symbol: {symbol!r}")
        ensure_timestamp_ms(oldest, f"first_round_wanted_from[{symbol!r}]")
        if oldest % tf_ms:
            raise ValueError(f"first_round_wanted_from[{symbol!r}] {oldest} is not aligned to the timeframe")
    watches = {symbol: _WriteWatch(sink) for symbol in config.symbols}  # one per symbol for the whole run
    semaphore = asyncio.Semaphore(config.max_concurrency)
    fatal: list[Exception] = []

    def uncertain() -> tuple[str, ...]:
        return tuple(symbol for symbol in config.symbols if watches[symbol].uncertain)

    async def run_symbol(symbol: str, target: int | None, first: bool) -> SymbolResult:
        async with semaphore:
            if stop.is_set():
                return SymbolResult(symbol, SymbolOutcome.STOPPED, 0, 0, None, (), None)
            limit_for_attempt: Callable[[], int] | None = None
            observe: Callable[[int, int], None] | None = None
            if first and symbol in wanted:
                oldest = wanted[symbol]

                def limit_for_attempt() -> int:
                    return attempt_limit(
                        now_ms=clock.wall_ms(),
                        oldest_wanted_ms=oldest,
                        timeframe_ms=tf_ms,
                        default_limit=config.limit,
                        max_limit=MAX_LIMIT,
                    )

            if first and observe_forming_slot is not None:
                hook = observe_forming_slot

                def observe(open_ms: int, wall_ms: int) -> None:
                    hook(symbol, open_ms, wall_ms)

            try:
                return await collect_symbol(
                    source,
                    watches[symbol],
                    config.fetch_config(symbol),
                    target,
                    config.retry,
                    clock,
                    sleep,
                    stop,
                    limit_for_attempt,
                    observe,
                )
            except Exception as error:  # noqa: BLE001 - only fatal classes reach here; CancelledError is not an Exception
                fatal.append(error)
                stop.set()  # before the semaphore is released: queued symbols then see it and never start
                return SymbolResult(symbol, SymbolOutcome.FATAL, None, None, None, None, repr(error))

    async def run_round(target: int | None, first: bool) -> tuple[SymbolResult, ...]:
        tasks = [
            asyncio.create_task(run_symbol(symbol, target, first), name=f"collect-{symbol}")
            for symbol in config.symbols
        ]
        pending: set[asyncio.Future[Any]] = set(tasks)
        stop_wait: asyncio.Future[Any] | None = asyncio.ensure_future(stop.wait())
        grace: asyncio.Future[Any] | None = None
        expired: set[asyncio.Future[Any]] = set()
        try:
            while pending:
                waiting: set[asyncio.Future[Any]] = set(pending)
                if stop_wait is not None:
                    waiting.add(stop_wait)
                if grace is not None:
                    waiting.add(grace)
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                pending -= done
                if stop_wait is not None and stop_wait in done:
                    stop_wait = None  # served its purpose; never waited on again
                    if pending and grace is None:
                        grace = asyncio.ensure_future(sleep(config.shutdown_grace_ms))  # one timer per shutdown
                if grace is not None and grace in done:
                    expired = set(pending)
                    break
            for task in expired:
                task.cancel()
            await asyncio.gather(*expired, return_exceptions=True)
        finally:
            leftovers: list[asyncio.Future[Any]] = [f for f in (stop_wait, grace) if f is not None]
            leftovers += [task for task in tasks if not task.done()]
            for future in leftovers:
                future.cancel()
            await asyncio.gather(*leftovers, return_exceptions=True)
        results: list[SymbolResult] = []
        for symbol, task in zip(config.symbols, tasks, strict=True):
            if task in expired and task.cancelled():
                results.append(
                    SymbolResult(symbol, SymbolOutcome.STOPPED, None, None, None, None, "shutdown grace expired")
                )
            else:
                results.append(task.result())
        return tuple(results)

    tf, delay = config.timeframe, config.publication_delay_ms
    first_round = True
    while not stop.is_set():
        started = clock.wall_ms()
        target = newest_expected_open_ms(started, tf, delay)
        results = await run_round(target, first_round)
        if fatal:
            raise FatalLoopError(fatal[0], uncertain()) from fatal[0]
        report = RoundReport(target, started, results, uncertain())
        on_round(report)
        if first_round and after_first_round is not None and not stop.is_set():
            try:
                await after_first_round(report)
            except Exception as error:
                stop.set()
                raise FatalLoopError(error, uncertain()) from error
        first_round = False
        if stop.is_set():
            break
        now = clock.wall_ms()
        await _sleep_or_stop(sleep, next_wake_ms(now, tf, delay) - now, stop)
    return LoopExit(uncertain())
