"""Controlled test time: nothing advances unless the test says so.

``ManualTime`` is both the ``Clock`` and the ``Sleep`` of a test. ``sleep(ms)`` registers a waiter and awaits
it; ``advance`` moves time and wakes the waiters whose deadline has passed. Tests that need concurrent waiters
use this; ``fetch_fakes.advancing_sleep`` is only for tests with a single waiter.

``settle`` also waits, in real time, for every job handed to a worker thread (``asyncio.to_thread`` and
``loop.run_in_executor``: the SQLite reads and writes of the code under test). Without that, simulated time runs
ahead of work that needs real time, and a slower machine sees "nothing is waiting" or an expired grace (F-19).
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

NS_PER_MS = 1_000_000
YIELDS = 100  # event-loop hops granted after every wake-up so awakened tasks run until they block again
MAX_WAIT_YIELDS = 1_000


class _ThreadJobs:
    """The jobs a loop has handed to an executor and that have not completed."""

    def __init__(self) -> None:
        self.pending = 0


def _thread_jobs(loop: asyncio.AbstractEventLoop) -> _ThreadJobs:
    """Count the executor jobs of ``loop`` from now on (once per loop; a job started before the first call is not counted)."""
    existing: _ThreadJobs | None = getattr(loop, "_manual_time_jobs", None)
    if existing is not None:
        return existing
    jobs = _ThreadJobs()
    original: Callable[..., Any] = loop.run_in_executor

    def run_in_executor(executor: Any, func: Callable[..., Any], *args: Any) -> "asyncio.Future[Any]":
        future: asyncio.Future[Any] = original(executor, func, *args)
        jobs.pending += 1
        future.add_done_callback(lambda _: setattr(jobs, "pending", jobs.pending - 1))
        return future

    loop.run_in_executor = run_in_executor  # type: ignore[assignment]
    loop._manual_time_jobs = jobs  # type: ignore[attr-defined]
    return jobs


def _finish(jobs: _ThreadJobs) -> None:
    jobs.pending -= 1


@dataclass(order=True)
class _Waiter:
    deadline_ns: int
    seq: int
    future: "asyncio.Future[None]" = field(compare=False)


class ManualTime:
    def __init__(
        self,
        wall_start_ms: int = 1_792_677_036_000,
        mono_start_ns: int = 18_053_000_000_000,
        *,
        wait_for_threads: bool = True,
    ) -> None:
        self._wait_for_threads = wait_for_threads  # False: a test that blocks a worker thread on purpose
        self._mono_ns = mono_start_ns
        self._wall_ms = wall_start_ms
        self._waiters: list[_Waiter] = []
        self._seq = 0
        try:
            _thread_jobs(asyncio.get_running_loop())  # count worker-thread jobs from the start, when a loop runs
        except RuntimeError:
            pass  # created outside a loop: counting starts at the first sleep or settle

    def monotonic_ns(self) -> int:
        return self._mono_ns

    def wall_ms(self) -> int:
        return self._wall_ms

    @property
    def waiting(self) -> list[int]:
        """Sorted deadlines (monotonic ns) of the registered waiters."""
        return sorted(w.deadline_ns for w in self._waiters)

    async def sleep(self, ms: int) -> None:
        _thread_jobs(asyncio.get_running_loop())  # from the first sleep on, settle can see worker-thread jobs
        waiter = _Waiter(self._mono_ns + ms * NS_PER_MS, self._seq, asyncio.get_running_loop().create_future())
        self._seq += 1
        self._waiters.append(waiter)
        try:
            await waiter.future
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def track_executor(self, executor: ThreadPoolExecutor) -> None:
        """Make ``settle`` wait for the jobs submitted to ``executor`` with ``executor.submit``.

        ``settle`` sees only ``loop.run_in_executor`` jobs; ``AsyncOpenInterestWriter`` submits to its own executor and
        wraps the future with ``asyncio.wrap_future``, which it cannot see (the F-19 race again). Call it from inside the
        running loop, once per executor, before the first submission.
        """
        loop = asyncio.get_running_loop()
        jobs = _thread_jobs(loop)
        original = executor.submit

        def submit(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
            future = original(function, *args, **kwargs)
            jobs.pending += 1
            future.add_done_callback(lambda _: loop.call_soon_threadsafe(_finish, jobs))
            return future

        executor.submit = submit  # type: ignore[method-assign]

    async def settle(self) -> None:
        jobs = _thread_jobs(asyncio.get_running_loop())
        while True:
            for _ in range(YIELDS):
                await asyncio.sleep(0)
            if jobs.pending == 0 or not self._wait_for_threads:
                return
            await asyncio.sleep(0.001)  # real time: a worker thread is still running

    async def until_waiting(self, count: int) -> None:
        for _ in range(MAX_WAIT_YIELDS):
            if len(self._waiters) == count:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"expected {count} waiters, have {len(self._waiters)}")

    def _move_to(self, target_ns: int) -> None:
        self._wall_ms += (target_ns - self._mono_ns) // NS_PER_MS
        self._mono_ns = target_ns

    async def advance_ns(self, ns: int, *, late: bool = False) -> None:
        """Move time forward by ``ns``. By default every due waiter wakes at its own deadline, in order; with
        ``late=True`` time jumps first and all due waiters wake at the final instant (a delayed wake-up)."""
        target = self._mono_ns + ns
        if late:
            self._move_to(target)
        while True:
            due = sorted(w for w in self._waiters if w.deadline_ns <= target and not w.future.done())
            if not due:
                break
            waiter = due[0]
            if not late:
                self._move_to(max(self._mono_ns, waiter.deadline_ns))
            waiter.future.set_result(None)
            await self.settle()
        self._move_to(target)
        await self.settle()

    async def advance(self, ms: int, *, late: bool = False) -> None:
        await self.advance_ns(ms * NS_PER_MS, late=late)


async def assert_no_leftover_tasks() -> None:
    """Fail when a task other than the caller's is still alive after letting the loop settle."""
    for _ in range(YIELDS):
        await asyncio.sleep(0)
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not leftover, f"leftover tasks: {leftover}"
