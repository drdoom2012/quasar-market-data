import asyncio
import gc
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector.async_candle_writer import WriterState
from quasar_market_data.collector.async_open_interest_writer import (
    DEFAULT_OPEN_INTEREST_BUSY_TIMEOUT_MS,
    AsyncOpenInterestWriter,
    OpenInterestPageError,
)
from quasar_market_data.collector.candle_write_activity import CandleWriteActivity
from quasar_market_data.collector.open_interest_writer import (
    OpenInterestWriteBusy,
    OpenInterestWriteError,
    OpenInterestWriteOutcomeUnknown,
)
from quasar_market_data.domain.open_interest import OpenInterest, OpenInterestKey
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.faulty_connection import Fault, FaultyConnection, open_faulty_writer

AT = 1_793_621_000_000
T = 1_793_620_800_000
P5 = Timeframe("5m")
INSERT_ROW = "INSERT INTO open_interest ("
BEGIN = "BEGIN IMMEDIATE"


def page(count: int, start: int = 0, value: str = "1") -> list[OpenInterest]:
    return [
        OpenInterest(OpenInterestKey("binance-usdm", "BTC/USDT:USDT", P5, T + (start + i) * 300_000), value, "2")
        for i in range(count)
    ]


def stored_count(db: Path) -> int:
    conn = open_reader(db)
    try:
        return int(conn.execute("SELECT count(*) FROM open_interest").fetchone()[0])
    finally:
        conn.close()


def conflict_count(db: Path) -> int:
    conn = open_reader(db)
    try:
        return int(conn.execute("SELECT count(*) FROM open_interest_conflicts").fetchone()[0])
    finally:
        conn.close()


class GatedConnection(FaultyConnection):
    """Blocks the ``gate_on``-th COMMIT on the worker thread: ``reached`` is set, then it waits for ``release``.

    With ``after`` the COMMIT executes first and blocks afterwards.
    """

    gate_on = 1
    after = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.armed = False  # armed by ``factory`` after the migration's own COMMITs
        self.commits = 0
        self.reached = threading.Event()
        self.release = threading.Event()

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql != "COMMIT" or not self.armed:
            return super().execute(sql, *args)
        self.commits += 1
        if self.commits != self.gate_on:
            return super().execute(sql, *args)
        if self.after:
            cursor = super().execute(sql, *args)
            self.reached.set()
            assert self.release.wait(10)
            return cursor
        self.reached.set()
        assert self.release.wait(10)
        return super().execute(sql, *args)


def factory(
    made: list[FaultyConnection], kind: type[FaultyConnection] = FaultyConnection
) -> Callable[[Path, int], sqlite3.Connection]:
    def connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
        conn = open_faulty_writer(path, AT, factory=kind)
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        if isinstance(conn, GatedConnection):
            conn.armed = True
        made.append(conn)
        return conn

    return connect


def begins(conn: FaultyConnection) -> int:
    return sum(1 for sql in conn.statements if sql == BEGIN)


async def spin(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)  # real time, so a job wrongly submitted to the worker thread would have started


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "market.db"


def test_page_of_500_is_five_transactions(db: Path) -> None:
    async def scenario() -> None:
        made: list[FaultyConnection] = []
        writer = await AsyncOpenInterestWriter.open(db, activity=CandleWriteActivity(), connect=factory(made))
        report = await writer.write(page(500), ingested_at_ms=AT)
        await writer.close()
        assert report.inserted == 500 and len(report.durations_ns) == 5 and len(report.lock_wait_ns) == 5
        statements = made[0].statements
        assert begins(made[0]) == 5
        inside = 0
        for sql in statements:
            if sql == BEGIN:
                inside = 0
            elif sql.startswith(INSERT_ROW):
                inside += 1
                assert inside <= 100

    asyncio.run(scenario())
    assert stored_count(db) == 500


def test_failure_inside_the_third_transaction_keeps_two_and_retry_completes(db: Path) -> None:
    async def scenario() -> None:
        made: list[FaultyConnection] = []
        writer = await AsyncOpenInterestWriter.open(db, activity=CandleWriteActivity(), connect=factory(made))
        made[0].faults.append(Fault(INSERT_ROW, sqlite3.OperationalError("injected"), nth=251))
        with pytest.raises(OpenInterestPageError) as error:
            await writer.write(page(500), ingested_at_ms=AT)
        assert error.value.committed_transactions == 2
        assert error.value.partial.inserted == 200
        assert type(error.value.cause) is OpenInterestWriteError
        assert error.value.__cause__ is error.value.cause
        assert stored_count(db) == 200
        retry = await writer.write(page(500), ingested_at_ms=AT + 1)
        assert (retry.inserted, retry.duplicates, retry.conflicts) == (300, 200, ())
        await writer.close()

    asyncio.run(scenario())
    assert stored_count(db) == 500 and conflict_count(db) == 0


