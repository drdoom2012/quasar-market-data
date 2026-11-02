"""``quasar-collector``: the Collector's command line.

``fetch-oi-once`` (ADR-0003, OI-3) fetches the newest 5m open interest rows of one symbol on the open interest
source's own client, stores the eligible ones and reports what happened. Exit codes as for ``fetch-once``, plus:
``4`` also when a page failed after at least one of its transactions committed (those rows stay stored; re-run
to complete the page), and ``1`` when server time is untrusted (no request is made, nothing is written).

``fetch-once`` fetches the most recent klines of one symbol, stores the confirmed closed candles and reports
what happened. Exit codes keep apart what is known about persistence:

* ``0`` every eligible candle stored, nothing invalid or conflicting;
* ``3`` eligible candles stored, but invalid candles or conflicts were found (reported on stdout);
* ``1`` failure after which nothing was written, with certainty;
* ``4`` the write's outcome is unknown (the batch is stored completely or not at all; re-run to reconcile);
* ``130`` interrupted: whether the last batch was stored is unknown;
* ``2`` usage error.

A resource that fails to close is only a warning when persistence is settled; the exit status ``4`` and
``130`` are never changed by what ``close`` does.

``run`` collects continuously until SIGINT or SIGTERM (S14). Before any network call it reads the stored window and
history of every symbol (S15 restart reconcile; a failing read exits ``1``), sizes round one's requests to reach the
oldest absent slot of the window, and after round one reads the database again to report what is still absent
(``reconcile-after``; a failing read only withholds that report). Its exit codes:

* ``0`` stopped on request and every write is accounted for;
* ``4`` a write's outcome was unknown or cancelled at some point in the run (the batch may or may not be stored;
  restarting re-fetches idempotently), even if everything after it was clean, and it outranks a fatal error;
* ``1`` a fatal error with nothing in doubt, a failure before the loop started, or a failing writer ``close``
  after an otherwise clean stop;
* ``2`` usage error, which includes a configuration file that cannot be read or is invalid.

``run [CONFIG]`` takes no other argument: everything it needs comes from the configuration file (default
``config/quasar.toml``, S20), whose path, hash and database are the first line it prints. Between rounds ``run``
backfills holes of the stored history (S16; ``collector.backfill`` enables it and bounds how far back it looks). Backfill never writes ``unavailable_candles`` and never says a history is complete: a
slot the exchange does not serve stays missing. Exit codes are unchanged.
"""

import argparse
import asyncio
import signal
import sqlite3
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AsyncExitStack
from importlib.metadata import version
from pathlib import Path

from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.async_open_interest_writer import AsyncOpenInterestWriter, OpenInterestPageError
from quasar_market_data.collector.backfill import BackfillConfig, Backfiller, SliceReport
from quasar_market_data.collector.binance_klines import VENUE_BINANCE_USDM, KlineParseError
from quasar_market_data.collector.binance_open_interest import OpenInterestParseError
from quasar_market_data.collector.binance_usdm import BinanceUsdmSource
from quasar_market_data.collector.binance_usdm_open_interest import (
    DEFAULT_BUDGET_PER_5MIN,
    BinanceUsdmOpenInterestSource,
)
from quasar_market_data.collector.candle_write_activity import CandleWriteActivity
from quasar_market_data.collector.candle_writer import CandleWriteOutcomeUnknown
from quasar_market_data.collector.clock import Clock, Sleep, SystemClock, system_sleep
from quasar_market_data.collector.clock_lead import ClockLeadMonitor
from quasar_market_data.collector.collection_loop import (
    FatalLoopError,
    LoopConfig,
    LoopExit,
    RoundReport,
    SymbolOutcome,
    run_collection_loop,
)
from quasar_market_data.collector.fetch_once import (
    MAX_LIMIT,
    FetchOnceConfig,
    FetchOnceReport,
    fetch_once,
)
from quasar_market_data.collector.fetch_open_interest_once import (
    DEFAULT_PERIODS,
    MAX_PERIODS,
    FetchOpenInterestOnceConfig,
    FetchOpenInterestOnceReport,
    OpenInterestOnceSource,
    fetch_open_interest_once,
)
from quasar_market_data.collector.kline_source import KlineSource, SourceError
from quasar_market_data.collector.open_interest_budget import OpenInterestBudget
from quasar_market_data.collector.open_interest_gate import OpenInterestGate
from quasar_market_data.collector.open_interest_writer import OpenInterestWriteOutcomeUnknown
from quasar_market_data.collector.rate_limiter import RateLimitedSource, RateLimiter
from quasar_market_data.collector.reconcile_reader import (
    ReadGuard,
    ReconcileError,
    ReconcileInputs,
    expected_target,
    reconcile,
)
from quasar_market_data.config.settings import DEFAULT_CONFIG_PATH, ConfigError, Settings, load_settings
from quasar_market_data.domain.closed_candles import DEFAULT_PUBLICATION_DELAY_MS
from quasar_market_data.domain.open_interest import DEFAULT_OPEN_INTEREST_PUBLICATION_DELAY_MS
from quasar_market_data.domain.reconcile import (
    REACH_MARGIN_SLOTS,
    HistoryStatus,
    SeriesReconcile,
    WindowStatus,
)
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import StorageError, open_writer
from quasar_market_data.storage.migrate import applied_version, migrate, refuse_foreign_file

