"""Asyncio front for ``OpenInterestWriter`` on its own thread and connection (ADR-0003 P-9).

Resource isolation: one single-thread executor and one writer connection of its own, separate from
``AsyncCandleWriter``; ``busy_timeout`` is 1 000 ms (the candle connection keeps 5 000 ms), so a locked database
fails an open interest transaction quickly instead of holding it.

Contention rules (P-9 rules 1 and 2): ``write`` splits a page into transactions of at most
``MAX_ROWS_PER_TRANSACTION`` rows, in input order. Immediately before submitting each transaction it waits while
``CandleWriteActivity`` is active (``while activity.active: await activity.wait_idle()``, with no ``await``
between the last check and the submission). At most one open interest transaction is ever in flight: before its
check, ``write`` waits for the previous transaction, also one left running by a cancelled ``write`` or submitted by
another concurrent ``write``; both conditions are re-checked after every ``await``. A candle write that starts after
the check can therefore wait for at most that one transaction.

A failing transaction raises ``OpenInterestPageError``: earlier transactions of the page stay committed, the error
carries their count and report, and re-sending the page is idempotent. Cancellation between transactions starts no
further one.

Lifecycle (``OPEN -> CLOSING -> CLOSED``), cancellation of ``open`` and ``close`` are copied from
``collector/async_candle_writer.py`` (keep the two in step).
"""

import asyncio
import sqlite3
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final

from quasar_market_data.collector.async_candle_writer import WriterState
from quasar_market_data.collector.candle_write_activity import CandleWriteActivity
from quasar_market_data.collector.open_interest_writer import (
    MAX_ROWS_PER_TRANSACTION,
    OpenInterestBatchReport,
    OpenInterestConflict,
    OpenInterestWriteError,
    OpenInterestWriter,
)
from quasar_market_data.domain.open_interest import OpenInterest
from quasar_market_data.domain.timestamps import ensure_timestamp_ms
from quasar_market_data.storage.connection import open_writer

DEFAULT_OPEN_INTEREST_BUSY_TIMEOUT_MS: Final = 1_000
_THREAD_NAME_PREFIX: Final = "open-interest-writer"

Connect = Callable[[Path, int], sqlite3.Connection]


@dataclass(frozen=True)
class OpenInterestWriteReport:
    inserted: int
    duplicates: int
    conflicts: tuple[OpenInterestConflict, ...]
    lock_wait_ns: tuple[int, ...]
    durations_ns: tuple[int, ...]
    yields: int  # transactions that had to wait for the candle writer at least once
    yield_wait_ns: int


class OpenInterestPageError(OpenInterestWriteError):
    """A transaction of a page failed; ``committed_transactions`` earlier ones are stored (``partial``)."""

    def __init__(self, cause: OpenInterestWriteError, committed_transactions: int, partial: OpenInterestWriteReport):
        self.cause = cause
        self.committed_transactions = committed_transactions
        self.partial = partial
        super().__init__(f"open interest page failed after {committed_transactions} committed transactions: {cause}")


class _Totals:
    def __init__(self) -> None:
        self.inserted = 0
        self.duplicates = 0
        self.conflicts: list[OpenInterestConflict] = []
        self.lock_wait_ns: list[int] = []
        self.durations_ns: list[int] = []
        self.yields = 0
        self.yield_wait_ns = 0

    def add(self, batch: OpenInterestBatchReport) -> None:
        self.inserted += batch.inserted
        self.duplicates += batch.duplicates
        self.conflicts.extend(batch.conflicts)
        self.lock_wait_ns.append(batch.lock_wait_ns)
        self.durations_ns.append(batch.duration_ns)

    def report(self) -> OpenInterestWriteReport:
        return OpenInterestWriteReport(
            self.inserted,
            self.duplicates,
            tuple(self.conflicts),
            tuple(self.lock_wait_ns),
            tuple(self.durations_ns),
            self.yields,
            self.yield_wait_ns,
        )


def _connect_writer(path: Path, busy_timeout_ms: int, connect: Connect | None) -> OpenInterestWriter:
    conn = open_writer(path, busy_timeout_ms=busy_timeout_ms) if connect is None else connect(path, busy_timeout_ms)
    try:
        return OpenInterestWriter(conn)
    except BaseException:
        conn.close()
        raise


def _discard_when_connected(executor: ThreadPoolExecutor, connecting: "Future[OpenInterestWriter]") -> None:
    def cleanup(done: "Future[OpenInterestWriter]") -> None:
        if not done.cancelled() and done.exception() is None:
            executor.submit(done.result().close)
        executor.shutdown(wait=False)

    connecting.add_done_callback(cleanup)


