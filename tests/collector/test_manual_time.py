import asyncio
import threading
import time
from typing import Any, cast

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


def test_settle_lets_the_task_that_awaits_a_job_finished_during_the_hops_run_until_it_blocks() -> None:
    """A job that ends near the end of the hops leaves ``pending`` at zero while the task that awaits it has only a few
    hops left: settle must hop again, or time moves on before that task has run (a request started at the next step
    instead of at its own instant, on a slower CI machine)."""
    from concurrent.futures import ThreadPoolExecutor

    async def scenario() -> None:
        t = ManualTime()
        executor = ThreadPoolExecutor(max_workers=1)
        t.track_executor(executor)
        release = threading.Event()
        job = executor.submit(release.wait, 5)
        work = asyncio.wrap_future(job)
        after: list[int] = []

        def block_until_done() -> None:
            """Block the event loop on purpose until the job's completion callbacks are queued on it."""
            while not job.done():
                time.sleep(0.001)
            time.sleep(0.05)

        async def awaiter() -> None:
            await work
            for hop in range(30):  # the work the task does after the job, before it blocks again
                await asyncio.sleep(0)
                after.append(hop)

        async def releaser() -> None:
            for _ in range(80):  # late in the hops of the settle below
                await asyncio.sleep(0)
            release.set()
            block_until_done()  # the completion callbacks are queued before the next hop

        tasks = [asyncio.ensure_future(awaiter()), asyncio.ensure_future(releaser())]
        await asyncio.sleep(0)
        await t.settle()
        assert after == list(range(30)), f"settle returned after {len(after)} of the 30 hops"
        await asyncio.gather(*tasks)
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


def test_idle_settle_runs_a_woken_chain_to_its_end_and_still_waits_for_a_worker_thread() -> None:
    async def scenario() -> None:
        t = ManualTime(idle_settle=True)
        reached: list[int] = []

        async def chain() -> None:
            for hop in range(40):  # a long chain of loop hops, as a dispatcher starting a group of requests
                await asyncio.sleep(0)
                reached.append(hop)

        finished = threading.Event()

        def job() -> None:
            time.sleep(0.2)
            finished.set()

        task = asyncio.ensure_future(chain())
        work = asyncio.ensure_future(asyncio.to_thread(job))
        await asyncio.sleep(0)
        await t.settle()
        assert reached == list(range(40)) and task.done()
        assert finished.is_set(), "settle returned while a worker thread was still running"
        await work

    asyncio.run(scenario())


def test_idle_settle_takes_fewer_hops_than_the_default_when_nothing_is_runnable() -> None:
    async def hops(idle: bool) -> int:
        count = 0
        loop = asyncio.get_running_loop()
        original = loop.call_soon

        def counting(*args: object, **kwargs: object) -> object:
            nonlocal count
            count += 1
            return original(*args, **kwargs)  # type: ignore[arg-type]

        loop.call_soon = cast(Any, counting)  # type: ignore[method-assign]
        try:
            await ManualTime(idle_settle=idle).settle()
        finally:
            loop.call_soon = original  # type: ignore[method-assign]
        return count

    async def scenario() -> None:
        assert await hops(True) < await hops(False)

    asyncio.run(scenario())


def test_the_event_loop_still_has_the_ready_queue_idle_settle_looks_at() -> None:
    """If a Python upgrade removes ``_ready``, ``idle_settle`` silently falls back to all hops: fail loudly instead."""
    loop = asyncio.new_event_loop()
    try:
        assert hasattr(loop, "_ready") and len(loop._ready) == 0
    finally:
        loop.close()
