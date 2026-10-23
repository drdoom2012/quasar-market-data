import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter, WriterState
from quasar_market_data.collector.candle_writer import (
    CandleConflict,
    CandleWriteError,
    CandleWriteOutcomeUnknown,
    CandleWriter,
    WriteReport,
)
from quasar_market_data.domain.validation import ValidatedCandle
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate
from quasar_market_data.storage.schema import SchemaMismatchError
from tests.collector.test_candle_writer import AT, valid

WORKER_PREFIX = "candle-writer"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "market.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=AT)
    conn.close()
    return path


def state_of(writer: AsyncCandleWriter) -> WriterState:
    return writer.state  # a function call, so mypy does not narrow the state across awaits


def stored_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT count(*) FROM candles").fetchone()[0])
    finally:
        conn.close()


def worker_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(WORKER_PREFIX)]


def wait_for_no_workers(timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not worker_threads():
            return True
        time.sleep(0.01)
    return not worker_threads()


class Hold:
    """Holds the first call of a patched function inside the worker thread until ``release`` is set."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self._used = False

    def maybe_hold(self) -> None:
        if not self._used:
            self._used = True
            self.started.set()
            assert self.release.wait(5), "test never released the held worker job"


async def wait_started(hold: Hold) -> None:
    assert await asyncio.get_running_loop().run_in_executor(None, hold.started.wait, 5)


def spy_on_write(monkeypatch: pytest.MonkeyPatch, hold: Hold | None = None) -> list[int]:
    """Record the thread of every ``CandleWriter.write`` call; optionally hold the first call."""
    idents: list[int] = []
    real = CandleWriter.write

    def spy(self: CandleWriter, candles: Sequence[ValidatedCandle], *, ingested_at_ms: int) -> WriteReport:
        idents.append(threading.get_ident())
        if hold is not None:
            hold.maybe_hold()
        return real(self, candles, ingested_at_ms=ingested_at_ms)

    monkeypatch.setattr(CandleWriter, "write", spy)
    return idents


def spy_on_close(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> list[int]:
    calls: list[int] = []
    real = CandleWriter.close

    def spy(self: CandleWriter) -> None:
        calls.append(threading.get_ident())
        if fail:
            raise RuntimeError("injected close failure")
        real(self)

    monkeypatch.setattr(CandleWriter, "close", spy)
    return calls


def test_all_writes_run_on_one_dedicated_thread_off_the_event_loop(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    idents = spy_on_write(monkeypatch)

    async def scenario() -> int:
        writer = await AsyncCandleWriter.open(db)
        try:
            for n in range(3):
                await writer.write([valid(n)], ingested_at_ms=AT)
        finally:
            await writer.close()
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())
    assert len(idents) == 3
    assert len(set(idents)) == 1
    assert idents[0] != loop_thread


def test_conflict_is_recorded_through_the_async_writer(db: Path) -> None:
    stored, conflicting = valid(0), valid(0, close="100.1")

    async def scenario() -> tuple[WriteReport, WriteReport]:
        writer = await AsyncCandleWriter.open(db)
        try:
            await writer.write([stored], ingested_at_ms=AT)
            first = await writer.write([conflicting], ingested_at_ms=AT + 1)
            return first, await writer.write([conflicting], ingested_at_ms=AT + 2)
        finally:
            await writer.close()

    first, second = asyncio.run(scenario())
    assert first.conflicts == second.conflicts == (CandleConflict(conflicting, stored),)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT close, first_seen_at_ms FROM candle_conflicts").fetchall() == [
            (conflicting.close, AT + 1)
        ]
        assert conn.execute("SELECT close FROM candles").fetchall() == [(stored.close,)]
    finally:
        conn.close()


def test_unknown_outcome_reaches_the_async_caller_unchanged(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unknown = CandleWriteOutcomeUnknown("outcome unknown")

    def raise_unknown(self: CandleWriter, candles: Sequence[ValidatedCandle], *, ingested_at_ms: int) -> WriteReport:
        raise unknown

    monkeypatch.setattr(CandleWriter, "write", raise_unknown)

    async def scenario() -> BaseException:
        writer = await AsyncCandleWriter.open(db)
        try:
            with pytest.raises(CandleWriteOutcomeUnknown) as caught:
                await writer.write([valid(0)], ingested_at_ms=AT)
            return caught.value
        finally:
            await writer.close()

    assert asyncio.run(scenario()) is unknown


def test_concurrent_writes_are_serialized_and_none_is_lost(db: Path) -> None:
    async def scenario() -> list[WriteReport]:
        writer = await AsyncCandleWriter.open(db)
        try:
            calls = [writer.write([valid(n)], ingested_at_ms=AT) for n in range(20)]
            calls += [writer.write([valid(0)], ingested_at_ms=AT) for _ in range(5)]
            return list(await asyncio.gather(*calls))
        finally:
            await writer.close()

    reports = asyncio.run(scenario())
    assert sum(r.inserted for r in reports) == 20
    assert sum(r.duplicates for r in reports) == 5
    assert stored_count(db) == 20


def test_open_fails_closed_on_an_unmigrated_database(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    open_writer(path).close()
    with pytest.raises(SchemaMismatchError):
        asyncio.run(AsyncCandleWriter.open(path))
    assert wait_for_no_workers()


def test_event_loop_stays_responsive_while_a_write_waits(db: Path) -> None:
    blocker = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    threading.Timer(0.2, lambda: blocker.execute("ROLLBACK")).start()

    async def scenario() -> tuple[WriteReport, int]:
        writer = await AsyncCandleWriter.open(db, busy_timeout_ms=1000)
        ticks = 0
        done = False

        async def ticker() -> None:
            nonlocal ticks
            while not done:
                ticks += 1
                await asyncio.sleep(0.01)

        tick_task = asyncio.create_task(ticker())
        try:
            report = await writer.write([valid(0)], ingested_at_ms=AT)
        finally:
            done = True
            await tick_task
            await writer.close()
        return report, ticks

    report, ticks = asyncio.run(scenario())
    blocker.close()
    assert report.inserted == 1
    assert ticks >= 5


# --- lifecycle -----------------------------------------------------------------------------------------------


def test_write_after_close_has_started_is_rejected(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hold = Hold()
    spy_on_write(monkeypatch, hold)

    async def scenario() -> WriteReport:
        writer = await AsyncCandleWriter.open(db)
        held = asyncio.create_task(writer.write([valid(0)], ingested_at_ms=AT))
        await wait_started(hold)
        closer = asyncio.create_task(writer.close())
        await asyncio.sleep(0)
        assert state_of(writer) is WriterState.CLOSING
        with pytest.raises(CandleWriteError, match="closing"):
            await writer.write([valid(1)], ingested_at_ms=AT)
        hold.release.set()
        report = await held
        await closer
        assert state_of(writer) is WriterState.CLOSED
        return report

    assert asyncio.run(scenario()) == WriteReport(1, 0, ())
    assert stored_count(db) == 1


def test_close_waits_for_every_accepted_write(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hold = Hold()
    spy_on_write(monkeypatch, hold)

    async def scenario() -> list[WriteReport]:
        writer = await AsyncCandleWriter.open(db)
        writes = [asyncio.create_task(writer.write([valid(n)], ingested_at_ms=AT)) for n in range(3)]
        await wait_started(hold)
        closer = asyncio.create_task(writer.close())
        await asyncio.sleep(0.05)
        assert not closer.done()  # accepted writes are still pending behind the held one
        hold.release.set()
        reports = list(await asyncio.gather(*writes))
        await closer
        return reports

    reports = asyncio.run(scenario())
    assert [r.inserted for r in reports] == [1, 1, 1]
    assert stored_count(db) == 3


def test_write_close_race_never_loses_an_accepted_write(db: Path) -> None:
    async def scenario() -> list[Any]:
        writer = await AsyncCandleWriter.open(db)
        first = asyncio.create_task(writer.write([valid(0)], ingested_at_ms=AT))
        closer = asyncio.create_task(writer.close())
        second = asyncio.create_task(writer.write([valid(1)], ingested_at_ms=AT))
        return list(await asyncio.gather(first, closer, second, return_exceptions=True))

    first, closed, second = asyncio.run(scenario())
    assert first == WriteReport(1, 0, ())
    assert closed is None
    assert isinstance(second, CandleWriteError)
    assert stored_count(db) == 1


def test_concurrent_close_calls_close_the_connection_once(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closes = spy_on_close(monkeypatch)

    async def scenario() -> WriterState:
        writer = await AsyncCandleWriter.open(db)
        await asyncio.gather(writer.close(), writer.close())
        return writer.state

    assert asyncio.run(scenario()) is WriterState.CLOSED
    assert len(closes) == 1


def test_close_is_idempotent_after_closed(db: Path) -> None:
    async def scenario() -> None:
        writer = await AsyncCandleWriter.open(db)
        await writer.close()
        await writer.close()
        await writer.close()
        assert state_of(writer) is WriterState.CLOSED

    asyncio.run(scenario())
    assert wait_for_no_workers()


def test_close_failure_is_reported_and_the_writer_still_ends_closed(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spy_on_close(monkeypatch, fail=True)

    async def scenario() -> list[Any]:
        writer = await AsyncCandleWriter.open(db)
        results = await asyncio.gather(writer.close(), writer.close(), return_exceptions=True)
        await writer.close()  # later calls return without raising
        assert state_of(writer) is WriterState.CLOSED
        return list(results)

    results = asyncio.run(scenario())
    assert all(isinstance(r, CandleWriteError) for r in results)
    assert wait_for_no_workers()


def test_write_after_closed_raises(db: Path) -> None:
    async def scenario() -> None:
        writer = await AsyncCandleWriter.open(db)
        await writer.close()
        with pytest.raises(CandleWriteError, match="closed"):
            await writer.write([valid(0)], ingested_at_ms=AT)

    asyncio.run(scenario())


# --- cancellation --------------------------------------------------------------------------------------------


def test_cancelled_started_write_still_completes(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hold = Hold()
    spy_on_write(monkeypatch, hold)

    async def scenario() -> None:
        writer = await AsyncCandleWriter.open(db)
        task = asyncio.create_task(writer.write([valid(0)], ingested_at_ms=AT))
        await wait_started(hold)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state_of(writer) is WriterState.OPEN
        hold.release.set()
        await writer.close()  # waits for the running job

    asyncio.run(scenario())
    assert stored_count(db) == 1  # the transaction completed although its caller was cancelled

    async def rewrite() -> WriteReport:
        writer = await AsyncCandleWriter.open(db)
        try:
            return await writer.write([valid(0)], ingested_at_ms=AT)
        finally:
            await writer.close()

    assert asyncio.run(rewrite()) == WriteReport(0, 1, ())  # re-sending after a cancel is safe


def test_cancelled_queued_write_is_withdrawn(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Best effort: the queued job is withdrawn once the event loop has processed the cancellation. The held
    # first job keeps the worker busy, so the second job cannot start before that happens.
    hold = Hold()
    spy_on_write(monkeypatch, hold)

    async def scenario() -> None:
        writer = await AsyncCandleWriter.open(db)
        first = asyncio.create_task(writer.write([valid(0)], ingested_at_ms=AT))
        await wait_started(hold)
        second = asyncio.create_task(writer.write([valid(1)], ingested_at_ms=AT))
        await asyncio.sleep(0.05)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        await asyncio.sleep(0.05)
        hold.release.set()
        assert (await first).inserted == 1
        await writer.close()

    asyncio.run(scenario())
    assert stored_count(db) == 1


def test_writer_stays_usable_after_a_cancelled_write(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hold = Hold()
    spy_on_write(monkeypatch, hold)

    async def scenario() -> WriteReport:
        writer = await AsyncCandleWriter.open(db)
        task = asyncio.create_task(writer.write([valid(0)], ingested_at_ms=AT))
        await wait_started(hold)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        hold.release.set()
        try:
            return await writer.write([valid(1)], ingested_at_ms=AT)
        finally:
            await writer.close()

    assert asyncio.run(scenario()) == WriteReport(1, 0, ())
    assert stored_count(db) == 2


def test_cancelled_close_still_closes(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hold = Hold()
    spy_on_write(monkeypatch, hold)

    async def scenario() -> None:
        writer = await AsyncCandleWriter.open(db)
        held = asyncio.create_task(writer.write([valid(0)], ingested_at_ms=AT))
        await wait_started(hold)
        closer = asyncio.create_task(writer.close())
        await asyncio.sleep(0)
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closer
        assert state_of(writer) is WriterState.CLOSING  # closing continues without its caller
        hold.release.set()
        await held
        await writer.close()
        assert state_of(writer) is WriterState.CLOSED
        with pytest.raises(CandleWriteError, match="closed"):
            await writer.write([valid(1)], ingested_at_ms=AT)

    asyncio.run(scenario())
    assert wait_for_no_workers()


def test_cancelled_open_leaves_nothing_behind(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hold = Hold()
    real_open: Callable[..., sqlite3.Connection] = open_writer

    def gated_open(path: Path, *, busy_timeout_ms: int) -> sqlite3.Connection:
        hold.maybe_hold()
        return real_open(path, busy_timeout_ms=busy_timeout_ms)

    monkeypatch.setattr("quasar_market_data.collector.async_candle_writer.open_writer", gated_open)
    closes = spy_on_close(monkeypatch)

    async def scenario() -> None:
        opening = asyncio.create_task(AsyncCandleWriter.open(db))
        await wait_started(hold)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        hold.release.set()

    asyncio.run(scenario())
    assert wait_for_no_workers()
    assert len(closes) == 1  # the connection the worker finished opening was closed