PROG = "quasar-collector"
DEFAULT_LIMIT = 100
EXIT_FINDINGS = 3
EXIT_FAILED = 1
EXIT_UNKNOWN_OUTCOME = 4
EXIT_INTERRUPTED = 130
POST_CHECK_GRACE_MS = 2_000  # how long shutdown waits for a running post-check before interrupting it

type OpenSource = Callable[[], Awaitable[KlineSource]]
type OpenInterestOpenSource = Callable[[Clock, Sleep], Awaitable[OpenInterestOnceSource]]


async def open_binance_open_interest_source(clock: Clock, sleep: Sleep) -> OpenInterestOnceSource:
    """The production factory: its own client, the ADR-0003 default budget (OI-4 reads it from the configuration)."""
    budget = OpenInterestBudget(DEFAULT_BUDGET_PER_5MIN, clock, sleep)
    return await BinanceUsdmOpenInterestSource.open(budget, OpenInterestGate(clock), clock)


def _error(message: str) -> None:
    print(message, file=sys.stderr)


def _limit(text: str) -> int:
    value = int(text)
    if not 1 <= value <= MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"must be in 1..{MAX_LIMIT}")
    return value


def _periods(text: str) -> int:
    value = int(text)
    if not 1 <= value <= MAX_PERIODS:
        raise argparse.ArgumentTypeError(f"must be in 1..{MAX_PERIODS}")
    return value


def _delay(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG)
    parser.add_argument("--version", action="version", version=f"{PROG} {version('quasar-market-data')}")
    commands = parser.add_subparsers(dest="command")
    fetch = commands.add_parser("fetch-once", help="fetch recent 5m klines of one symbol and store the closed ones")
    fetch.add_argument("--db", required=True, type=Path, help="SQLite database file (its directory must exist)")
    fetch.add_argument("--symbol", required=True, help="CCXT unified symbol, e.g. BTC/USDT:USDT")
    fetch.add_argument("--limit", type=_limit, default=DEFAULT_LIMIT, help=f"rows to request (1..{MAX_LIMIT})")
    fetch.add_argument(
        "--publication-delay-ms", type=_delay, default=DEFAULT_PUBLICATION_DELAY_MS, help="publication delay (ADR-0001)"
    )
    oi = commands.add_parser(
        "fetch-oi-once", help="fetch the newest 5m open interest rows of one symbol and store the eligible ones"
    )
    oi.add_argument("--db", required=True, type=Path, help="SQLite database file (its directory must exist)")
    oi.add_argument("--symbol", required=True, help="CCXT unified symbol, e.g. BTC/USDT:USDT")
    oi.add_argument(
        "--periods", type=_periods, default=DEFAULT_PERIODS, help=f"newest period ends to request (1..{MAX_PERIODS})"
    )
    oi.add_argument(
        "--publication-delay-ms",
        type=_delay,
        default=DEFAULT_OPEN_INTEREST_PUBLICATION_DELAY_MS,
        help="publication delay (ADR-0003 P-4)",
    )
    run = commands.add_parser(
        "run", help="collect the newest closed 5m candles of the configured symbols until stopped"
    )
    run.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"configuration file (default {DEFAULT_CONFIG_PATH})",
    )
    return parser


async def _close_quietly(name: str, close: Callable[[], Awaitable[None]]) -> None:
    try:
        await close()
    except Exception as error:  # noqa: BLE001 - a close failure is a warning; CancelledError/KeyboardInterrupt keep unwinding
        _error(f"warning: closing {name} failed: {error}")


