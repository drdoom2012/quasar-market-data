import asyncio

import pytest

from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.shared_server_time import REFRESH_RETRY_MS, SharedServerTime
from quasar_market_data.domain.server_time import DEFAULT_TIME_TRUST, ServerTimeObservation
from tests.collector.fetch_fakes import FakeClock, FakeSource

REFRESH_MS = 30_000


def shared(clock: FakeClock) -> SharedServerTime:
    return SharedServerTime(clock, DEFAULT_TIME_TRUST, REFRESH_MS)


def source(clock: FakeClock, **kwargs: object) -> FakeSource:
    return FakeSource(clock, [], **kwargs)  # type: ignore[arg-type]


class YieldingSource(FakeSource):
    """Its time request suspends once, so concurrent callers really overlap."""

    async def fetch_server_time(self) -> ServerTimeObservation:
        await asyncio.sleep(0)
        return await super().fetch_server_time()


class BlockedSource(FakeSource):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock, [])
        self.entered = asyncio.Event()

    async def fetch_server_time(self) -> ServerTimeObservation:
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_the_first_call_fetches_and_a_second_call_within_the_refresh_age_reuses_it() -> None:
    clock = FakeClock()
    src, provider = source(clock, rtt_ms=100), shared(clock)

    async def scenario() -> None:
        first = await provider.observation(src)
        second = await provider.observation(src)
        assert first is not None and second is first
        assert src.time_calls == 1

    asyncio.run(scenario())


def test_an_observation_older_than_the_refresh_age_is_refreshed() -> None:
    clock = FakeClock()
    src, provider = source(clock, rtt_ms=100), shared(clock)

    async def scenario() -> None:
        await provider.observation(src)
        clock.advance(REFRESH_MS)  # exactly the refresh age: still usable
        await provider.observation(src)
        assert src.time_calls == 1
        clock.advance(1)
        await provider.observation(src)
        assert src.time_calls == 2

    asyncio.run(scenario())


def test_an_observation_the_trust_gate_rejects_is_returned_but_never_served_twice() -> None:
    clock = FakeClock()
    src, provider = source(clock, rtt_ms=2500), shared(clock)  # above max_rtt_ms (2000)

    async def scenario() -> None:
        first = await provider.observation(src)
        second = await provider.observation(src)
        assert first is not None and second is not None and second is not first
        assert src.time_calls == 2

    asyncio.run(scenario())


def test_a_wall_clock_step_makes_the_cached_observation_unusable() -> None:
    clock = FakeClock()
    src, provider = source(clock, rtt_ms=100), shared(clock)

    async def scenario() -> None:
        await provider.observation(src)
        clock.advance_wall(5_000)  # beyond max_clock_divergence_ms (100)
        await provider.observation(src)
        assert src.time_calls == 2

    asyncio.run(scenario())


def test_concurrent_callers_share_one_request() -> None:
    clock = FakeClock()
    src, provider = YieldingSource(clock, []), shared(clock)

    async def scenario() -> None:
        results = await asyncio.gather(*(provider.observation(src) for _ in range(5)))
        assert src.time_calls == 1
        assert all(r is results[0] for r in results) and results[0] is not None

    asyncio.run(scenario())


def test_a_failed_refresh_falls_back_to_the_previous_observation_and_never_raises() -> None:
    clock = FakeClock()
    src, provider = source(clock, rtt_ms=100), shared(clock)

    async def scenario() -> None:
        first = await provider.observation(src)
        clock.advance(REFRESH_MS + 1)
        src.time_error = SourceError("down")
        assert await provider.observation(src) is first

    asyncio.run(scenario())


def test_a_failed_first_request_returns_none() -> None:
    clock = FakeClock()
    src, provider = source(clock, time_error=SourceError("down")), shared(clock)
    assert asyncio.run(provider.observation(src)) is None


def test_a_cancelled_refresh_releases_the_lock() -> None:
    clock = FakeClock()
    blocked, working, provider = BlockedSource(clock), source(clock, rtt_ms=100), shared(clock)

    async def scenario() -> None:
        task = asyncio.create_task(provider.observation(blocked))
        await blocked.entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.wait_for(provider.observation(working), timeout=1) is not None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("value", "error"), [(True, TypeError), (1.5, TypeError), (0, ValueError), (60_000, ValueError)]
)
def test_refresh_age_must_be_an_int_below_max_age(value: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        SharedServerTime(FakeClock(), DEFAULT_TIME_TRUST, value)  # type: ignore[arg-type]


def test_a_still_valid_observation_is_served_while_a_refresh_is_in_flight() -> None:
    clock = FakeClock()
    working, blocked, provider = source(clock, rtt_ms=100), BlockedSource(clock), shared(clock)

    async def scenario() -> None:
        first = await provider.observation(working)
        clock.advance(35_000)  # past the refresh age, far from the age gate (60 s)
        refresher = asyncio.create_task(provider.observation(blocked))  # a hanging time endpoint
        await blocked.entered.wait()
        served = await asyncio.wait_for(provider.observation(working), timeout=1)
        assert served is first and working.time_calls == 1  # not queued behind the hanging refresh
        refresher.cancel()
        await asyncio.gather(refresher, return_exceptions=True)

    asyncio.run(scenario())


def test_a_failed_refresh_is_not_retried_at_once() -> None:
    clock = FakeClock()
    src, provider = source(clock, rtt_ms=100), shared(clock)

    async def scenario() -> None:
        first = await provider.observation(src)
        clock.advance(REFRESH_MS + 1)
        src.time_error = SourceError("down")
        assert await provider.observation(src) is first and src.time_calls == 2  # the failed refresh
        assert await provider.observation(src) is first and src.time_calls == 2  # backing off
        clock.advance(REFRESH_RETRY_MS - 1)
        await provider.observation(src)
        assert src.time_calls == 2
        clock.advance(1)
        await provider.observation(src)
        assert src.time_calls == 3

    asyncio.run(scenario())


def test_a_failed_first_request_is_not_repeated_at_once() -> None:
    clock = FakeClock()
    src, provider = source(clock, time_error=SourceError("down")), shared(clock)

    async def scenario() -> None:
        assert await provider.observation(src) is None
        assert await provider.observation(src) is None
        assert src.time_calls == 1

    asyncio.run(scenario())
