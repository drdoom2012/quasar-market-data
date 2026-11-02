"""The open interest task: rounds on the derived cadence (ADR-0003 P-9, P-10).

One asyncio task per Collector process, started by ``run`` after the candle loop's first round and its post-check.
It owns its source, writer, budget and gate (nothing is shared with candle collection) and never imports the candle
rate limiter, the shared server time or the collection loop (import contract). Any exception that leaves
``run_open_interest_task`` ends this task only: the caller prints one ``oi-error`` line; the task is not restarted.

Rounds. The round for boundary ``B`` wakes at ``B + round_offset_ms`` and is cancelled at ``B + 300 s + offset - 15 s``.
Rounds are sequential in this one task, so two rounds never run concurrently; ``next_round`` runs a late round that is
still inside its deadline and counts the ones it skips. A round observes server time once; the lower bound ``L`` taken
right after that observation is the ``as_of`` and the eligibility bound of **every** request of the round (the trust
gate expires after 60 s, a lower bound only gets more conservative as time passes). Without trusted time the round
makes no history request at all. Each symbol of the round (``symbols_for_round``) asks for ``[B - 2c * 5 min, B]``; rows
are parsed all-or-nothing, only eligible rows are written. A row that Binance has not published yet is simply absent:
not an error, not retried; the symbol's next overlapping request or the catch-up stores it. A request error (not a
refusal, a pause or a ban) gets one retry after ``RETRY_DELAY_MS`` if that fits before the deadline.

429 / 418. ``OpenInterestPaused`` stops the round at once (the remaining symbols are ``paused``) and the task goes on
with the next round; ``OpenInterestBanned`` ends the task. At the round deadline unfinished symbols are cancelled and
logged with their phase; rows still waiting for the candle writer are dropped unwritten (P-9 rule 2) and the next
overlapping request stores them.

Output is one injected callable ``emit(line)``; the families are ``oi-round``, ``oi-conflict``, ``oi-warning``,
``oi-measurement`` (here) and ``oi-reconcile`` and ``oi-catchup`` (catch-up).
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from quasar_market_data.collector.async_open_interest_writer import OpenInterestPageError, OpenInterestWriteReport
from quasar_market_data.collector.binance_open_interest import OpenInterestParseError, parse_open_interest_hist
from quasar_market_data.collector.binance_usdm_open_interest import MAX_IN_FLIGHT, OpenInterestRequestRefused
from quasar_market_data.collector.candle_write_activity import CandleWriteActivity
from quasar_market_data.collector.clock import Clock, Sleep
from quasar_market_data.collector.fetch_open_interest_once import OpenInterestOnceSource, OpenInterestSink
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.open_interest_budget import OpenInterestBudget
from quasar_market_data.collector.open_interest_gate import OpenInterestBanned, OpenInterestPaused
from quasar_market_data.collector.open_interest_measurements import OpenInterestMeasurements
from quasar_market_data.collector.open_interest_writer import (
    OpenInterestWriteBusy,
    OpenInterestWriteError,
    OpenInterestWriteOutcomeUnknown,
)
from quasar_market_data.domain.open_interest import OpenInterest, select_eligible_open_interest
from quasar_market_data.domain.open_interest_cadence import open_interest_cadence, round_cost, symbols_for_round
from quasar_market_data.domain.open_interest_schedule import next_round, round_deadline_ms, round_range
from quasar_market_data.domain.server_time import (
    DEFAULT_TIME_TRUST,
    ServerTimeObservation,
    TimeTrustConfig,
    TrustedTime,
    server_lower_bound,
)
from quasar_market_data.domain.timeframe import Timeframe

RETRY_DELAY_MS: Final = 1_000  # the first step of the candle RetryPolicy, copied
MEASUREMENT_WINDOW_MS: Final = 3_600_000
PROBLEM_MAX_CHARS: Final = 200
_NS_PER_MS: Final = 1_000_000

type Emit = Callable[[str], None]


class OpenInterestWriterUnusable(Exception):
    """A write failed in a way that is neither busy nor an unknown outcome; the writer cannot be relied on any more."""


class OpenInterestSymbolOutcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    PAUSED = "paused"
    DEADLINE = "deadline"
    STOPPED = "stopped"
    REFUSED = "refused"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class OpenInterestTaskConfig:
    venue: str
    symbols: tuple[str, ...]
    period: Timeframe
    publication_delay_ms: int
    round_offset_ms: int
    budget_per_5min: int
    time_trust: TimeTrustConfig = DEFAULT_TIME_TRUST

    def __post_init__(self) -> None:
        if type(self.symbols) is not tuple:
            raise TypeError(f"symbols must be a tuple, got {type(self.symbols).__name__}")
        if not self.symbols:
            raise ValueError("at least one symbol is required")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be unique")
        object.__setattr__(self, "symbols", tuple(sorted(self.symbols)))
        if type(self.publication_delay_ms) is not int or self.publication_delay_ms < 0:
            raise ValueError(f"publication_delay_ms must be an int >= 0, got {self.publication_delay_ms!r}")
        open_interest_cadence(len(self.symbols), self.budget_per_5min)  # CadenceError / ValueError: fail at once

    @property
    def cadence(self) -> int:
        return open_interest_cadence(len(self.symbols), self.budget_per_5min)

    @property
    def round_cost(self) -> int:
        return round_cost(len(self.symbols), self.cadence)


@dataclass(frozen=True)
class OpenInterestRoundReport:
    boundary_ms: int
    symbols: tuple[str, ...]
    outcomes: Mapping[str, OpenInterestSymbolOutcome]
    stored: int
    duplicates: int
    conflicts: int
    pending: int
    skipped_rounds: int
    late_ms: int

    def line(self) -> str:
        counts = {outcome: 0 for outcome in OpenInterestSymbolOutcome}
        for outcome in self.outcomes.values():
            counts[outcome] += 1
        fields = [f"oi-round boundary={self.boundary_ms}", f"symbols={len(self.symbols)}"]
        fields += [f"{outcome.value}={count}" for outcome, count in counts.items()]
        fields += [
            f"stored={self.stored}",
            f"duplicates={self.duplicates}",
            f"conflicts={self.conflicts}",
            f"pending={self.pending}",
            f"skipped_rounds={self.skipped_rounds}",
            f"late_ms={self.late_ms}",
        ]
        return " ".join(fields)


@dataclass
class _RoundState:
    symbols: tuple[str, ...]
    outcomes: dict[str, OpenInterestSymbolOutcome] = field(default_factory=dict)
    phases: dict[str, str] = field(default_factory=dict)
    stored: int = 0
    duplicates: int = 0
    conflicts: int = 0
    pending: int = 0
    paused: bool = False


async def sleep_or_stop(sleep: Sleep, ms: int, stop: asyncio.Event) -> bool:
    """Sleep ``ms`` unless ``stop`` is set first; ``True`` when stopped. Leaves no task behind.

    A copy of ``collection_loop._sleep_or_stop``: this module must not import the loop (import contract); keep the two in step.
    """
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


def _problem(error: BaseException) -> str:
    text = " ".join((str(error) or type(error).__name__).split())
    return f"{type(error).__name__}: {text}"[:PROBLEM_MAX_CHARS]


def _oi_text(row: OpenInterest) -> str:
    return f"{row.open_interest}/{row.open_interest_value}"


class _OpenInterestTask:
    def __init__(
        self,
        source: OpenInterestOnceSource,
        writer: OpenInterestSink,
        db: Path,
        config: OpenInterestTaskConfig,
        budget: OpenInterestBudget,
        activity: CandleWriteActivity,
        clock: Clock,
        sleep: Sleep,
        stop: asyncio.Event,
        emit: Emit,
        measurements: OpenInterestMeasurements,
    ) -> None:
        self.source, self.writer, self.db, self.config = source, writer, db, config
        self.budget, self.activity = budget, activity
        self.clock, self.sleep, self.stop, self.emit = clock, sleep, stop, emit
        self.measurements = measurements
        self._measure_started_ns = clock.monotonic_ns()

    # ---- schedule -------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        config = self.config
        last: int | None = None
        while not self.stop.is_set():
            self._emit_measurements(force=False)
            now = self.clock.wall_ms()
            nxt = next_round(now, last, config.round_offset_ms, config.period)
            wake_ns = self.clock.monotonic_ns() + (nxt.wake_ms - now) * _NS_PER_MS
            if nxt.wake_ms > now and await self._wait_until(wake_ns, nxt.wake_ms):
                break
            started_ns = self.clock.monotonic_ns()
            deadline_ms = round_deadline_ms(nxt.boundary_ms, config.round_offset_ms, config.period)
            deadline_ns = started_ns + (deadline_ms - self.clock.wall_ms()) * _NS_PER_MS
            late_ms = max(0, (started_ns - wake_ns) // _NS_PER_MS)
            report = await self._round(nxt.boundary_ms, deadline_ns, nxt.skipped, late_ms)
            last = nxt.boundary_ms
            self.emit(report.line())
        self._emit_measurements(force=True)

    async def _wait_until(self, wake_ns: int, wake_ms: int) -> bool:
        """Wait for the next round's wake; ``True`` when stopped. (Catch-up fills this wait.)"""
        remaining_ns = wake_ns - self.clock.monotonic_ns()
        return await sleep_or_stop(self.sleep, -(-remaining_ns // _NS_PER_MS), self.stop)

    def _emit_measurements(self, *, force: bool) -> None:
        elapsed_ms = (self.clock.monotonic_ns() - self._measure_started_ns) // _NS_PER_MS
        if not force and elapsed_ms < MEASUREMENT_WINDOW_MS:
            return
        line = self.measurements.line(elapsed_ms)
        if line is not None:
            self.emit(line)
        self.measurements.reset()
        self._measure_started_ns = self.clock.monotonic_ns()

    # ---- one scope that is cancelled at a deadline or on stop -----------------------------------------------------

    async def _run_until[T](self, work: Awaitable[T], deadline_ns: int) -> tuple[str, T | None]:
        """Run ``work`` until it ends, the monotonic ``deadline_ns`` passes or ``stop`` is set; leaves no task behind."""
        job = asyncio.ensure_future(work)
        remaining_ns = max(0, deadline_ns - self.clock.monotonic_ns())
        timer = asyncio.ensure_future(self.sleep(max(1, -(-remaining_ns // _NS_PER_MS))))
        stopper = asyncio.ensure_future(self.stop.wait())
        try:
            await asyncio.wait({job, timer, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if job.done():
                return "done", job.result()
            return ("stopped" if self.stop.is_set() else "deadline"), None
        finally:
            for future in (job, timer, stopper):
                future.cancel()
            await asyncio.gather(job, timer, stopper, return_exceptions=True)

    # ---- one round ------------------------------------------------------------------------------------------------

    async def _round(self, boundary: int, deadline_ns: int, skipped: int, late_ms: int) -> OpenInterestRoundReport:
        config = self.config
        symbols = symbols_for_round(config.symbols, boundary, config.cadence, config.period)
        state = _RoundState(symbols)
        status, _ = await self._run_until(self._round_work(boundary, state, deadline_ns), deadline_ns)
        if status != "done":
            self._close_unfinished(boundary, state, status)
        return OpenInterestRoundReport(
            boundary,
            symbols,
            dict(state.outcomes),
            state.stored,
            state.duplicates,
            state.conflicts,
            state.pending,
            skipped,
            late_ms,
        )

    def _close_unfinished(self, boundary: int, state: _RoundState, status: str) -> None:
        outcome = OpenInterestSymbolOutcome.DEADLINE if status == "deadline" else OpenInterestSymbolOutcome.STOPPED
        left: list[str] = []
        for symbol in state.symbols:
            if symbol in state.outcomes:
                continue
            phase = state.phases.get(symbol, "waiting")
            if phase == "writing" and self.activity.active:
                phase = "waiting-for-candles"
            left.append(f"{symbol}:{phase}")
            state.outcomes[symbol] = outcome
        if left and status == "deadline":
            self.emit(
                f"oi-warning round-deadline boundary={boundary} unfinished={','.join(left)} rows-dropped-unwritten"
            )

    async def _observe(self) -> ServerTimeObservation | None:
        try:
            return await self.source.fetch_server_time()
        except (OpenInterestPaused, OpenInterestBanned):
            raise
        except SourceError:
            return None  # a failed time request means no observation

    async def _round_work(self, boundary: int, state: _RoundState, deadline_ns: int) -> None:
        config = self.config
        if not state.symbols:
            return
        try:
            observation = await self._observe()
        except OpenInterestPaused:
            self._mark_all(state, OpenInterestSymbolOutcome.PAUSED)
            self.emit(f"oi-warning round-paused boundary={boundary} time request answered 429")
            return
        trust = server_lower_bound(
            observation, self.clock.monotonic_ns(), self.clock.wall_ms(), None, config.time_trust
        )
        if not isinstance(trust, TrustedTime):
            self._mark_all(state, OpenInterestSymbolOutcome.UNTRUSTED)
            self.emit(
                f"oi-warning round-untrusted-time boundary={boundary} reason={trust.reason.value} no request made"
            )
            return
        start, end = round_range(boundary, config.cadence, config.period)
        semaphore = asyncio.Semaphore(MAX_IN_FLIGHT)
        tasks = [
            asyncio.create_task(self._symbol(symbol, boundary, start, end, trust, semaphore, state, deadline_ns))
            for symbol in state.symbols
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _mark_all(state: _RoundState, outcome: OpenInterestSymbolOutcome) -> None:
        for symbol in state.symbols:
            state.outcomes.setdefault(symbol, outcome)

    async def _symbol(
        self,
        symbol: str,
        boundary: int,
        start: int,
        end: int,
        trust: TrustedTime,
        semaphore: asyncio.Semaphore,
        state: _RoundState,
        deadline_ns: int,
    ) -> None:
        problem = ""
        for attempt in (1, 2):
            async with semaphore:  # the slot is held for one attempt, never across the retry delay
                if state.paused or self.stop.is_set():
                    state.outcomes[symbol] = (
                        OpenInterestSymbolOutcome.PAUSED if state.paused else OpenInterestSymbolOutcome.STOPPED
                    )
                    return
                state.phases[symbol] = "fetching"
                try:
                    await self._collect(symbol, start, end, trust, state)
                except OpenInterestPaused:
                    state.paused = True
                    state.outcomes[symbol] = OpenInterestSymbolOutcome.PAUSED
                    self.emit(f"oi-warning round-paused boundary={boundary} symbol={symbol} Binance answered 429")
                    return
                except OpenInterestBanned:
                    raise
                except OpenInterestRequestRefused as error:
                    state.outcomes[symbol] = OpenInterestSymbolOutcome.REFUSED
                    self.emit(f"oi-error request-refused boundary={boundary} symbol={symbol} {_problem(error)}")
                    return
                except (SourceError, OpenInterestParseError) as error:
                    problem = _problem(error)
                except OpenInterestWriteError as error:
                    problem = f"write {self._write_problem(error)}: {_problem(error)}"
                else:
                    state.outcomes[symbol] = OpenInterestSymbolOutcome.OK
                    return
            if attempt == 2:
                break
            retry_at_ns = self.clock.monotonic_ns() + RETRY_DELAY_MS * _NS_PER_MS
            if retry_at_ns >= deadline_ns or await sleep_or_stop(self.sleep, RETRY_DELAY_MS, self.stop):
                break
        state.outcomes[symbol] = OpenInterestSymbolOutcome.FAILED
        self.emit(f"oi-warning symbol-failed boundary={boundary} symbol={symbol} {problem}")

    def _write_problem(self, error: OpenInterestWriteError) -> str:
        """``busy`` and ``outcome-unknown`` are retried; anything else makes the writer unusable and ends the task."""
        cause = error.cause if isinstance(error, OpenInterestPageError) else error
        if isinstance(cause, OpenInterestWriteBusy):
            self.measurements.record_busy()
            return "busy"
        if isinstance(cause, OpenInterestWriteOutcomeUnknown):
            return "outcome-unknown"
        raise OpenInterestWriterUnusable(_problem(error)) from error

    async def _collect(self, symbol: str, start: int, end: int, trust: TrustedTime, state: _RoundState) -> None:
        config = self.config
        response = await self.source.fetch_open_interest_hist(symbol, start, end, as_of_ms=trust.lower_bound_ms)
        rows = parse_open_interest_hist(
            response,
            market_id=self.source.market_id(symbol),
            symbol=symbol,
            period=config.period,
            start_time_ms=start,
            end_time_ms=end,
        )
        selection = select_eligible_open_interest(rows, config.publication_delay_ms, trust)
        state.pending += len(selection.pending)
        if not selection.eligible:
            return
        state.phases[symbol] = "writing"
        report = await self._write(selection.eligible)
        state.stored += report.inserted
        state.duplicates += report.duplicates
        state.conflicts += len(report.conflicts)
        self._report_conflicts(report)

    async def _write(self, rows: Sequence[OpenInterest]) -> OpenInterestWriteReport:
        try:
            report = await self.writer.write(rows, ingested_at_ms=self.clock.wall_ms())
        except OpenInterestPageError as error:
            self.measurements.record_open_interest_write(error.partial, len(rows))
            raise
        self.measurements.record_open_interest_write(report, len(rows))
        return report

    def _report_conflicts(self, report: OpenInterestWriteReport) -> None:
        for conflict in report.conflicts:
            if conflict.newly_recorded:
                key = conflict.incoming.key
                self.emit(
                    f"oi-conflict venue={key.venue} symbol={key.symbol} time_ms={key.time_ms}"
                    f" stored={_oi_text(conflict.stored)} incoming={_oi_text(conflict.incoming)}"
                )


async def run_open_interest_task(
    source: OpenInterestOnceSource,
    writer: OpenInterestSink,
    db: Path,
    config: OpenInterestTaskConfig,
    budget: OpenInterestBudget,
    activity: CandleWriteActivity,
    clock: Clock,
    sleep: Sleep,
    stop: asyncio.Event,
    emit: Emit,
    measurements: OpenInterestMeasurements,
) -> None:
    """Run rounds until ``stop`` is set. Raises on a fatal condition without logging it; never restarts itself."""
    await _OpenInterestTask(source, writer, db, config, budget, activity, clock, sleep, stop, emit, measurements).run()