def _print_report(config: FetchOnceConfig, report: FetchOnceReport) -> int:
    selection, write = report.selection, report.write
    reason = selection.untrusted_reason
    conflicts = len(write.conflicts) if write else 0
    print(f"symbol={config.symbol} timeframe={config.timeframe.label} fetched={report.fetched}")
    print("time=trusted" if reason is None else f"time=untrusted reason={reason.value}")
    print(
        f"inserted={write.inserted if write else 0} duplicates={write.duplicates if write else 0} "
        f"conflicts={conflicts} pending={len(selection.pending)} invalid={len(selection.invalid)}"
    )
    if reason is not None:
        _error(f"error: server time is untrusted ({reason.value}); no candle was confirmed and nothing was written")
        return EXIT_FAILED
    return EXIT_FINDINGS if conflicts or selection.invalid else 0


async def _run(config: FetchOnceConfig, db: Path, open_source: OpenSource, clock: Clock) -> int:
    async with AsyncExitStack() as stack:
        source = await open_source()
        stack.push_async_callback(_close_quietly, "the exchange client", source.close)
        writer = await AsyncCandleWriter.open(db)
        stack.push_async_callback(_close_quietly, "the candle writer", writer.close)
        report = await fetch_once(source, writer, config, clock)
        return _print_report(config, report)  # printed before the resources close; the report is complete here


def _print_open_interest_report(config: FetchOpenInterestOnceConfig, report: FetchOpenInterestOnceReport) -> int:
    selection, write = report.selection, report.write
    reason = selection.untrusted_reason
    conflicts = len(write.conflicts) if write else 0
    start = "none" if report.start_ms is None else report.start_ms
    end = "none" if report.end_ms is None else report.end_ms
    print(f"symbol={config.symbol} period=5m start_ms={start} end_ms={end} fetched={report.fetched}")
    print("time=trusted" if reason is None else f"time=untrusted reason={reason.value}")
    print(
        f"inserted={write.inserted if write else 0} duplicates={write.duplicates if write else 0} "
        f"conflicts={conflicts} pending={len(selection.pending)}"
    )
    if reason is not None:
        _error(
            f"error: server time is untrusted ({reason.value}); no open interest row was confirmed"
            " and nothing was written"
        )
        return EXIT_FAILED
    return EXIT_FINDINGS if conflicts else 0


async def _run_open_interest(
    config: FetchOpenInterestOnceConfig, db: Path, open_source: OpenInterestOpenSource, clock: Clock, sleep: Sleep
) -> int:
    async with AsyncExitStack() as stack:
        source = await open_source(clock, sleep)
        stack.push_async_callback(_close_quietly, "the exchange client", source.close)
        writer = await AsyncOpenInterestWriter.open(db, activity=CandleWriteActivity())
        stack.push_async_callback(_close_quietly, "the open interest writer", writer.close)
        report = await fetch_open_interest_once(source, writer, config, clock)
        return _print_open_interest_report(config, report)


def run_exit_code(outcome: LoopExit | FatalLoopError) -> int:
    """An uncertain write outranks everything: it is the fact the operator must act on."""
    if outcome.uncertain_symbols:
        return EXIT_UNKNOWN_OUTCOME
    return 0 if isinstance(outcome, LoopExit) else EXIT_FAILED


def _print_round(report: RoundReport) -> None:
    counts = Counter(result.outcome for result in report.results)
    target = "none" if report.target_open_ms is None else str(report.target_open_ms)
    print(
        f"round target={target} collected={counts[SymbolOutcome.COLLECTED]}"
        f" conflicted={counts[SymbolOutcome.CONFLICTED]} gave_up={counts[SymbolOutcome.GAVE_UP]}"
        f" stopped={counts[SymbolOutcome.STOPPED]} write_uncertain={len(report.uncertain_symbols)}"
        f" lateness_ms={report.lateness_ms} idle_overrun_ms={report.idle_overrun_ms}"
        f" skipped_rounds={report.skipped_rounds} clock_step_ms={report.clock_step_ms}"
        f" clock_lead_slots={_ms(report.clock_lead_slots)}",
        flush=True,
    )
    if report.idle_overrun_ms or report.skipped_rounds:
        _error(
            f"warning: the round started late: lateness_ms={report.lateness_ms}"
            f" idle_overrun_ms={report.idle_overrun_ms} skipped_rounds={report.skipped_rounds}"
            f" clock_step_ms={report.clock_step_ms}"
        )
    for result in report.results:
        plain = result.outcome in (SymbolOutcome.COLLECTED, SymbolOutcome.NOTHING_EXPECTED)
        if plain and not result.conflict_open_times:
            continue
        attempts = "unknown" if result.attempts is None else str(result.attempts)
        conflicts = "unknown" if result.conflict_open_times is None else ",".join(map(str, result.conflict_open_times))
        print(
            f"  {result.symbol} {result.outcome.value} attempts={attempts} conflicts={conflicts or '-'}"
            f" problem={result.last_problem or '-'}",
            flush=True,
        )


