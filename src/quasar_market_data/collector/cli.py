"""``quasar-collector``: the Collector's command line.

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
* ``2`` usage error.

Between rounds ``run`` backfills holes of the stored history (S16; ``--no-backfill`` disables it, ``--backfill-days``
bounds how far back it looks). Backfill never writes ``unavailable_candles`` and never says a history is complete: a
slot the exchange does not serve stays missing. Exit codes are unchanged.
"""

import argparse
import asyncio
import signal
import sqlite3
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from importlib.metadata import version
from pathlib import Path

from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.backfill import BackfillConfig, Backfiller, SliceReport
from quasar_market_data.collector.binance_klines import VENUE_BINANCE_USDM, KlineParseError
from quasar_market_data.collector.binance_usdm import BinanceUsdmSource
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
from quasar_market_data.collector.kline_source import KlineSource, SourceError
from quasar_market_data.collector.rate_limiter import RateLimitedSource, RateLimiter
from quasar_market_data.collector.reconcile_reader import ReconcileError, ReconcileInputs, expected_target, reconcile
from quasar_market_data.domain.closed_candles import DEFAULT_PUBLICATION_DELAY_MS
from quasar_market_data.domain.reconcile import (
    REACH_MARGIN_SLOTS,
    HistoryStatus,
    SeriesReconcile,
    WindowStatus,
    steady_state_limit_needed,
)
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import StorageError, open_writer
from quasar_market_data.storage.migrate import migrate, refuse_foreign_file

PROG = "quasar-collector"
DEFAULT_LIMIT = 100
EXIT_FINDINGS = 3
EXIT_FAILED = 1
EXIT_UNKNOWN_OUTCOME = 4
EXIT_INTERRUPTED = 130
DEFAULT_CONCURRENCY = 4
DEFAULT_REQUEST_INTERVAL_MS = 100
DEFAULT_BACKFILL_DAYS = 30

type OpenSource = Callable[[], Awaitable[KlineSource]]


def _error(message: str) -> None:
    print(message, file=sys.stderr)


def _limit(text: str) -> int:
    value = int(text)
    if not 1 <= value <= MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"must be in 1..{MAX_LIMIT}")
    return value


