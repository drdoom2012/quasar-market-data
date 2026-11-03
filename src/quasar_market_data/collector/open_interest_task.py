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

Catch-up (ADR-0003 P-12). Between rounds, in the idle window that ends 15 s before the next wake, the task derives the
holes of every series from the stored rows (read-only, one series at a time) and fetches them in pages of at most 500
period ends, oldest first, one request at a time. The whole window (its time call, reads and pages) is cancelled at the
window deadline, so a hung page can never delay the next round. A page starts only if the start is grantable at once and
the next round keeps its whole budget (``backfill_may_start``): rounds pre-empt the catch-up. A series with holes is
re-read in full at every window and after each of its pages; a hole-free series is extended by reading only the stretch
that became expected since the previous window. The first scan of a series logs its ``oi-reconcile`` summary. A slot a
successful page answers without its row is *tried*; after three tries it is not planned again in this run (try book,
in memory). A failed page is not a try: the symbol backs off 1, 2, 4 windows, then is suspended until restart.

Output is one injected callable ``emit(line)``; the families are ``oi-round``, ``oi-conflict``, ``oi-warning``,
``oi-error``, ``oi-measurement``, ``oi-reconcile`` and ``oi-catchup``.
"""

import asyncio
from bisect import bisect_left
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
from quasar_market_data.collector.open_interest_reader import SeriesFacts, read_series
from quasar_market_data.collector.open_interest_writer import (
    OpenInterestWriteBusy,
    OpenInterestWriteError,
    OpenInterestWriteOutcomeUnknown,
)
from quasar_market_data.domain.open_interest import OpenInterest, select_eligible_open_interest
from quasar_market_data.domain.open_interest_cadence import (
    backfill_share,
    open_interest_cadence,
    round_cost,
    symbols_for_round,
)
from quasar_market_data.domain.open_interest_holes import (
    DAY_MS,
    Page,
    holes_in,
    newest_expected_ms,
    oldest_allowed_ms,
    plan_pages,
    slots_at_risk,
)
from quasar_market_data.domain.open_interest_request import MAX_PERIOD_ENDS, RETRIEVAL_HORIZON_MS
from quasar_market_data.domain.open_interest_schedule import (
    ROUND_DEADLINE_RESERVE_MS,
    backfill_may_start,
    next_round,
    round_deadline_ms,
    round_range,
)
from quasar_market_data.domain.open_interest_tries import OpenInterestTryBook
from quasar_market_data.domain.server_time import (
    DEFAULT_TIME_TRUST,
    ServerTimeObservation,
    TimeTrustConfig,
    TrustedTime,
    server_lower_bound,
)
from quasar_market_data.domain.timeframe import Timeframe

PAGE_BUDGET_MS: Final = 15_000  # a page must be able to start this long before the window deadline
MAX_PAGE_FAILURES: Final = 4  # the 4th consecutive failed page suspends the symbol until restart
RETRY_DELAY_MS: Final = 1_000  # the first step of the candle RetryPolicy, copied
MEASUREMENT_WINDOW_MS: Final = 3_600_000
PROBLEM_MAX_CHARS: Final = 200
_NS_PER_MS: Final = 1_000_000
_FAR_FUTURE_MS: Final = 2**62
URGENT_SLOTS_LOGGED: Final = 5

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


@dataclass
class _Window:
    pages: int = 0
    stored: int = 0
    tries: int = 0
    failures: int = 0
    queued: int = 0  # symbols with a page waiting at the start of the window
    end: str = "no-pages"


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
        self.cycle = 0
        self.holes: dict[str, list[int]] = {}
        self._scanned_newest: dict[str, int] = {}
        self.tries = OpenInterestTryBook(config.period)
        self._failures: dict[str, int] = {}
        self._backoff_until: dict[str, int] = {}
        self._suspended: set[str] = set()
        self._reconcile_totals = [0, 0, 0]  # stored, holes, no longer recoverable, over the first scans
        self._exhausted_logged: dict[str, int] = {}
        self._reconciled_all = False

    # ---- schedule -------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        config = self.config
        last: int | None = None
        while not self.stop.is_set():
            self.cycle += 1
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
        """The idle time before the next round: catch-up first, then wait for the wake; ``True`` when stopped."""
        await self._idle_window(wake_ns, wake_ms)
        remaining_ns = wake_ns - self.clock.monotonic_ns()
        if self.stop.is_set():
            return True
        return await sleep_or_stop(self.sleep, max(0, -(-remaining_ns // _NS_PER_MS)), self.stop)

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

    # ---- the idle window: reconcile and catch-up ---------------------------------------------------------------------

    async def _idle_window(self, wake_ns: int, wake_ms: int) -> None:
        deadline_ns = wake_ns - ROUND_DEADLINE_RESERVE_MS * _NS_PER_MS
        if deadline_ns <= self.clock.monotonic_ns():
            return  # the window would already be over
        window = _Window()
        status, _ = await self._run_until(self._window_work(window, wake_ns, deadline_ns), deadline_ns)
        if status != "done":
            window.end = status
        if window.end != "no-pages" or window.pages or window.failures:
            self.emit(
                f"oi-catchup cycle={self.cycle} queued={window.queued} pages={window.pages} stored={window.stored}"
                f" tries={window.tries} failures={window.failures} end={window.end}"
            )

    def _grantable_now(self) -> bool:
        return self.budget.committed_at(self.clock.monotonic_ns()) + 1 <= self.config.budget_per_5min

    async def _window_work(self, window: _Window, wake_ns: int, deadline_ns: int) -> None:
        config = self.config
        if not self._grantable_now():
            window.end = "budget"
            return
        try:
            observation = await self._observe()
        except OpenInterestPaused:
            window.end = "paused"
            return
        trust = server_lower_bound(
            observation, self.clock.monotonic_ns(), self.clock.wall_ms(), None, config.time_trust
        )
        if not isinstance(trust, TrustedTime):
            window.end = "untrusted-time"
            self.emit(
                f"oi-warning window-untrusted-time cycle={self.cycle} reason={trust.reason.value} no request made"
            )
            return
        as_of = trust.lower_bound_ms
        await self._derive(as_of)
        heads = {symbol: page for symbol in config.symbols if (page := self._next_page(symbol)) is not None}
        window.queued = len(heads)
        self._warn_slots_at_risk(as_of)
        self._log_exhausted()
        share = backfill_share(len(config.symbols), config.budget_per_5min)  # P-10: the catch-up's requests per cycle
        while heads and not self.stop.is_set():
            if window.pages + window.failures >= share:
                window.end = "share"
                return
            now_ns = self.clock.monotonic_ns()
            if now_ns + PAGE_BUDGET_MS * _NS_PER_MS > deadline_ns:
                window.end = "deadline"
                return
            if not backfill_may_start(
                self.budget.committed_at(now_ns),
                self.budget.committed_at(wake_ns),
                config.budget_per_5min,
                config.round_cost,
            ):
                window.end = "budget"
                return
            page = min(heads.values(), key=lambda head: (head.start_ms, head.symbol))  # oldest first, then by symbol
            status = await self._run_page(page, trust, window)
            if status == "paused":
                window.end = "paused"
                return
            if status == "ok":
                await self._derive_symbol(page.symbol, as_of)  # a hole a round filled meanwhile is not asked again
            following = self._next_page(page.symbol)
            if following is None:
                del heads[page.symbol]
            else:
                heads[page.symbol] = following
        window.end = "stopped" if self.stop.is_set() else "no-pages"

    # Hole derivation ------------------------------------------------------------------------------------------------

    async def _derive(self, as_of: int) -> None:
        for symbol in self.config.symbols:
            if self.stop.is_set():
                return
            await self._derive_symbol(symbol, as_of)

    async def _derive_symbol(self, symbol: str, as_of: int) -> None:
        config = self.config
        period = config.period
        step = period.milliseconds
        oldest = oldest_allowed_ms(as_of, period)
        newest = newest_expected_ms(as_of, config.cadence, config.round_offset_ms, period)
        first_scan = symbol not in self.holes
        known = self.holes.get(symbol)
        if not first_scan and not known:
            previous = self._scanned_newest[symbol]
            if newest <= previous:
                return
            facts = await self._read(symbol, previous + step, newest)
            self.holes[symbol] = list(holes_in(facts.stored, facts.conflicted, previous + step, newest, period))
        else:
            facts = await self._read(symbol, oldest, newest, with_history=first_scan)
            holes = holes_in(facts.stored, facts.conflicted, oldest, newest, period)
            self.holes[symbol] = list(holes)
            self.tries.forget_present(symbol, holes)
            if first_scan:
                self._log_reconcile(
                    symbol, as_of, oldest, facts.stored, holes, facts.first_stored_ms, facts.stored_older
                )
        self._scanned_newest[symbol] = newest

    async def _read(self, symbol: str, oldest: int, newest: int, *, with_history: bool = False) -> SeriesFacts:
        return await read_series(
            self.db,
            venue=self.config.venue,
            symbol=symbol,
            period=self.config.period,
            oldest_ms=oldest,
            newest_ms=newest,
            with_history=with_history,
        )

    def _log_reconcile(
        self,
        symbol: str,
        as_of: int,
        oldest: int,
        stored: Sequence[int],
        holes: Sequence[int],
        first_stored: int | None,
        stored_older: int | None,
    ) -> None:
        step = self.config.period.milliseconds
        lost = 0
        if first_stored is not None and first_stored < oldest:
            lost = max(0, (oldest - first_stored) // step - (stored_older or 0))
        leaves = f"{holes[0] + RETRIEVAL_HORIZON_MS - as_of}" if holes else "na"
        self.emit(
            f"oi-reconcile symbol={symbol} stored={len(stored)} holes={len(holes)}"
            f" no_longer_recoverable={lost} oldest_hole_leaves_in_ms={leaves}"
        )
        totals = self._reconcile_totals
        totals[0] += len(stored)
        totals[1] += len(holes)
        totals[2] += lost
        if not self._reconciled_all and len(self.holes) >= len(self.config.symbols):
            self._reconciled_all = True
            self.emit(
                f"oi-reconcile total series={len(self.config.symbols)} stored={totals[0]} holes={totals[1]}"
                f" no_longer_recoverable={totals[2]} as_of={as_of}"
            )

    # Planning -------------------------------------------------------------------------------------------------------

    def _next_page(self, symbol: str) -> Page | None:
        """The symbol's next page, or ``None``: built from the first available holes only (O(500), not O(holes)).

        The global order is ``(start, symbol)`` over each symbol's own ascending pages, so taking the minimum head and
        replacing it by the same symbol's next page visits the pages in exactly the order ``plan_pages`` would.
        """
        if symbol in self._suspended or self.cycle < self._backoff_until.get(symbol, 0):
            return None
        holes = self.holes.get(symbol, ())
        mono_ms = self.clock.monotonic_ns() // _NS_PER_MS
        for chunk in range(0, len(holes), 1024):
            if self.tries.available(symbol, holes[chunk : chunk + 1024], mono_ms):
                break
        else:
            return None
        # the first available hole of the symbol, then the page that starts at it
        start_index = chunk
        while self.tries.available(symbol, holes[start_index : start_index + 1], mono_ms) == ():
            start_index += 1
        window = self.tries.available(symbol, holes[start_index : start_index + MAX_PERIOD_ENDS], mono_ms)
        pages = plan_pages({symbol: window}, self.config.period)
        return pages[0] if pages else None

    def _warn_slots_at_risk(self, as_of: int) -> None:
        config = self.config
        step = config.period.milliseconds
        threshold = as_of - RETRIEVAL_HORIZON_MS + DAY_MS  # only slots that leave within a day are judged
        reachable: dict[str, tuple[int, ...]] = {}
        for symbol in config.symbols:
            holes = self.holes.get(symbol, ())
            if symbol in self._suspended or not holes:
                continue
            near = holes[
                : bisect_left(holes, threshold - threshold % step + step)
            ]  # a page's position needs only these
            if slots := self.tries.available(symbol, near, _FAR_FUTURE_MS):
                reachable[symbol] = slots
        share = backfill_share(len(config.symbols), config.budget_per_5min)
        lost = slots_at_risk(plan_pages(reachable, config.period), as_of, share, config.period)
        for symbol, slots in lost.items():
            first = ",".join(str(slot) for slot in slots[:URGENT_SLOTS_LOGGED])
            self.emit(f"oi-warning holes-will-leave-the-horizon symbol={symbol} slots={len(slots)} first={first}")

    def _log_exhausted(self) -> None:
        for symbol, count in self.tries.exhausted_counts().items():
            if self._exhausted_logged.get(symbol) != count:
                self._exhausted_logged[symbol] = count
                self.emit(f"oi-catchup-exhausted symbol={symbol} slots={count} tried-3-times-in-this-run")

    # One page -------------------------------------------------------------------------------------------------------

    async def _run_page(self, page: Page, trust: TrustedTime, window: _Window) -> str:
        config = self.config
        symbol = page.symbol
        try:
            response = await self.source.fetch_open_interest_hist(
                symbol, page.start_ms, page.end_ms, as_of_ms=trust.lower_bound_ms
            )
            rows = parse_open_interest_hist(
                response,
                market_id=self.source.market_id(symbol),
                symbol=symbol,
                period=config.period,
                start_time_ms=page.start_ms,
                end_time_ms=page.end_ms,
            )
            selection = select_eligible_open_interest(rows, config.publication_delay_ms, trust)
            if selection.eligible:
                report = await self._write(selection.eligible)
                window.stored += report.inserted
                self._report_conflicts(report)
        except OpenInterestPaused:
            return "paused"
        except OpenInterestBanned:
            raise
        except (SourceError, OpenInterestParseError) as error:
            self._page_failed(page, window, _problem(error))
            return "failed"
        except OpenInterestWriteError as error:
            self._page_failed(page, window, f"write {self._write_problem(error)}: {_problem(error)}")
            return "failed"
        window.pages += 1
        self._failures.pop(symbol, None)
        if not selection.pending:  # a row Binance served but we could not yet judge must never use up a try
            served = {row.key.time_ms for row in rows}
            absent = [slot for slot in page.slots if slot not in served]
            self.tries.record_tries(symbol, absent, self.clock.monotonic_ns() // _NS_PER_MS)
            window.tries += len(absent)
        return "ok"

    def _page_failed(self, page: Page, window: _Window, problem: str) -> None:
        symbol = page.symbol
        window.failures += 1
        failures = self._failures.get(symbol, 0) + 1
        self._failures[symbol] = failures
        if failures >= MAX_PAGE_FAILURES:
            self._suspended.add(symbol)
            self.emit(f"oi-warning catchup-suspended symbol={symbol} failures={failures} until restart; {problem}")
            return
        wait = 2 ** (failures - 1)
        self._backoff_until[symbol] = self.cycle + wait
        self.emit(
            f"oi-warning catchup-page-failed symbol={symbol} start={page.start_ms} failures={failures}"
            f" retry-in-windows={wait} {problem}"
        )

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