def _count(value: int | None) -> str:
    return "unverified" if value is None else str(value)


def _print_slice(report: SliceReport) -> None:
    """One line per symbol slice. Three separate statements: what the exchange served, what the writer reported,
    and what the read-back recovered (an integer only when it could be attributed to this slice)."""
    print(
        f"backfill symbol={report.symbol} pages={report.pages}"
        f" | served_all={report.served_all} served_some={report.served_some} served_none={report.served_none}"
        f" | stored={report.stored} duplicates={report.duplicates} conflicts={report.conflicts}"
        f" invalid={report.invalid} pending={report.pending}"
        f" | new_in_targets={_count(report.new_in_targets)} recovered={_count(report.recovered)}"
        f" missing_in_targets={_count(report.missing_in_targets)} attribution={report.attribution.value}"
        f" | status={report.status.value}",
        flush=True,
    )
    if report.reason is not None:  # stderr, so the documented stdout line keeps its format
        _error(f"warning: backfill symbol={report.symbol} status={report.status.value} reason={report.reason}")


def _report_suspension(consecutive: int, idle_overrun_ms: int) -> None:
    print(f"backfill suspended reason=idle_overrun consecutive={consecutive} until=restart", flush=True)
    _error(
        f"warning: backfill suspended after {consecutive} consecutive idle overruns"
        f" (last idle_overrun_ms={idle_overrun_ms}); restart to resume"
    )


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _ms(value: int | None) -> str:
    return "-" if value is None else str(value)


def _state_fields(r: SeriesReconcile) -> str:
    return (
        f"window={r.window.value} all_present={_flag(r.all_present)}"
        f" clock_behind_stored={_flag(r.clock_behind_stored)} history={r.history.value} missing={r.missing}"
        f" due={r.due} stale={r.stale} conflicted={r.conflicted} unavailable={r.unavailable}"
    )


def _total(counts: Iterable[int | None]) -> str:
    """A sum, or ``-`` when any count is unknown (an unread history is never added as 0)."""
    known = list(counts)
    return "-" if any(count is None for count in known) else str(sum(count or 0 for count in known))


def _summary_fields(results: Mapping[str, SeriesReconcile]) -> str:
    values = results.values()
    return (
        f"symbols={len(results)} window_gaps={sum(r.window is WindowStatus.WINDOW_GAPS for r in values)}"
        f" not_all_present={sum(not r.all_present for r in values)} due_slots={sum(r.due for r in values)}"
        f" history_holes={_total(r.history_unaccounted_holes for r in values)}"
        f" needs_s16_slots={_total(r.needs_s16_slots for r in values)}"
    )


def _print_reconcile(results: Mapping[str, SeriesReconcile], limit: int) -> None:
    """The pre-start read: a line per symbol unless its classified window has every slot present and its history
    no hole, then the summary. Never says "complete": see S15 Decisions 3 and 6."""
    for symbol, r in results.items():
        if r.window is WindowStatus.WINDOW_NO_GAPS and r.all_present and r.history is not HistoryStatus.HOLES:
            continue
        print(
            f"reconcile symbol={symbol} {_state_fields(r)} wanted_from={_ms(r.oldest_wanted_ms)}"
            f" unreachable_tail={r.unreachable_tail_slots} history_holes={_ms(r.history_unaccounted_holes)}"
            f" holes_older_than_window={_ms(r.history_holes_older_than_window)} needs_s16={_ms(r.needs_s16_slots)}"
            f" unknown_before={_ms(r.unknown_before_ms)} catch_up_limit={r.catch_up_limit_at_read}",
            flush=True,
        )
        for start, end in r.largest_hole_runs:
            print(f"  hole {start}..{end} (raw)", flush=True)
    catch_up = sum(r.catch_up_limit_at_read > limit for r in results.values())
    print(f"reconcile summary {_summary_fields(results)} catch_up={catch_up}", flush=True)


