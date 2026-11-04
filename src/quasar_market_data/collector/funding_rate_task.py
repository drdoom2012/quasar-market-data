"""The funding rate task: one dispatcher for rounds, catch-up and backfill (ADR-0004 P-9 to P-11).

One asyncio task per Collector process, started by ``run`` after the candle loop's first round and its post-check.
It owns its source, limiter, pause and writer (nothing is shared with candle collection) and never imports the candle
rate limiter, the shared server time, the collection loop or an open interest module (import contract). Any
exception that leaves ``run_funding_rate_task`` ends this task only: the caller prints one ``funding-error`` line;
the task is not restarted.

The dispatcher. Whenever a worker slot is free and the limiter has room for the class's group, the pure
``select_next`` (P-10 selection rules) names the next request; the dispatcher starts a worker for that **single
page**, yields once so the worker's reservation in the limiter exists, and decides again. A worker runs
``execute_plan`` (fetch, parse, select, write with the final-transaction ``merge``, P-7) under a request timeout, and
the dispatcher applies the result to the in-memory state: the span from the writer's report, ``continuing`` from a
truncated page, the history chunk, the failure record. Rounds, catch-up and history run in this one loop at the same
time; a round only enables classes ``S`` and ``R``.

Time. ``b_now = L - publication_delay`` where ``L`` is evaluated **once right after each observation** (one at every
round's wake, class ``R``; one at the start only when catch-up has work and the next wake is more than the limiter
window away, so the extra start cannot delay the first round). A lower bound stays a lower bound as time passes, so
``b_now`` is the end of every forward request until the next observation. Round boundaries are wall-clock; deadlines,
retry times and suspensions are on the monotonic clock.

Rounds. The round of hour ``B`` wakes at ``B + round_offset_ms`` and has the deadline ``B + 1 h - 15 s``. At the
deadline the ``S``/``R`` workers are cancelled (the page's outcome is then unknown: the series is re-read from the
database before it is planned again), series not asked are counted as skipped and **nothing is marked failed**.
Series that are ``continuing`` belong to class ``C`` and are not skipped by a round.

Failures. A failed request counts toward the series' retry delay and suspension (``funding_rate_suspension``). A
``429`` is not a series failure: the dispatcher waits until the pause ends. A ``418`` and an unusable writer end the
task by raising. Everything runs on the event loop; there is no locking.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from quasar_market_data.collector.binance_funding_rate import FundingRateParseError
from quasar_market_data.collector.binance_usdm_funding_rate import FundingRateRequestRefused
from quasar_market_data.collector.clock import Clock, Sleep
from quasar_market_data.collector.extension_pause import ExtensionPause, ExtensionPaused, ExtensionStopped
from quasar_market_data.collector.fetch_funding_rate_once import FundingRateSelectionError, FundingRateSink
from quasar_market_data.collector.funding_rate_execution import (
    FundingRateExecutionError,
    FundingRatePageSource,
    PageResult,
    execute_plan,
)
from quasar_market_data.collector.funding_rate_limiter import MAX_IN_FLIGHT, FundingRateLimiter, RequestClass
from quasar_market_data.collector.funding_rate_measurements import FundingRateMeasurements
from quasar_market_data.collector.funding_rate_reader import read_series_facts, read_span
from quasar_market_data.collector.funding_rate_writer import (
    FundingRateConflict,
    FundingRateWriteBusy,
    FundingRateWriteError,
    FundingRateWriteOutcomeUnknown,
    FundingSpanNotAdjacentError,
)
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.funding_rate import forward_end_bound_ms
from quasar_market_data.domain.funding_rate_capacity import LIMITER_WINDOW_MS
from quasar_market_data.domain.funding_rate_schedule import (
    RoundPlan,
    next_round,
    round_deadline_ms,
)
from quasar_market_data.domain.funding_rate_scheduler import (
    ClassTurn,
    HistoryChunk,
    Plan,
    SeriesStatus,
    select_next,
)
from quasar_market_data.domain.funding_rate_span import HISTORY_CHUNK_MS, Interval, history_pending, is_behind
from quasar_market_data.domain.funding_rate_suspension import (
    FailureRecord,
    on_failure,
    on_success,
    unavailable_until_ms,
)
from quasar_market_data.domain.server_time import (
    DEFAULT_TIME_TRUST,
    ServerTimeObservation,
    TimeTrustConfig,
    TrustedTime,
    server_lower_bound,
)

REQUEST_TIMEOUT_MS: Final = 30_000  # above CCXT's own 10 s: a hung request fails the series, never a round or `stop`
MEASUREMENT_WINDOW_MS: Final = 3_600_000
_NS_PER_MS: Final = 1_000_000
_PROBLEM_MAX_CHARS: Final = 300
_RESERVATION_HOPS: Final = 50  # loop iterations a started worker may need to reach the limiter

type Emit = Callable[[str], None]


class FundingRateWriterUnusable(Exception):
    """A write failed in a way that is neither busy, an unknown outcome nor a refused merge: the writer cannot be relied on."""


class FundingRateTaskSource(FundingRatePageSource, Protocol):
    """``BinanceUsdmFundingRateSource`` satisfies it."""

    async def fetch_server_time(self, request_class: RequestClass = RequestClass.R) -> ServerTimeObservation: ...


@dataclass(frozen=True)
class FundingRateTaskConfig:
    venue: str
    symbols: tuple[str, ...]
    publication_delay_ms: int
    round_offset_ms: int
    budget_per_5min: int
    backfill_days: int
    recheck_ms: int
    time_trust: TimeTrustConfig = DEFAULT_TIME_TRUST

    def __post_init__(self) -> None:
        if type(self.symbols) is not tuple:
            raise TypeError(f"symbols must be a tuple, got {type(self.symbols).__name__}")
        if not self.symbols:
            raise ValueError("at least one symbol is required")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be unique")
        object.__setattr__(self, "symbols", tuple(sorted(self.symbols)))
        for name in ("publication_delay_ms", "round_offset_ms", "budget_per_5min", "backfill_days", "recheck_ms"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
        if self.publication_delay_ms < 0:
            raise ValueError(f"publication_delay_ms must be >= 0, got {self.publication_delay_ms}")
        # the ranges of the configuration table are validated there; the limiter validates the budget


def _problem(error: BaseException) -> str:
    text = " ".join((str(error) or type(error).__name__).split())
    return f"{type(error).__name__}: {text}"[:_PROBLEM_MAX_CHARS]


@dataclass
class _Series:
    symbol: str
    span: Interval | None = None
    verified_at_ms: int | None = None
    busy: bool = False
    failures: FailureRecord = field(default_factory=FailureRecord)
    continuing_from_ms: int | None = None
    history_chunk: HistoryChunk | None = None
    never_served: bool = True
    last_served: int = 0
    asked_in_round: bool = False
    needs_reread: bool = False
    _view: SeriesStatus | None = field(default=None, repr=False)

    def touch(self) -> None:
        self._view = None

    def view(self) -> SeriesStatus:
        if self._view is None:
            self._view = SeriesStatus(
                symbol=self.symbol,
                span=self.span,
                verified_at_ms=self.verified_at_ms,
                busy=self.busy,
                unavailable_until_ms=unavailable_until_ms(self.failures),
                continuing_from_ms=self.continuing_from_ms,
                history_chunk=self.history_chunk,
                never_served=self.never_served,
                last_served=self.last_served,
                asked_in_round=self.asked_in_round,
                needs_reread=self.needs_reread,
            )
        return self._view


@dataclass
class _Round:
    boundary_ms: int
    deadline_ns: int
    started_ns: int
    skipped_before: int
    late_ms: int
    asked: int = 0
    failed: int = 0


@dataclass
class _Opening:
    """A round whose server-time observation is in flight: the dispatcher goes on with catch-up and history meanwhile."""

    plan: RoundPlan
    deadline_ns: int
    started_ns: int
    late_ms: int
    task: "asyncio.Task[bool]"


@dataclass
class _Worker:
    plan: Plan
    series: _Series
    started_ns: int


@dataclass(frozen=True)
class _Failed:
    error: BaseException


@dataclass(frozen=True)
class _Paused:
    pass


def _ns(ms: int) -> int:
    return ms * _NS_PER_MS


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


class _FundingRateTask:
    def __init__(
        self,
        source: FundingRateTaskSource,
        writer: FundingRateSink,
        db: Path,
        config: FundingRateTaskConfig,
        limiter: FundingRateLimiter,
        pause: ExtensionPause,
        clock: Clock,
        sleep: Sleep,
        stop: asyncio.Event,
        emit: Emit,
        measurements: FundingRateMeasurements,
    ) -> None:
        self.source, self.writer, self.db, self.config = source, writer, db, config
        self.limiter, self.pause = limiter, pause
        self.clock, self.sleep, self.stop, self.emit = clock, sleep, stop, emit
        self.measurements = measurements
        self.series = {symbol: _Series(symbol) for symbol in config.symbols}
        self.b_now_ms: int | None = None
        self.turn: ClassTurn = "C"
        self.round: _Round | None = None
        self.last_boundary_ms: int | None = None
        self.workers: dict[asyncio.Task[PageResult | _Failed | _Paused], _Worker] = {}
        self._sequence = 0
        self._behind_marked = False
        self._measure_started_ns = clock.monotonic_ns()
        self._next_plan: RoundPlan | None = None
        self.opening: _Opening | None = None
        self._pause_until_ns: int | None = None  # set by an observation that met a 429 pause
        self._open_not_before_ns: int | None = None  # a round delayed by a pause that ends before its deadline

    # ---- lifecycle ------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        try:
            await self._start_up()
            await self._loop()
        finally:
            try:
                if self.opening is not None:
                    self.opening.task.cancel()
                    await asyncio.gather(self.opening.task, return_exceptions=True)
                    self.opening = None
                await self._cancel_workers(list(self.workers))
            finally:
                self._emit_measurements(force=True)

    def _now_ms(self) -> int:
        return self.clock.monotonic_ns() // _NS_PER_MS

    # ---- start-up reconcile (P-11) --------------------------------------------------------------------------------

    async def _start_up(self) -> None:
        config = self.config
        estimate = self.clock.wall_ms() - config.publication_delay_ms
        totals = [0, 0, 0, 0]
        for index, symbol in enumerate(config.symbols):
            if index % 16 == 0:
                await asyncio.sleep(0)  # reading 1 000 series is I/O work: let the candle loop run in between
            facts = await read_series_facts(self.db, config.venue, symbol)
            state = self.series[symbol]
            state.span, state.verified_at_ms = facts.span, facts.verified_at_ms
            behind = facts.span is not None and is_behind(estimate, facts.span)
            pending = facts.span is not None and history_pending(facts.span, config.backfill_days)
            chunks = 0
            if facts.span is not None and pending:
                target = facts.span.through_ms - config.backfill_days * 86_400_000
                chunks = -(-(facts.span.from_ms - target) // HISTORY_CHUNK_MS)
            totals[0] += facts.stored_rows
            totals[1] += facts.conflicted_events
            totals[2] += int(behind)
            totals[3] += chunks
            span_text = "none" if facts.span is None else f"{facts.span.from_ms}..{facts.span.through_ms}"
            self.emit(
                f"funding-reconcile symbol={symbol} state={'unseeded' if facts.span is None else 'active'}"
                f" span={span_text} verified_at_ms={facts.verified_at_ms if facts.verified_at_ms is not None else 'none'}"
                f" stored={facts.stored_rows} conflicted_events={facts.conflicted_events}"
                f" behind_estimate={'yes' if behind else 'no'} history_pending={'yes' if pending else 'no'}"
                f" history_chunks_planned={chunks}"
            )
            if self.stop.is_set():
                return
        self.emit(
            f"funding-reconcile summary symbols={len(config.symbols)} stored={totals[0]} conflicted_events={totals[1]}"
            f" behind_estimate={totals[2]} history_chunks_planned={totals[3]}"
        )
        now = self.clock.wall_ms()
        plan = next_round(now, None, config.round_offset_ms)
        needs_bound = totals[2] > 0
        if needs_bound and plan.wake_ms - now > LIMITER_WINDOW_MS:
            await self._observe(deadline_ns=self.clock.monotonic_ns() + _ns(REQUEST_TIMEOUT_MS))

    def _mark_behind(self) -> None:
        """P-11 step 2, once, with the first trusted bound: a series that is behind after a restart is class C."""
        if self._behind_marked or self.b_now_ms is None:
            return
        self._behind_marked = True
        for state in self.series.values():
            if state.span is not None and state.continuing_from_ms is None and is_behind(self.b_now_ms, state.span):
                state.continuing_from_ms = state.span.through_ms + 1
                state.touch()

    # ---- time ----------------------------------------------------------------------------------------------------

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

    async def _observe(self, deadline_ns: int) -> bool:
        """One class-R server-time observation; sets ``b_now`` and returns ``True`` when time is trusted."""
        self._pause_until_ns = None
        try:
            status, observation = await self._run_until(self.source.fetch_server_time(RequestClass.R), deadline_ns)
        except ExtensionStopped:
            raise
        except ExtensionPaused as paused:
            self._pause_until_ns = paused.until_monotonic_ns
            self.emit("funding-warning observation-paused the time request met a 429 pause; no bound taken")
            return False
        except SourceError as error:
            self.emit(f"funding-warning observation-failed {_problem(error)}")
            return False
        if status != "done" or observation is None:
            return False
        trust = server_lower_bound(
            observation, self.clock.monotonic_ns(), self.clock.wall_ms(), None, self.config.time_trust
        )
        if not isinstance(trust, TrustedTime):
            self.emit(f"funding-warning observation-untrusted reason={trust.reason.value} no bound taken")
            return False
        self.b_now_ms = forward_end_bound_ms(self.config.publication_delay_ms, trust.lower_bound_ms)
        self._mark_behind()
        return True

    # ---- the loop ------------------------------------------------------------------------------------------------

    async def _loop(self) -> None:
        while not self.stop.is_set():
            self._emit_measurements(force=False)
            self._drain()
            await self._reread_marked()
            await self._rounds()
            if self.stop.is_set():
                return
            if await self._dispatch():
                continue
            await self._wait()

    def _drain(self) -> None:
        for task in [t for t in self.workers if t.done()]:
            worker = self.workers.pop(task)
            worker.series.busy = False
            worker.series.touch()
            self._apply(worker, task.result())  # a fatal condition raised inside a worker ends the task here

    async def _cancel_workers(self, tasks: list[asyncio.Task[PageResult | _Failed | _Paused]]) -> None:
        try:
            self._drain()  # a page that has already ended is applied, not thrown away (a retry cannot report it again)
        finally:
            running = [t for t in tasks if t in self.workers and not t.done()]
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            for task in running:
                worker = self.workers.pop(task, None)
                if worker is not None:
                    worker.series.busy = False
                    worker.series.needs_reread = (
                        True  # the page's outcome is unknown: read the span before planning again
                    )
                    worker.series.touch()

    async def _reread_marked(self) -> None:
        for state in self.series.values():
            if state.needs_reread and not state.busy:
                span, verified = await read_span(self.db, self.config.venue, state.symbol)
                state.span, state.verified_at_ms = span, verified
                chunk = state.history_chunk
                if chunk is not None and (span is None or span.from_ms != chunk.through_ms + 1):
                    state.history_chunk = None  # the chunk was claimed (or never fits): plan from the span again
                if state.continuing_from_ms is not None and span is not None:
                    state.continuing_from_ms = span.through_ms + 1  # re-fetching overlapping rows is idempotent
                state.needs_reread = False
                state.touch()

    # ---- rounds (P-10) --------------------------------------------------------------------------------------------

    def _round_pending(self, deadline_ns: int) -> int:
        """Series a round still has to ask: not asked, not continuing (class C owns them), not suspended past the deadline."""
        deadline_ms = deadline_ns // _NS_PER_MS
        return sum(
            1
            for s in self.series.values()
            if not s.asked_in_round and s.continuing_from_ms is None and unavailable_until_ms(s.failures) < deadline_ms
        )

    async def _rounds(self) -> None:
        if self.opening is not None:
            if not self.opening.task.done():
                return
            opening, self.opening = self.opening, None
            self._finish_opening(opening, opening.task.result())
            return
        round_ = self.round
        if round_ is not None:
            expired = self.clock.monotonic_ns() >= round_.deadline_ns
            running = [t for t, w in self.workers.items() if w.plan.kind in ("S", "R")]
            pending = self._round_pending(round_.deadline_ns)
            if expired:
                await self._cancel_workers(running)
                self._close_round(round_, self._round_pending(round_.deadline_ns))
            elif pending == 0 and not running:
                self._close_round(round_, 0)
            return
        now = self.clock.wall_ms()
        plan = self._next_plan = next_round(now, self.last_boundary_ms, self.config.round_offset_ms)
        if plan.wake_ms > now:
            return
        if self._open_not_before_ns is not None and self.clock.monotonic_ns() < self._open_not_before_ns:
            return  # delayed by a 429 pause that ends before the round's deadline
        self._open_round(plan, now)

    def _open_round(self, plan: RoundPlan, now_wall_ms: int) -> None:
        """Start the round's server-time observation as a job beside the workers (it may wait for room, or hang)."""
        started_ns = self.clock.monotonic_ns()
        deadline_ns = started_ns + _ns(round_deadline_ms(plan.boundary_ms) - now_wall_ms)
        late_ms = max(0, now_wall_ms - plan.wake_ms)
        if plan.skipped and self._open_not_before_ns is None:
            self.emit(
                f"funding-warning rounds-skipped skipped={plan.skipped} before boundary={plan.boundary_ms}"
                " past their deadline"
            )
        self._open_not_before_ns = None
        task = asyncio.ensure_future(self._observe(deadline_ns))
        self.opening = _Opening(plan, deadline_ns, started_ns, late_ms, task)

    def _finish_opening(self, opening: _Opening, trusted: bool) -> None:
        plan = opening.plan
        if self.stop.is_set():
            return
        if not trusted:
            until = self._pause_until_ns
            if until is not None and until < opening.deadline_ns:  # a pause delays the round; it does not cancel it
                self._open_not_before_ns = until
                self.emit(f"funding-warning round-delayed boundary={plan.boundary_ms} until the 429 pause ends")
                return
            self.last_boundary_ms = plan.boundary_ms
            self.emit(f"funding-warning round-skipped boundary={plan.boundary_ms} no trusted server time")
            return
        for state in self.series.values():
            state.asked_in_round = False
            state.touch()
        self.round = _Round(plan.boundary_ms, opening.deadline_ns, opening.started_ns, plan.skipped, opening.late_ms)

    def _close_round(self, round_: _Round, skipped: int) -> None:
        self.round = None
        self.last_boundary_ms = round_.boundary_ms
        self.measurements.record_skipped(skipped)
        suspended = sum(
            1 for s in self.series.values() if unavailable_until_ms(s.failures) >= round_.deadline_ns // _NS_PER_MS
        )
        self.emit(
            f"funding-round boundary={round_.boundary_ms} symbols={len(self.series)} asked={round_.asked}"
            f" failed={round_.failed} skipped={skipped} suspended={suspended} skipped_rounds={round_.skipped_before}"
            f" late_ms={round_.late_ms}"
        )

    # ---- selection and workers ----------------------------------------------------------------------------------

    async def _dispatch(self) -> bool:
        """Start one worker if the pure selection names a request; ``True`` when one was started."""
        if len(self.workers) >= MAX_IN_FLIGHT:
            return False
        try:
            self.pause.check()
        except ExtensionPaused:
            return False
        now_ns = self.clock.monotonic_ns()
        round_ = self.round
        selection = select_next(
            [s.view() for s in self.series.values()],
            now_ms=now_ns // _NS_PER_MS,
            b_now_ms=self.b_now_ms,
            round_active=round_ is not None and now_ns < round_.deadline_ns,
            r_room=self.limiter.room(RequestClass.R, now_ns),
            ch_room=self.limiter.room(RequestClass.C, now_ns),
            turn=self.turn,
            recheck_ms=self.config.recheck_ms,
            backfill_days=self.config.backfill_days,
        )
        if selection is None:
            return False
        self.turn = selection.turn
        plan = selection.plan
        state = self.series[plan.symbol]
        if state.failures.suspensions > 0:
            self.emit(f"funding-probe symbol={plan.symbol} kind={plan.kind}")
        state.busy = True
        state.touch()
        committed = self.limiter.committed(plan.request_class, now_ns)
        task = asyncio.ensure_future(self._work(plan, state))
        self.workers[task] = _Worker(plan, state, now_ns)
        # The worker needs a few loop iterations (the scoped job, the source) before it reaches the limiter's
        # reservation; the next decision must not be taken before the reservation exists, or two workers could be
        # promised the same room. A worker that ends first (a refused request) never reserves.
        for _ in range(_RESERVATION_HOPS):
            await asyncio.sleep(0)
            if task.done() or self.limiter.committed(plan.request_class, self.clock.monotonic_ns()) > committed:
                break
        return True

    async def _work(self, plan: Plan, state: _Series) -> PageResult | _Failed | _Paused:
        started_ns = self.clock.monotonic_ns()
        try:
            status, result = await self._run_until(
                execute_plan(
                    plan,
                    source=self.source,
                    writer=self.writer,
                    venue=self.config.venue,
                    delay_ms=self.config.publication_delay_ms,
                    b_now_ms=self.b_now_ms,
                    series_verified_at_ms=state.verified_at_ms,
                    span_planned=state.span,
                    clock=self.clock,
                ),
                started_ns + _ns(REQUEST_TIMEOUT_MS),
            )
        except asyncio.CancelledError:
            state.needs_reread = True  # cancelled at a deadline or on stop: the page's outcome is unknown
            raise
        except ExtensionPaused:
            return _Paused()
        except ExtensionStopped:
            raise
        except FundingRateExecutionError as error:
            return self._classify_write_failure(error, state)
        except FundingRateWriteError as error:
            raise FundingRateWriterUnusable(_problem(error)) from error
        except (SourceError, FundingRateParseError, FundingRateSelectionError) as error:
            return _Failed(error)
        if status == "done" and result is not None:
            return result
        state.needs_reread = True  # the request timed out (or stop arrived): it may have reached the writer
        return _Failed(TimeoutError(f"request exceeded {REQUEST_TIMEOUT_MS} ms")) if status == "deadline" else _Paused()

    def _classify_write_failure(self, error: FundingRateExecutionError, state: _Series) -> _Failed:
        cause = error.cause.cause
        self.measurements.record_partial(error.cause.partial, error.rows, late_events=len(error.late_events))
        for conflict in error.cause.partial.conflicts:  # recorded by the committed part; a retry reports them as old
            self._emit_conflict(conflict)
        for key in error.late_events:
            self.emit(f"funding-warning late-event symbol={key.symbol} funding_time_ms={key.funding_time_ms}")
        if isinstance(cause, FundingRateWriteOutcomeUnknown):
            state.needs_reread = True
            return _Failed(cause)
        if isinstance(cause, FundingSpanNotAdjacentError):
            state.needs_reread = True
            self.emit(f"funding-warning merge-refused symbol={state.symbol} {_problem(cause)}")
            return _Failed(cause)
        if isinstance(cause, FundingRateWriteBusy):
            return _Failed(cause)
        raise FundingRateWriterUnusable(_problem(cause))

    # ---- applying a result -------------------------------------------------------------------------------------

    def _apply(self, worker: _Worker, outcome: PageResult | _Failed | _Paused) -> None:
        state, plan = worker.series, worker.plan
        round_ = self.round
        if isinstance(outcome, _Paused):
            return
        if isinstance(outcome, _Failed):
            now_ms = self._now_ms()
            before = state.failures.suspensions
            state.failures = on_failure(state.failures, now_ms)
            if isinstance(outcome.error, FundingRateRequestRefused):
                self.emit(f"funding-error request-refused symbol={plan.symbol} {_problem(outcome.error)}")
            self.emit(
                f"funding-warning request-failed symbol={plan.symbol} kind={plan.kind}"
                f" consecutive={state.failures.consecutive} {_problem(outcome.error)}"
            )
            if state.failures.suspensions > before:
                state.asked_in_round = True  # a suspended series counts as asked in this round
                hours = (state.failures.suspended_until_ms - now_ms) // 3_600_000
                self.emit(
                    f"funding-suspended symbol={plan.symbol} hours={hours} suspensions={state.failures.suspensions}"
                )
            if round_ is not None and plan.kind in ("S", "R"):
                round_.failed += 1
            state.touch()
            return
        state.failures = on_success(state.failures)
        if outcome.span_after is not None:
            state.span = outcome.span_after
        if outcome.verified_at_ms is not None:
            state.verified_at_ms = max(state.verified_at_ms or 0, outcome.verified_at_ms)
        if plan.kind == "H":
            chunk = plan.chunk
            assert chunk is not None
            next_start = outcome.truncated_next_start
            state.history_chunk = (
                None if next_start is None else HistoryChunk(chunk.from_ms, chunk.through_ms, next_start)
            )
        else:
            state.continuing_from_ms = outcome.truncated_next_start
        if plan.kind == "H":  # the history order is round-robin over history service only (P-10 selection rule 2)
            self._sequence += 1
            state.never_served = False
            state.last_served = self._sequence
        if plan.kind in ("S", "R"):
            state.asked_in_round = True
            if round_ is not None:
                round_.asked += 1
        self.measurements.record_page(outcome)
        for conflict in outcome.conflicts:
            self._emit_conflict(conflict)
        for key in outcome.late_events:
            self.emit(f"funding-warning late-event symbol={key.symbol} funding_time_ms={key.funding_time_ms}")
        if outcome.off_hour_rows:
            self.emit(f"funding-warning off-hour symbol={plan.symbol} rows={outcome.off_hour_rows}")
        state.touch()

    def _emit_conflict(self, conflict: FundingRateConflict) -> None:
        """One line per NEWLY recorded conflict (P-8); a re-delivered known conflict is not logged again."""
        if conflict.newly_recorded:
            key = conflict.incoming.key
            self.emit(
                f"funding-conflict symbol={key.symbol} funding_time_ms={key.funding_time_ms}"
                f" rate_type={key.rate_type.value} stored={conflict.stored.funding_rate}"
                f" incoming={conflict.incoming.funding_rate}"
            )

    # ---- waiting ------------------------------------------------------------------------------------------------

    async def _wait(self) -> None:
        now_ns = self.clock.monotonic_ns()
        timers: list[int] = []
        round_ = self.round
        if round_ is not None:
            timers.append(round_.deadline_ns)
        elif self.opening is not None:
            pass  # the observation task wakes the loop when it ends (or at its own deadline)
        elif self._open_not_before_ns is not None and self._open_not_before_ns > now_ns:
            timers.append(self._open_not_before_ns)
        elif self._next_plan is not None:
            timers.append(now_ns + _ns(max(0, self._next_plan.wake_ms - self.clock.wall_ms())))
        for group in (RequestClass.R, RequestClass.C):
            if not self.limiter.room(group, now_ns):
                timers.append(self.limiter.next_room_ns(group, now_ns))
        for state in self.series.values():
            until = unavailable_until_ms(state.failures)
            if until * _NS_PER_MS > now_ns:
                timers.append(until * _NS_PER_MS)
        try:
            self.pause.check()
        except ExtensionPaused as paused:
            timers.append(paused.until_monotonic_ns)
        timers.append(self._measure_started_ns + _ns(MEASUREMENT_WINDOW_MS))
        wake_ns = min(timers)
        remaining_ms = max(1, -(-(wake_ns - now_ns) // _NS_PER_MS))
        timer = asyncio.ensure_future(self.sleep(remaining_ms))
        stopper = asyncio.ensure_future(self.stop.wait())
        try:
            waiting: set[asyncio.Future[Any]] = {timer, stopper, *self.workers}
            if self.opening is not None:
                waiting.add(self.opening.task)
            await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for future in (timer, stopper):
                future.cancel()
            await asyncio.gather(timer, stopper, return_exceptions=True)

    # ---- measurements ------------------------------------------------------------------------------------------

    def _emit_measurements(self, *, force: bool) -> None:
        elapsed_ms = (self.clock.monotonic_ns() - self._measure_started_ns) // _NS_PER_MS
        if not force and elapsed_ms < MEASUREMENT_WINDOW_MS:
            return
        line = self.measurements.line(elapsed_ms, self.limiter.peak_starts())
        if line is not None:
            self.emit(line)
        self.measurements.reset()
        self._measure_started_ns = self.clock.monotonic_ns()


async def run_funding_rate_task(
    source: FundingRateTaskSource,
    writer: FundingRateSink,
    db: Path,
    config: FundingRateTaskConfig,
    limiter: FundingRateLimiter,
    pause: ExtensionPause,
    clock: Clock,
    sleep: Sleep,
    stop: asyncio.Event,
    emit: Emit,
    measurements: FundingRateMeasurements,
) -> None:
    """Run until ``stop`` is set. Raises on a fatal condition without logging it; never restarts itself."""
    await _FundingRateTask(source, writer, db, config, limiter, pause, clock, sleep, stop, emit, measurements).run()


__all__ = [
    "MEASUREMENT_WINDOW_MS",
    "REQUEST_TIMEOUT_MS",
    "FundingRateTaskConfig",
    "FundingRateWriterUnusable",
    "run_funding_rate_task",
]