def _retrieve_future_exception(future: "asyncio.Future[OpenInterestBatchReport]") -> None:
    if not future.cancelled():
        future.exception()  # the write that submitted the job reports (or abandoned) it


def _retrieve_exception(task: "asyncio.Task[None]") -> None:
    if not task.cancelled():
        task.exception()


class AsyncOpenInterestWriter:
    def __init__(
        self,
        executor: ThreadPoolExecutor,
        writer: OpenInterestWriter,
        activity: CandleWriteActivity,
        monotonic_ns: Callable[[], int],
    ) -> None:
        self._executor = executor
        self._writer = writer
        self._activity = activity
        self._monotonic_ns = monotonic_ns
        self._state = WriterState.OPEN
        self._close_task: asyncio.Task[None] | None = None
        self._last_job: Future[OpenInterestBatchReport] | None = None

    @classmethod
    async def open(
        cls,
        path: Path,
        *,
        activity: CandleWriteActivity,
        busy_timeout_ms: int = DEFAULT_OPEN_INTEREST_BUSY_TIMEOUT_MS,
        connect: Connect | None = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> "AsyncOpenInterestWriter":
        """Open the writer; ``monotonic_ns`` times the yields (the sync writer times transactions itself)."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=_THREAD_NAME_PREFIX)
        connecting = executor.submit(_connect_writer, path, busy_timeout_ms, connect)
        try:
            writer = await asyncio.wrap_future(connecting)
        except asyncio.CancelledError:
            _discard_when_connected(executor, connecting)
            raise
        except BaseException:
            executor.shutdown(wait=False)
            raise
        return cls(executor, writer, activity, monotonic_ns)

    @property
    def state(self) -> WriterState:
        return self._state

    async def _wait_for_previous(self) -> None:
        job = self._last_job
        if job is None or job.done():
            return
        waiter = asyncio.wrap_future(job)
        waiter.add_done_callback(_retrieve_future_exception)  # also when this write is cancelled while waiting
        await asyncio.wait({waiter})  # never raises the job's error; cancelling this write does not cancel the job

    async def _wait_for_candles(self, totals: _Totals) -> bool:
        """Wait while a candle write is active; ``True`` when it had to wait."""
        if not self._activity.active:
            return False
        started = self._monotonic_ns()
        while self._activity.active:
            await self._activity.wait_idle()
        totals.yield_wait_ns += self._monotonic_ns() - started
        return True

    async def write(self, rows: Sequence[OpenInterest], *, ingested_at_ms: int) -> OpenInterestWriteReport:
        if self._state is not WriterState.OPEN:
            raise OpenInterestWriteError(f"writer is {self._state.value}")
        ensure_timestamp_ms(ingested_at_ms, "ingested_at_ms")
        page = tuple(rows)
        for index, row in enumerate(page):
            if type(row) is not OpenInterest:
                raise TypeError(f"rows[{index}] must be an OpenInterest, got {type(row).__name__}")
        totals = _Totals()
        for committed, start in enumerate(range(0, len(page), MAX_ROWS_PER_TRANSACTION)):
            chunk = page[start : start + MAX_ROWS_PER_TRANSACTION]
            yielded = False
            while True:  # several callers may wait at once: re-check both conditions after every await
                await self._wait_for_previous()
                yielded = await self._wait_for_candles(totals) or yielded
                if (self._last_job is None or self._last_job.done()) and not self._activity.active:
                    break
            totals.yields += yielded
            if self._state is not WriterState.OPEN:
                closed = OpenInterestWriteError(f"writer is {self._state.value}")
                raise OpenInterestPageError(closed, committed, totals.report()) from closed
            # no await between the last activity check and the submission
            job = self._executor.submit(partial(self._writer.write_transaction, chunk, ingested_at_ms=ingested_at_ms))
            self._last_job = job
            try:
                batch = await asyncio.wrap_future(job)
            except OpenInterestWriteError as error:
                raise OpenInterestPageError(error, committed, totals.report()) from error
            totals.add(batch)
        return totals.report()

    async def close(self) -> None:
        if self._state is WriterState.CLOSED:
            return
        if self._close_task is None:
            self._state = WriterState.CLOSING
            self._close_task = asyncio.ensure_future(self._do_close())
            self._close_task.add_done_callback(_retrieve_exception)
        await asyncio.shield(self._close_task)

    async def _do_close(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._executor, self._writer.close)  # runs after every accepted transaction
        except Exception as error:
            raise OpenInterestWriteError(f"closing the connection failed: {error}") from error
        finally:
            await loop.run_in_executor(None, self._executor.shutdown)
            self._state = WriterState.CLOSED