def _print_reconcile_after(
    after: Mapping[str, SeriesReconcile], before: Mapping[str, SeriesReconcile], lead_max: int | None
) -> None:
    """The post-check: what the database holds after round one, next to what was absent at the pre-check.

    ``absent_before`` is only a comparison value; no field claims a slot was recovered (S15 Decision 5)."""
    for symbol, r in after.items():
        prior = before.get(symbol)
        absent_before = "-" if prior is None else str(prior.missing + prior.due + prior.stale)
        print(
            f"reconcile-after symbol={symbol} {_state_fields(r)} absent_before={absent_before}"
            f" history_holes={_ms(r.history_unaccounted_holes)}"
            f" holes_older_than_window={_ms(r.history_holes_older_than_window)}"
            f" needs_s16={_ms(r.needs_s16_slots)} unknown_before={_ms(r.unknown_before_ms)}",
            flush=True,
        )
        for run in r.absent_runs:
            print(f"  still_missing {symbol} {run.start_ms}..{run.end_ms} {run.state.value}", flush=True)
        for start, end in r.largest_hole_runs:
            print(f"  hole {start}..{end} (raw)", flush=True)
    print(f"reconcile-after summary {_summary_fields(after)} clock_lead_slots_max={_ms(lead_max)}", flush=True)


def _warn_clock_lead(lead: int, first_round: bool) -> None:
    """Called by ``ClockLeadMonitor`` on a new run-wide maximum above the A1 margin."""
    head = (
        f"warning: the server's forming slot was {lead} slots ahead of the local clock"
        f" (assumption A1 allows {REACH_MARGIN_SLOTS});"
    )
    if first_round:
        _error(
            f"{head} first-round requests may have missed up to {lead - REACH_MARGIN_SLOTS} of the oldest wanted slots"
        )
    else:
        _error(f"{head} every round line reports clock_lead_slots")


