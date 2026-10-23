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
"""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from importlib.metadata import version
from pathlib import Path

from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.binance_klines import KlineParseError
from quasar_market_data.collector.binance_usdm import BinanceUsdmSource
from quasar_market_data.collector.candle_writer import CandleWriteOutcomeUnknown
from quasar_market_data.collector.fetch_once import (
    MAX_LIMIT,
    Clock,
    FetchOnceConfig,
    FetchOnceReport,
    SystemClock,
    fetch_once,
)
from quasar_market_data.collector.kline_source import KlineSource, SourceError
from quasar_market_data.domain.closed_candles import DEFAULT_PUBLICATION_DELAY_MS
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import StorageError, open_writer
from quasar_market_data.storage.migrate import migrate

PROG = "quasar-collector"
DEFAULT_LIMIT = 100
EXIT_FINDINGS = 3
EXIT_FAILED = 1
EXIT_UNKNOWN_OUTCOME = 4
EXIT_INTERRUPTED = 130

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


def _migrate(db: Path, clock: Clock) -> None:
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
    except (SourceError, KlineParseError, StorageError) as error:
        _error(f"error: {error}")
        return EXIT_FAILED


def main(
    argv: list[str] | None = None,
    *,
    open_source: OpenSource = BinanceUsdmSource.open,
    clock: Clock | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_usage()
        return 0
    try:
        return _fetch_once(args, open_source, clock or SystemClock())
    except KeyboardInterrupt:
        _error(
            "interrupted: whether the last batch was stored is unknown; "
            "re-run the same command to reconcile (writes are idempotent)"
        )
        return EXIT_INTERRUPTED
