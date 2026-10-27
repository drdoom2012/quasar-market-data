"""The collection loop against the real ``AsyncCandleWriter`` (a worker thread); scheduling stays on ``ManualTime``.

The only real-time waits are safety timeouts that fail the test; no sleep stands in for a race.
"""

import asyncio
import sqlite3
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.candle_writer import CandleWriter, WriteReport
from quasar_market_data.collector.collection_loop import (
    LoopConfig,
    LoopExit,
    RoundReport,
    run_collection_loop,
)
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.validation import ValidatedCandle
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.loop_fakes import ScriptedSource
from tests.collector.manual_time import NS_PER_MS, ManualTime, assert_no_leftover_tasks
from tests.collector.test_candle_writer import AT

GRACE = 10_000
SAFETY_S = 5.0


class BlockingWriter:
    """Stands in for ``CandleWriter`` on the worker thread: ``write`` blocks until the test releases it."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = False

    def write(self, candles: Sequence[ValidatedCandle], *, ingested_at_ms: int) -> WriteReport:
        self.started.set()
        if not self.release.wait(SAFETY_S):
            raise AssertionError("the test never released the blocked write")
        return WriteReport(len(candles), 0, ())

    def close(self) -> None:
        self.closed = True


def config() -> LoopConfig:
    return LoopConfig(symbols=("A",), timeframe=Timeframe("5m"), limit=100)


def test_a_cancelled_blocked_write_stays_uncertain_and_close_waits_for_it() -> None:
    async def scenario() -> None:
        t = ManualTime()
        source = ScriptedSource(t)
        blocking = BlockingWriter()
        writer = AsyncCandleWriter(ThreadPoolExecutor(max_workers=1), cast(CandleWriter, blocking))
        stop = asyncio.Event()
        rounds: list[RoundReport] = []
        loop_task = asyncio.create_task(run_collection_loop(source, writer, config(), t, t.sleep, stop, rounds.append))
        in_write = await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, SAFETY_S)
        assert in_write  # the worker thread is inside write
        stop.set()
        await t.until_waiting(1)
        stop_instant_ns = t.monotonic_ns()
        assert t.waiting == [stop_instant_ns + GRACE * NS_PER_MS]
        await t.advance(GRACE)
        assert await loop_task == LoopExit(("A",))
        assert rounds[0].results[0].last_problem == "shutdown grace expired"

        closing = asyncio.create_task(writer.close())  # queued behind the write on the same worker thread
        await t.settle()
        assert not closing.done() and not blocking.closed  # the grace did not bound close()
        blocking.release.set()
        await asyncio.wait_for(closing, SAFETY_S)
        assert blocking.closed
        await assert_no_leftover_tasks()

    asyncio.run(scenario())


def test_real_writer_stop_after_a_completed_write_is_clean(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = tmp_path / "market.db"
        conn = open_writer(db)
        migrate(conn, applied_at_ms=AT)
        conn.close()
        t = ManualTime()
        source = ScriptedSource(t)
        writer = await AsyncCandleWriter.open(db)
        stop = asyncio.Event()
        done = asyncio.Event()
        rounds: list[RoundReport] = []

        def on_round(report: RoundReport) -> None:
            rounds.append(report)
            done.set()

        loop_task = asyncio.create_task(run_collection_loop(source, writer, config(), t, t.sleep, stop, on_round))
        await asyncio.wait_for(done.wait(), SAFETY_S)  # the real thread finished the write
        stop.set()
        assert await loop_task == LoopExit(())
        await writer.close()
        conn2 = sqlite3.connect(db)
        try:
            assert conn2.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 3
        finally:
            conn2.close()

    asyncio.run(scenario())