async def _run_loop(
    config: LoopConfig,
    db: Path,
    min_interval_ms: int,
    open_source: OpenSource,
    clock: Clock,
    sleep: Sleep,
    backfill: BackfillConfig | None,
) -> int:
    """Handlers first, so that a stop during the pre-start read is honoured; restored on every way out."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in previous:
        loop.add_signal_handler(sig, stop.set)
    try:
        return await _reconcile_and_collect(config, db, min_interval_ms, open_source, clock, sleep, stop, backfill)
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


async def _finish_post_check(task: "asyncio.Task[None] | None", guard: ReadGuard) -> None:
    """Give a running post-check ``POST_CHECK_GRACE_MS``, then interrupt it and wait for it to end.

    The grace is real monotonic time (``asyncio.wait``), not the injected ``sleep``: the read is a real thread whose
    duration a fake clock does not model. A cancellation still interrupts the read before it propagates, so no thread
    keeps reading behind the process; an error in the task is only a warning and never changes the exit code.
    """
    if task is None:
        return
    try:
        await asyncio.wait({task}, timeout=POST_CHECK_GRACE_MS / 1000)
    finally:
        if not task.done():
            guard.interrupt()
    try:
        await task
    except Exception as error:  # noqa: BLE001 - only the report failed; the exit code stays the loop's
        _error(f"warning: the post-check ended with an error: {error!r}")


async def _reconcile_and_collect(
    config: LoopConfig,
    db: Path,
    min_interval_ms: int,
    open_source: OpenSource,
    clock: Clock,
    sleep: Sleep,
    stop: asyncio.Event,
    backfill: BackfillConfig | None,
) -> int:
    inputs = ReconcileInputs(
        symbols=config.symbols,
        venue=VENUE_BINANCE_USDM,
        timeframe=config.timeframe,
        publication_delay_ms=config.publication_delay_ms,
        lag_threshold_ms=config.lag_threshold_ms,
        default_limit=config.limit,
        max_limit=MAX_LIMIT,
    )
    as_of = clock.wall_ms()
    try:
        target = expected_target(inputs, as_of)
    except Exception as error:
        raise ReconcileError(f"restart reconcile failed: {error}") from error
    # Before any network call and before the writer opens (S15 Decision 9); a failure is a ReconcileError (exit 1).
    before = await reconcile(db, inputs, as_of_ms=as_of, target_open_ms=target, history=False)  # window only
    _print_reconcile(before, config.limit)
    if stop.is_set():  # stopped during the read: nothing was fetched or written
        return 0

    monitor = ClockLeadMonitor(config.timeframe.milliseconds, _warn_clock_lead)
    guard = ReadGuard()
    post_check: asyncio.Task[None] | None = None

    async def report_post_check(report: RoundReport, lead_max: int | None, snapshot: asyncio.Event) -> None:
        try:
            after = await reconcile(
                db,
                inputs,
                as_of_ms=report.started_wall_ms,
                target_open_ms=report.target_open_ms,
                snapshot=snapshot,
                guard=guard,
            )
        except Exception as error:  # noqa: BLE001 - the claim is withheld; collection itself is healthy
            _error(f"reconcile-after status=unverified error={error}")
        else:
            _print_reconcile_after(after, before, lead_max)

    async def after_first_round(report: RoundReport) -> None:
        """Start the post-check and return once its read holds a snapshot (or has ended): it then sees exactly what
        round one left, never a later write, while the series-wide read itself runs beside round two and the backfill
        (2026-10-28 amendment of S15 D4)."""
        nonlocal post_check
        lead_max = monitor.max_lead  # round one's figure; later rounds keep updating the monitor
        monitor.first_round_done()
        snapshot = asyncio.Event()
        post_check = asyncio.create_task(report_post_check(report, lead_max, snapshot), name="post-check")
        snapshot_taken = asyncio.ensure_future(snapshot.wait())
        try:
            await asyncio.wait({post_check, snapshot_taken}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            snapshot_taken.cancel()
            await asyncio.gather(snapshot_taken, return_exceptions=True)

    writer_close_error: Exception | None = None

    async def close_writer(writer: AsyncCandleWriter) -> None:
        nonlocal writer_close_error
        try:
            await writer.close()
        except Exception as error:  # noqa: BLE001 - reported below; it can only turn a clean exit into 1
            writer_close_error = error

    async with AsyncExitStack() as stack:
        source = await open_source()
        stack.push_async_callback(_close_quietly, "the exchange client", source.close)
        writer = await AsyncCandleWriter.open(db)
        stack.push_async_callback(close_writer, writer)
        limited = RateLimitedSource(source, RateLimiter(min_interval_ms, clock, sleep))
        backfiller = (
            None
            if backfill is None
            else Backfiller(backfill, config, db, clock, sleep, _print_slice, _report_suspension)
        )
        outcome: LoopExit | FatalLoopError
        try:
            outcome = await run_collection_loop(
                limited,
                writer,
                config,
                clock,
                sleep,
                stop,
                _print_round,
                first_round_wanted_from={
                    s: r.oldest_wanted_ms for s, r in before.items() if r.oldest_wanted_ms is not None
                },
                after_first_round=after_first_round,
                observe_forming_slot=monitor.observe,
                idle_work=None if backfiller is None else backfiller.run_idle,
            )
        except FatalLoopError as error:
            outcome = error
        finally:
            await _finish_post_check(post_check, guard)
        code = run_exit_code(outcome)
        if isinstance(outcome, FatalLoopError):
            _error(f"error: {outcome}")
        if outcome.uncertain_symbols:
            _error(
                "write outcome uncertain for: "
                + ", ".join(outcome.uncertain_symbols)
                + "; the batch may or may not be stored; restart to re-fetch (writes are idempotent)"
            )
    if writer_close_error is not None:
        _error(f"error: closing the candle writer failed: {writer_close_error}")
        return code or EXIT_FAILED  # never lowers 1 or 4
    return code


def _run_command(settings: Settings, open_source: OpenSource, clock: Clock, sleep: Sleep) -> int:
    venue, collector = settings.venues[0], settings.collector
    config = LoopConfig(
        symbols=venue.symbols,
        timeframe=Timeframe(venue.timeframes[0]),
        limit=collector.limit,
        publication_delay_ms=settings.rule.publication_delay_ms,
        lag_threshold_ms=settings.rule.lag_threshold_ms,
        max_concurrency=collector.max_concurrency,
    )
    try:
        old_version, new_version = _migrate(settings.database_path, clock)
        if new_version > old_version:
            _error(f"migrated to version {new_version} (from {old_version})")
        backfill = BackfillConfig(collector.backfill.days) if collector.backfill.enabled else None
        return asyncio.run(
            _run_loop(
                config, settings.database_path, collector.min_request_interval_ms, open_source, clock, sleep, backfill
            )
        )
    except (SourceError, StorageError, sqlite3.Error) as error:  # raised before the loop started
        _error(f"error: {error}")
        return EXIT_FAILED


def _migrate(db: Path, clock: Clock) -> tuple[int, int]:
    """Migrate ``db`` and return ``(versions applied before, version after)``."""
    refuse_foreign_file(db)  # read-only, before open_writer switches the file to WAL
    conn = open_writer(db)
    try:
        before = applied_version(conn)
        return before, migrate(conn, applied_at_ms=clock.wall_ms())
    finally:
        conn.close()


def _fetch_once(args: argparse.Namespace, open_source: OpenSource, clock: Clock) -> int:
    db: Path = args.db
    if not db.parent.is_dir():
        _error(f"error: database directory {db.parent} does not exist; nothing was written")
        return EXIT_FAILED
    config = FetchOnceConfig(
        symbol=args.symbol,
        timeframe=Timeframe("5m"),
        limit=args.limit,
        publication_delay_ms=args.publication_delay_ms,
    )
    try:
        _migrate(db, clock)
        return asyncio.run(_run(config, db, open_source, clock))
    except CandleWriteOutcomeUnknown:
        _error(
            "write outcome unknown: the batch is stored completely or not at all; "
            "re-run the same command to reconcile (writes are idempotent)"
        )
        return EXIT_UNKNOWN_OUTCOME
    except (SourceError, KlineParseError, StorageError, sqlite3.Error) as error:  # all raised before any write
        _error(f"error: {error}")
        return EXIT_FAILED


_UNKNOWN_OUTCOME_MESSAGE = (
    "write outcome unknown: the batch is stored completely or not at all; "
    "re-run the same command to reconcile (writes are idempotent)"
)


def _open_interest_page_failure(error: OpenInterestPageError) -> int:
    if error.committed_transactions > 0:
        _error(
            f"write incomplete: {error.committed_transactions} transaction(s) committed and stay stored,"
            f" the rest of the page is not stored or its outcome is unknown ({error.cause});"
            " re-run the same command to complete it (writes are idempotent)"
        )
        return EXIT_UNKNOWN_OUTCOME
    if isinstance(error.cause, OpenInterestWriteOutcomeUnknown):
        _error(_UNKNOWN_OUTCOME_MESSAGE)
        return EXIT_UNKNOWN_OUTCOME
    _error(f"error: {error.cause}")  # the first transaction failed with certainty: nothing was written
    return EXIT_FAILED


def _fetch_open_interest_once(
    args: argparse.Namespace, open_source: OpenInterestOpenSource, clock: Clock, sleep: Sleep
) -> int:
    db: Path = args.db
    if not db.parent.is_dir():
        _error(f"error: database directory {db.parent} does not exist; nothing was written")
        return EXIT_FAILED
    config = FetchOpenInterestOnceConfig(
        symbol=args.symbol, periods=args.periods, publication_delay_ms=args.publication_delay_ms
    )
    try:
        _migrate(db, clock)
        return asyncio.run(_run_open_interest(config, db, open_source, clock, sleep))
    except OpenInterestPageError as error:
        return _open_interest_page_failure(error)
    except (SourceError, OpenInterestParseError, StorageError, sqlite3.Error) as error:  # raised before any write
        _error(f"error: {error}")
        return EXIT_FAILED


def main(
    argv: list[str] | None = None,
    *,
    open_source: OpenSource = BinanceUsdmSource.open,
    open_oi_source: OpenInterestOpenSource = open_binance_open_interest_source,
    clock: Clock | None = None,
    sleep: Sleep = system_sleep,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_usage()
        return 0
    if args.command == "run":
        try:
            loaded = load_settings(args.config)
        except ConfigError as error:
            parser.error(f"config {args.config}: {error}")
        print(loaded.start_line(), flush=True)
        return _run_command(loaded.settings, open_source, clock or SystemClock(), sleep)
    try:
        if args.command == "fetch-oi-once":
            return _fetch_open_interest_once(args, open_oi_source, clock or SystemClock(), sleep)
        return _fetch_once(args, open_source, clock or SystemClock())
    except KeyboardInterrupt:
        _error(
            "interrupted: whether the last batch was stored is unknown; "
            "re-run the same command to reconcile (writes are idempotent)"
        )
        return EXIT_INTERRUPTED