def test_outcome_unknown_is_the_cause(db: Path) -> None:
    async def scenario() -> None:
        made: list[FaultyConnection] = []
        writer = await AsyncOpenInterestWriter.open(db, activity=CandleWriteActivity(), connect=factory(made))
        made[0].faults.append(Fault("COMMIT", sqlite3.OperationalError("disk I/O error"), after_execution=True))
        with pytest.raises(OpenInterestPageError) as error:
            await writer.write(page(150), ingested_at_ms=AT)
        assert isinstance(error.value.cause, OpenInterestWriteOutcomeUnknown)
        assert error.value.committed_transactions == 0
        await writer.close()

    asyncio.run(scenario())


def test_busy_database_fails_only_that_transaction(db: Path) -> None:
    open_faulty_writer(db, AT).close()  # the Collector migrates before the writer opens

    async def scenario() -> None:
        writer = await AsyncOpenInterestWriter.open(db, activity=CandleWriteActivity(), busy_timeout_ms=50)
        holder = open_writer(db)
        holder.execute(BEGIN)
        try:
            with pytest.raises(OpenInterestPageError) as error:
                await writer.write(page(10), ingested_at_ms=AT)
            assert isinstance(error.value.cause, OpenInterestWriteBusy)
            assert error.value.committed_transactions == 0
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert stored_count(db) == 0 and conflict_count(db) == 0
        assert (await writer.write(page(10), ingested_at_ms=AT)).inserted == 10
        await writer.close()

    asyncio.run(scenario())


def test_waits_while_candle_write_is_active(db: Path) -> None:
    async def scenario() -> None:
        made: list[FaultyConnection] = []
        activity = CandleWriteActivity()
        ticks = iter([1_000, 4_000])
        writer = await AsyncOpenInterestWriter.open(
            db, activity=activity, connect=factory(made), monotonic_ns=lambda: next(ticks)
        )
        with activity.writing():
            task = asyncio.ensure_future(writer.write(page(10), ingested_at_ms=AT))
            await spin()
            assert begins(made[0]) == 0 and not task.done()
        report = await task
        assert report.inserted == 10 and report.yields == 1 and report.yield_wait_ns == 3_000
        await writer.close()

    asyncio.run(scenario())


def test_reentry_in_the_same_iteration_keeps_waiting(db: Path) -> None:
    async def scenario() -> None:
        made: list[FaultyConnection] = []
        activity = CandleWriteActivity()
        writer = await AsyncOpenInterestWriter.open(db, activity=activity, connect=factory(made))
        first = activity.writing()
        first.__enter__()
        task = asyncio.ensure_future(writer.write(page(10), ingested_at_ms=AT))
        await spin()
        first.__exit__(None, None, None)
        second = activity.writing()
        second.__enter__()  # no await in between: the waiter was woken but the activity is active again
        await spin()
        assert begins(made[0]) == 0 and not task.done()
        second.__exit__(None, None, None)
        assert (await task).inserted == 10
        await writer.close()

    asyncio.run(scenario())


def test_cancel_between_transactions(db: Path) -> None:
    class AfterFirstCommit(GatedConnection):
        after = True

    async def scenario() -> None:
        made: list[FaultyConnection] = []
        activity = CandleWriteActivity()
        writer = await AsyncOpenInterestWriter.open(db, activity=activity, connect=factory(made, AfterFirstCommit))
        conn = made[0]
        assert isinstance(conn, GatedConnection)
        task = asyncio.ensure_future(writer.write(page(300), ingested_at_ms=AT))
        assert await asyncio.to_thread(conn.reached.wait, 10)
        with activity.writing():
            conn.release.set()
            await spin()
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await spin()
        assert begins(conn) == 1
        assert writer.state is WriterState.OPEN
        assert (await writer.write(page(1, start=400), ingested_at_ms=AT)).inserted == 1
        await writer.close()

    asyncio.run(scenario())
    assert stored_count(db) == 101


def test_write_after_cancel_waits_for_the_running_transaction(db: Path) -> None:
    async def scenario() -> None:
        made: list[FaultyConnection] = []
        activity = CandleWriteActivity()
        writer = await AsyncOpenInterestWriter.open(db, activity=activity, connect=factory(made, GatedConnection))
        conn = made[0]
        assert isinstance(conn, GatedConnection)
        cancelled = asyncio.ensure_future(writer.write(page(100), ingested_at_ms=AT))
        assert await asyncio.to_thread(conn.reached.wait, 10)  # its COMMIT is blocked on the worker thread
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        follow_up = asyncio.ensure_future(writer.write(page(10, start=200), ingested_at_ms=AT))
        await spin()
        with activity.writing():
            conn.release.set()
            await spin()
            assert begins(conn) == 1, "the follow-up must not begin while a candle write is active"
            assert not follow_up.done()
        assert (await follow_up).inserted == 10
        assert begins(conn) == 2
        await writer.close()

    asyncio.run(scenario())
    assert stored_count(db) == 110


