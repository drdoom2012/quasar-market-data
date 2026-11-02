import asyncio

import pytest

from quasar_market_data.collector.candle_write_activity import CandleWriteActivity


async def spin(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def test_idle_at_start_and_wait_returns_at_once() -> None:
    async def scenario() -> None:
        activity = CandleWriteActivity()
        assert not activity.active
        waiter = asyncio.ensure_future(activity.wait_idle())
        await spin(1)
        assert waiter.done()

    asyncio.run(scenario())


def test_nested_writes_stay_active_until_the_outer_exit() -> None:
    async def scenario() -> None:
        activity = CandleWriteActivity()
        with activity.writing():
            with activity.writing():
                assert activity.active
            assert activity.active
        assert not activity.active

    asyncio.run(scenario())


def test_exception_still_decrements() -> None:
    async def scenario() -> None:
        activity = CandleWriteActivity()
        with pytest.raises(RuntimeError), activity.writing():
            raise RuntimeError("boom")
        assert not activity.active

    asyncio.run(scenario())


def test_waiters_resume_only_after_the_last_exit() -> None:
    async def scenario() -> None:
        activity = CandleWriteActivity()
        outer = activity.writing()
        inner = activity.writing()
        outer.__enter__()
        inner.__enter__()
        first = asyncio.ensure_future(activity.wait_idle())
        second = asyncio.ensure_future(activity.wait_idle())
        await spin()
        assert not first.done() and not second.done()
        inner.__exit__(None, None, None)
        await spin()
        assert not first.done() and not second.done()
        outer.__exit__(None, None, None)
        await spin()
        assert first.done() and second.done()

    asyncio.run(scenario())


def test_there_is_no_public_decrement() -> None:
    public = {name for name in dir(CandleWriteActivity) if not name.startswith("_")}
    assert public == {"active", "wait_idle", "writing"}
