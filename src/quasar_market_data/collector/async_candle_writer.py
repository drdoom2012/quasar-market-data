"""Asyncio front for ``CandleWriter``: all SQLite work runs on one dedicated worker thread (spec D6).

One single-thread executor is the only place SQLite is touched. The connection is created inside that
thread (connections are thread-bound) and every job, including the close job, goes to the same executor, so
jobs run strictly in submission order.

Lifecycle: ``OPEN -> CLOSING -> CLOSED``, one way. ``write`` checks the state and submits its job with no
``await`` in between, so the check and the acceptance are atomic with respect to ``close``. A submitted job
is an *accepted write*: because the executor is FIFO, every write accepted before ``close`` runs to
completion before the connection is closed. After ``close`` has started, ``write`` raises.

Cancellation: a cancelled caller never gets a result and never leaves the worker mid-transaction. A write
that is already running completes (commit or rollback) on the worker thread; a write still queued is
withdrawn once the event loop has processed the cancellation, which is best effort, so a cancelled caller
cannot know whether its batch was applied. Re-sending it is safe because writes are idempotent. A cancelled
``close`` keeps closing; a cancelled ``open`` closes the connection the worker finishes opening.
"""

import asyncio
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from enum import StrEnum
from functools import partial
from pathlib import Path

from quasar_market_data.collector.candle_writer import CandleWriteError, CandleWriter, WriteReport
from quasar_market_data.domain.validation import ValidatedCandle
from quasar_market_data.storage.connection import DEFAULT_BUSY_TIMEOUT_MS, open_writer

_THREAD_NAME_PREFIX = "candle-writer"


class WriterState(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


def _connect(path: Path, busy_timeout_ms: int) -> CandleWriter:
    conn = open_writer(path, busy_timeout_ms=busy_timeout_ms)
    try:
        return CandleWriter(conn)
    except BaseException:
        conn.close()
        raise


def _discard_when_connected(executor: ThreadPoolExecutor, connecting: "Future[CandleWriter]") -> None:
    """After a cancelled ``open``: close whatever the worker produced, then stop the executor."""

    def cleanup(done: "Future[CandleWriter]") -> None:
        if not done.cancelled() and done.exception() is None:
            executor.submit(done.result().close)  # the connection must be closed on its own thread
        executor.shutdown(wait=False)

    connecting.add_done_callback(cleanup)


def _retrieve_exception(task: "asyncio.Task[None]") -> None:
    if not task.cancelled():
        task.exception()  # mark it retrieved so an unawaited failure does not log at garbage collection


class AsyncCandleWriter:
    def __init__(self, executor: ThreadPoolExecutor, writer: CandleWriter) -> None:
        self._executor = executor
        self._writer = writer
        self._state = WriterState.OPEN
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    async def open(cls, path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> "AsyncCandleWriter":
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=_THREAD_NAME_PREFIX)
        connecting = executor.submit(_connect, path, busy_timeout_ms)
        try:
            writer = await asyncio.wrap_future(connecting)
        except asyncio.CancelledError:
            _discard_when_connected(executor, connecting)
            raise
        except BaseException:
            executor.shutdown(wait=False)
            raise
        return cls(executor, writer)

    @property
    def state(self) -> WriterState:
        return self._state

    async def write(self, candles: Sequence[ValidatedCandle], *, ingested_at_ms: int) -> WriteReport:
        if self._state is not WriterState.OPEN:
            raise CandleWriteError(f"writer is {self._state.value}")
        job = partial(self._writer.write, tuple(candles), ingested_at_ms=ingested_at_ms)
        return await asyncio.get_running_loop().run_in_executor(self._executor, job)

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
            await loop.run_in_executor(self._executor, self._writer.close)  # runs after every accepted write
        except Exception as error:
            raise CandleWriteError(f"closing the connection failed: {error}") from error
        finally:  # the executor is stopped and the state is final even when closing failed
            await loop.run_in_executor(None, self._executor.shutdown)
            self._state = WriterState.CLOSED