def test_lifecycle(db: Path) -> None:
    async def scenario() -> None:
        seen: dict[str, object] = {}

        def connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
            seen["thread"] = threading.current_thread().name
            seen["busy"] = busy_timeout_ms
            conn = open_writer(path, busy_timeout_ms=busy_timeout_ms)
            migrate(conn, applied_at_ms=AT)
            seen["pragma"] = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            return conn

        writer = await AsyncOpenInterestWriter.open(db, activity=CandleWriteActivity(), connect=connect)
        assert str(seen["thread"]).startswith("open-interest-writer")
        assert seen["busy"] == DEFAULT_OPEN_INTEREST_BUSY_TIMEOUT_MS == 1_000
        assert seen["pragma"] == 1_000
        states = [writer.state]
        await writer.close()
        await writer.close()
        states.append(writer.state)
        assert states == [WriterState.OPEN, WriterState.CLOSED]
        with pytest.raises(OpenInterestWriteError, match="closed"):
            await writer.write(page(1), ingested_at_ms=AT)

    asyncio.run(scenario())


def test_wrong_row_type_is_refused_before_any_transaction(db: Path) -> None:
    async def scenario() -> None:
        made: list[FaultyConnection] = []
        writer = await AsyncOpenInterestWriter.open(db, activity=CandleWriteActivity(), connect=factory(made))
        rows: list[Any] = [*page(150), object()]
        with pytest.raises(TypeError):
            await writer.write(rows, ingested_at_ms=AT)
        assert begins(made[0]) == 0
        await writer.close()

    asyncio.run(scenario())


class TwoGates(FaultyConnection):
    """Blocks COMMIT #1 and COMMIT #2 on the worker thread, each until its own release."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.armed = False
        self.commits = 0
        self.reached = {1: threading.Event(), 2: threading.Event()}
        self.release = {1: threading.Event(), 2: threading.Event()}

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql == "COMMIT" and self.armed:
            self.commits += 1
            if self.commits in self.reached:
                self.reached[self.commits].set()
                assert self.release[self.commits].wait(10)
        return super().execute(sql, *args)


def test_two_concurrent_writes_never_queue_a_transaction_past_the_activity_check(db: Path) -> None:
    def connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
        conn = open_faulty_writer(path, AT, factory=TwoGates)
        assert isinstance(conn, TwoGates)
        conn.armed = True
        made.append(conn)
        return conn

    made: list[FaultyConnection] = []

    async def scenario() -> None:
        activity = CandleWriteActivity()
        writer = await AsyncOpenInterestWriter.open(db, activity=activity, connect=connect)
        conn = made[0]
        assert isinstance(conn, TwoGates)
        first = asyncio.ensure_future(writer.write(page(200), ingested_at_ms=AT))
        second = asyncio.ensure_future(writer.write(page(200, start=300), ingested_at_ms=AT))
        assert await asyncio.to_thread(conn.reached[1].wait, 10)
        conn.release[1].set()  # both callers wake; only one may submit, the other must re-check
        assert await asyncio.to_thread(conn.reached[2].wait, 10)
        with activity.writing():
            conn.release[2].set()
            await spin()
            assert begins(conn) == 2, "a transaction queued behind COMMIT #2 began while a candle write was active"
        await asyncio.gather(first, second)
        assert begins(conn) == 4
        await writer.close()

    asyncio.run(scenario())
    assert stored_count(db) == 400


def test_a_cancelled_waiter_leaves_no_unretrieved_exception(db: Path) -> None:
    problems: list[dict[str, Any]] = []

    async def scenario() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: problems.append(context))
        made: list[FaultyConnection] = []
        writer = await AsyncOpenInterestWriter.open(
            db, activity=CandleWriteActivity(), connect=factory(made, GatedConnection)
        )
        conn = made[0]
        assert isinstance(conn, GatedConnection)
        conn.faults.append(Fault("COMMIT", sqlite3.OperationalError("injected COMMIT failure")))
        first = asyncio.ensure_future(writer.write(page(10), ingested_at_ms=AT))
        assert await asyncio.to_thread(conn.reached.wait, 10)
        first.cancel()
        second = asyncio.ensure_future(writer.write(page(10, start=50), ingested_at_ms=AT))
        await spin()  # second now waits for the first's transaction
        second.cancel()
        for task in (first, second):
            with pytest.raises(asyncio.CancelledError):
                await task
        conn.release.set()  # the first's COMMIT now fails with nobody awaiting it
        await asyncio.sleep(0.1)
        await writer.close()
        gc.collect()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert [p for p in problems if "never retrieved" in str(p.get("message", ""))] == []


def test_close_in_the_middle_of_a_page_reports_the_committed_part(db: Path) -> None:
    class AfterFirstCommit(GatedConnection):
        after = True

    async def scenario() -> None:
        made: list[FaultyConnection] = []
        writer = await AsyncOpenInterestWriter.open(
            db, activity=CandleWriteActivity(), connect=factory(made, AfterFirstCommit)
        )
        conn = made[0]
        assert isinstance(conn, GatedConnection)
        task = asyncio.ensure_future(writer.write(page(300), ingested_at_ms=AT))
        assert await asyncio.to_thread(conn.reached.wait, 10)
        closing = asyncio.ensure_future(writer.close())
        await spin(1)
        conn.release.set()
        with pytest.raises(OpenInterestPageError) as error:
            await task
        assert error.value.committed_transactions == 1
        assert error.value.partial.inserted == 100
        assert "closing" in str(error.value.cause)
        await closing

    asyncio.run(scenario())
    assert stored_count(db) == 100