def _delay(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _backfill_days(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 365:
        raise argparse.ArgumentTypeError("must be in 1..365")
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
    run = commands.add_parser("run", help="collect the newest closed 5m candles of several symbols until stopped")
    run.add_argument("--db", required=True, type=Path, help="SQLite database file (its directory must exist)")
    run.add_argument("--symbol", required=True, action="append", help="CCXT unified symbol; repeat for more symbols")
    run.add_argument("--limit", type=_limit, default=DEFAULT_LIMIT, help=f"rows to request (1..{MAX_LIMIT})")
    run.add_argument(
        "--publication-delay-ms", type=_delay, default=DEFAULT_PUBLICATION_DELAY_MS, help="publication delay (ADR-0001)"
    )
    run.add_argument("--max-concurrency", type=_positive, default=DEFAULT_CONCURRENCY, help="symbols collected at once")
    run.add_argument(
        "--min-request-interval-ms",
        type=_delay,
        default=DEFAULT_REQUEST_INTERVAL_MS,
        help="minimum spacing of adapter calls (not of network requests)",
    )
    run.add_argument(
        "--backfill-days",
        type=_backfill_days,
        default=DEFAULT_BACKFILL_DAYS,
        help="how far back idle-time backfill looks for holes (1..365 days)",
    )
    run.add_argument("--no-backfill", action="store_true", help="do not backfill holes in the idle time between rounds")
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


def _summary_fields(results: Mapping[str, SeriesReconcile]) -> str:
    values = results.values()
    return (
        f"symbols={len(results)} window_gaps={sum(r.window is WindowStatus.WINDOW_GAPS for r in values)}"
        f" not_all_present={sum(not r.all_present for r in values)} due_slots={sum(r.due for r in values)}"
        f" history_holes={sum(r.history_unaccounted_holes for r in values)}"
        f" needs_s16_slots={sum(r.needs_s16_slots for r in values)}"
    )


def _print_reconcile(results: Mapping[str, SeriesReconcile], limit: int) -> None:
    """The pre-start read: a line per symbol unless its classified window has every slot present and its history
    no hole, then the summary. Never says "complete": see S15 Decisions 3 and 6."""
    for symbol, r in results.items():
        if r.window is WindowStatus.WINDOW_NO_GAPS and r.all_present and r.history is not HistoryStatus.HOLES:
            continue
        print(
            f"reconcile symbol={symbol} {_state_fields(r)} wanted_from={_ms(r.oldest_wanted_ms)}"
            f" unreachable_tail={r.unreachable_tail_slots} history_holes={r.history_unaccounted_holes}"
            f" holes_older_than_window={r.history_holes_older_than_window} needs_s16={r.needs_s16_slots}"
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
            f" history_holes={r.history_unaccounted_holes} holes_older_than_window={r.history_holes_older_than_window}"
            f" needs_s16={r.needs_s16_slots} unknown_before={_ms(r.unknown_before_ms)}",
            flush=True,
        )
        for run in r.absent_runs:
            print(f"  still_missing {symbol} {run.start_ms}..{run.end_ms} {run.state.value}", flush=True)
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
        default_limit=config.limit,
        max_limit=MAX_LIMIT,
    )
    as_of = clock.wall_ms()
    try:
        target = expected_target(inputs, as_of)
    except Exception as error:
        raise ReconcileError(f"restart reconcile failed: {error}") from error
    # Before any network call and before the writer opens (S15 Decision 9); a failure is a ReconcileError (exit 1).
    before = await reconcile(db, inputs, as_of_ms=as_of, target_open_ms=target)
    _print_reconcile(before, config.limit)
    if stop.is_set():  # stopped during the read: nothing was fetched or written
        return 0

    monitor = ClockLeadMonitor(config.timeframe.milliseconds, _warn_clock_lead)

    async def after_first_round(report: RoundReport) -> None:
        # The loop does not bound this hook by ``stop``: its only I/O is one read on a read-only connection (bounded
        # by the busy timeout and the series' size). A stop during it is seen by the loop as soon as it returns.
        try:
            after = await reconcile(db, inputs, as_of_ms=report.started_wall_ms, target_open_ms=report.target_open_ms)
        except Exception as error:  # noqa: BLE001 - the claim is withheld; collection itself is healthy
            _error(f"reconcile-after status=unverified error={error}")
        else:
            _print_reconcile_after(after, before, monitor.max_lead)
        monitor.first_round_done()

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


def _run_command(args: argparse.Namespace, open_source: OpenSource, clock: Clock, sleep: Sleep) -> int:
    db: Path = args.db
    if not db.parent.is_dir():
        _error(f"error: database directory {db.parent} does not exist; nothing was written")
        return EXIT_FAILED
    config = LoopConfig(
        symbols=tuple(args.symbol),
        timeframe=Timeframe("5m"),
        limit=args.limit,
        publication_delay_ms=args.publication_delay_ms,
        max_concurrency=args.max_concurrency,
    )
    try:
        _migrate(db, clock)
        backfill = None if args.no_backfill else BackfillConfig(args.backfill_days)
        return asyncio.run(_run_loop(config, db, args.min_request_interval_ms, open_source, clock, sleep, backfill))
    except (SourceError, StorageError, sqlite3.Error) as error:  # raised before the loop started
        _error(f"error: {error}")
        return EXIT_FAILED


def _migrate(db: Path, clock: Clock) -> None:
    refuse_foreign_file(db)  # read-only, before open_writer switches the file to WAL
    conn = open_writer(db)
    try:
        migrate(conn, applied_at_ms=clock.wall_ms())
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


def main(
    argv: list[str] | None = None,
    *,
    open_source: OpenSource = BinanceUsdmSource.open,
    clock: Clock | None = None,
    sleep: Sleep = system_sleep,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_usage()
        return 0
    if args.command == "run":
        if len(set(args.symbol)) != len(args.symbol):
            parser.error("--symbol must not be repeated with the same value")
        needed = steady_state_limit_needed(args.publication_delay_ms, Timeframe("5m").milliseconds)
        if needed > args.limit:
            parser.error(
                f"--publication-delay-ms {args.publication_delay_ms} needs --limit >= {needed}:"
                " with fewer rows no round can reach the newest expected candle"
            )
        return _run_command(args, open_source, clock or SystemClock(), sleep)
    try:
        return _fetch_once(args, open_source, clock or SystemClock())
    except KeyboardInterrupt:
        _error(
            "interrupted: whether the last batch was stored is unknown; "
            "re-run the same command to reconcile (writes are idempotent)"
        )
        return EXIT_INTERRUPTED
