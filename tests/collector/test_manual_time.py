import asyncio
import threading
import time

from tests.collector.manual_time import ManualTime


def test_settle_waits_for_a_job_running_on_a_worker_thread() -> None:
    """Simulated time must not run ahead of real thread work (SQLite reads and writes run on worker threads): a
    fixed number of event-loop hops is not enough on a slower machine (F-19)."""

    async def scenario() -> None:
        t = ManualTime()
        finished = threading.Event()

        def job() -> None:
            time.sleep(0.2)
            finished.set()

        work = asyncio.ensure_future(asyncio.to_thread(job))
        await asyncio.sleep(0)  # the job is handed to the worker thread
        await t.settle()
        assert finished.is_set(), "settle returned while a worker thread was still running"
        await work

    asyncio.run(scenario())


def test_settle_does_not_wait_when_no_thread_job_runs() -> None:
    async def scenario() -> None:
        t = ManualTime()
        started = time.monotonic()
        for _ in range(50):
            await t.settle()
        assert time.monotonic() - started < 1.0

    asyncio.run(scenario())


def test_settle_can_be_told_not_to_wait_for_a_thread_a_test_blocks_on_purpose() -> None:
    async def scenario() -> None:
        t = ManualTime(wait_for_threads=False)
        release = threading.Event()
        work = asyncio.ensure_future(asyncio.to_thread(release.wait, 5))
        await asyncio.sleep(0)
        started = time.monotonic()
        await t.settle()
        assert time.monotonic() - started < 1.0 and not work.done()
        release.set()
        await work

    asyncio.run(scenario())


def test_settle_waits_for_a_job_submitted_to_a_tracked_executor() -> None:
    """``AsyncOpenInterestWriter`` submits to its own executor and wraps the future: not a ``run_in_executor`` job."""
    from concurrent.futures import ThreadPoolExecutor

    async def scenario() -> None:
        t = ManualTime()
        executor = ThreadPoolExecutor(max_workers=1)
        t.track_executor(executor)
        finished = threading.Event()

        def job() -> None:
            time.sleep(0.2)
            finished.set()

        work = asyncio.wrap_future(executor.submit(job))
        await asyncio.sleep(0)
        await t.settle()
        assert finished.is_set(), "settle returned while a tracked executor job was still running"
        await work
        await t.settle()  # the counter is back to zero: this returns at once
        executor.shutdown()

    asyncio.run(scenario())


def test_an_untracked_executor_is_not_waited_for() -> None:
    from concurrent.futures import ThreadPoolExecutor

    async def scenario() -> None:
        t = ManualTime()
        executor = ThreadPoolExecutor(max_workers=1)
        release = threading.Event()
        work = asyncio.wrap_future(executor.submit(release.wait, 5))
        await t.settle()  # returns although the thread is still blocked: the documented limitation
        release.set()
        await work
        executor.shutdown()

    asyncio.run(scenario())
