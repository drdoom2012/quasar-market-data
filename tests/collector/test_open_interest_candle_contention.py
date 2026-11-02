"""Open interest yields to candle writes on a real SQLite file (ADR-0003 P-9, P-15 contention rows).

Both async writers run in one event loop on one database file; the test wraps each candle write in
``CandleWriteActivity.writing()`` as OI-4 will. The candle side is observed through a recording connection
(``async_candle_writer.open_writer`` is monkeypatched in the test only; the candle code is unchanged).

``test_candle_write_waits_for_at_most_one_open_interest_transaction`` checks the order on a recorded timeline; the
deterministic guard for "no open interest BEGIN while a candle write is active" is
``test_async_open_interest_writer.py::test_waits_while_candle_write_is_active``.
"""

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

from quasar_market_data.collector import async_candle_writer
from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.async_open_interest_writer import AsyncOpenInterestWriter, OpenInterestPageError
from quasar_market_data.collector.candle_write_activity import CandleWriteActivity
from quasar_market_data.collector.open_interest_writer import OpenInterestWriteBusy
from quasar_market_data.domain.candle import Candle, CandleKey
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.validation import ValidatedCandle, require_valid
from quasar_market_data.storage.connection import open_reader, open_writer
from tests.collector.faulty_connection import FaultyConnection, open_faulty_writer
from tests.collector.test_async_open_interest_writer import AT, GatedConnection, factory, page

P5 = Timeframe("5m")
Timeline = list[tuple[str, int]]


def candle(index: int = 0) -> ValidatedCandle:
    key = CandleKey("binance-usdm", "BTC/USDT:USDT", P5, 1_793_620_500_000 + index * 300_000)
    return require_valid(Candle(key, "100", "101", "99", "100", "5", "500"))


def count(db: Path, table: str) -> int:
    conn = open_reader(db)
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


class TimelineGate(GatedConnection):
    """The open interest connection: records its BEGIN and COMMIT on a shared timeline, gates COMMIT #1."""

    timeline: ClassVar[Timeline] = []

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if self.armed and sql == "BEGIN IMMEDIATE":
            self.timeline.append(("oi_begin", time.monotonic_ns()))
        cursor = super().execute(sql, *args)
        if self.armed and sql == "COMMIT":
            self.timeline.append(("oi_commit", time.monotonic_ns()))
        return cursor


class CandleRecorder(sqlite3.Connection):
    timeline: ClassVar[Timeline] = []
    begin_issued = threading.Event()

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql == "BEGIN IMMEDIATE":
            self.timeline.append(("candle_begin_issued", time.monotonic_ns()))
            self.begin_issued.set()
        cursor = super().execute(sql, *args)
        if sql == "COMMIT":
            self.timeline.append(("candle_commit", time.monotonic_ns()))
        return cursor


def open_recording_candle_writer(path: Path, *, busy_timeout_ms: int) -> sqlite3.Connection:
    conn = sqlite3.connect(path, factory=CandleRecorder, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA recursive_triggers=ON")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return conn


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "market.db"
    open_faulty_writer(path, AT).close()  # migrated, as the Collector does before opening writers
    return path


def test_candle_write_waits_for_at_most_one_open_interest_transaction(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeline: Timeline = []
    monkeypatch.setattr(TimelineGate, "timeline", timeline)
    monkeypatch.setattr(CandleRecorder, "timeline", timeline)
    monkeypatch.setattr(CandleRecorder, "begin_issued", threading.Event())
    monkeypatch.setattr(async_candle_writer, "open_writer", open_recording_candle_writer)

    async def scenario() -> None:
        activity = CandleWriteActivity()
        made: list[FaultyConnection] = []
        oi_writer = await AsyncOpenInterestWriter.open(db, activity=activity, connect=factory(made, TimelineGate))
        candle_writer = await AsyncCandleWriter.open(db)
        gate = made[0]
        assert isinstance(gate, TimelineGate)

        async def write_candle() -> None:
            with activity.writing():
                await candle_writer.write([candle()], ingested_at_ms=AT)

        oi_task = asyncio.ensure_future(oi_writer.write(page(300), ingested_at_ms=AT))
        assert await asyncio.to_thread(gate.reached.wait, 10)  # OI transaction 1 holds the lock, COMMIT pending
        candle_task = asyncio.ensure_future(write_candle())
        assert await asyncio.to_thread(CandleRecorder.begin_issued.wait, 10)  # candle BEGIN waits on the lock
        gate.release.set()
        await candle_task
        report = await oi_task
        await oi_writer.close()
        await candle_writer.close()
        assert report.inserted == 300

    asyncio.run(scenario())

    events = [name for name, _ in sorted(timeline, key=lambda event: event[1])]
    first_commit = events.index("oi_commit")
    candle_commit = events.index("candle_commit")
    second_begin = [i for i, name in enumerate(events) if name == "oi_begin"][1]
    assert first_commit < candle_commit < second_begin, events
    assert events[:candle_commit].count("oi_begin") == 1, events
    assert count(db, "open_interest") == 300 and count(db, "candles") == 1


def test_busy_on_open_interest_never_reaches_the_candle_writer(db: Path) -> None:
    async def scenario() -> None:
        activity = CandleWriteActivity()
        oi_writer = await AsyncOpenInterestWriter.open(db, activity=activity, busy_timeout_ms=50)
        candle_writer = await AsyncCandleWriter.open(db)
        holder = open_writer(db)
        holder.execute("BEGIN IMMEDIATE")
        oi_task = asyncio.ensure_future(oi_writer.write(page(10), ingested_at_ms=AT))
        candle_task = asyncio.ensure_future(candle_writer.write([candle()], ingested_at_ms=AT))
        await asyncio.sleep(0.3)
        holder.execute("ROLLBACK")
        holder.close()
        with pytest.raises(OpenInterestPageError) as error:
            await oi_task
        assert isinstance(error.value.cause, OpenInterestWriteBusy)
        assert (await candle_task).inserted == 1
        await oi_writer.close()
        await candle_writer.close()

    asyncio.run(scenario())
    assert count(db, "candles") == 1 and count(db, "open_interest") == 0
    assert count(db, "candle_conflicts") == 0 and count(db, "open_interest_conflicts") == 0
