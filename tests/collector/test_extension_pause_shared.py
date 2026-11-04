"""Open interest and funding share one ``ExtensionPause`` per process (amendment A-3 to ADR-0003, ADR-0004 P-9).

Both real adapters run on their fake exchanges with ONE pause and one fake clock. A 429 or 418 answered to either
dataset stops the other without a request, and neither dataset's own limiter is touched by it.
"""

import asyncio
from collections.abc import Callable, Coroutine

import pytest

from quasar_market_data.collector.binance_usdm_funding_rate import BinanceUsdmFundingRateSource
from quasar_market_data.collector.binance_usdm_open_interest import BinanceUsdmOpenInterestSource
from quasar_market_data.collector.extension_pause import ExtensionPause, ExtensionPaused, ExtensionStopped
from quasar_market_data.collector.funding_rate_limiter import RequestClass
from tests.collector.manual_time import ManualTime
from tests.collector.test_binance_usdm_funding_rate import CountingLimiter, ddos
from tests.collector.test_binance_usdm_funding_rate import FakeExchange as FakeFundingExchange
from tests.collector.test_binance_usdm_funding_rate import hist as funding_hist
from tests.collector.test_binance_usdm_open_interest import CountingBudget
from tests.collector.test_binance_usdm_open_interest import FakeExchange as FakeOiExchange
from tests.collector.test_binance_usdm_open_interest import hist as oi_hist

S = 1000
BAN_BODY = '{"code":-1003,"msg":"Way too many requests; IP banned."}'


def run(scenario: Callable[[], Coroutine[None, None, None]]) -> None:
    asyncio.run(scenario())


class Rig:
    """One clock, one pause, two real adapters, each with its own limiter and fake exchange."""

    def __init__(self) -> None:
        self.t = ManualTime()
        self.pause = ExtensionPause(self.t)
        self.oi_exchange = FakeOiExchange(self.t)
        self.funding_exchange = FakeFundingExchange(self.t)
        self.budget = CountingBudget(400, self.t)
        self.limiter = CountingLimiter(150, self.t)
        self.oi: BinanceUsdmOpenInterestSource | None = None
        self.funding: BinanceUsdmFundingRateSource | None = None

    async def open(self) -> tuple[BinanceUsdmOpenInterestSource, BinanceUsdmFundingRateSource]:
        self.oi = await BinanceUsdmOpenInterestSource.open(self.budget, self.pause, self.t, self.oi_exchange)
        self.funding = await BinanceUsdmFundingRateSource.open(self.limiter, self.pause, self.t, self.funding_exchange)
        return self.oi, self.funding

    def counters(self) -> tuple[int, int, int, int]:
        return (
            len(self.oi_exchange.calls),
            len(self.funding_exchange.calls),
            self.budget.acquired,
            self.limiter.acquired,
        )


def test_a_429_answered_to_funding_stops_open_interest_without_a_request_or_a_budget_start() -> None:
    async def scenario() -> None:
        rig = Rig()
        oi, funding = await rig.open()
        await oi_hist(oi)  # open interest works before the pause
        rig.funding_exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused) as raised:
            await funding_hist(funding)
        assert raised.value.remaining_ms == 300 * S
        before = rig.counters()
        for _ in range(3):
            with pytest.raises(ExtensionPaused):
                await oi_hist(oi)
            with pytest.raises(ExtensionPaused):
                await oi.fetch_server_time()
        assert rig.counters() == before  # nothing reached the exchange, no budget start was counted
        await rig.t.advance(300 * S)
        await oi_hist(oi)  # the pause is over for both

    run(scenario)


def test_a_429_answered_to_open_interest_stops_funding_without_a_request_or_a_limiter_start() -> None:
    async def scenario() -> None:
        rig = Rig()
        oi, funding = await rig.open()
        await funding_hist(funding)
        rig.oi_exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused) as raised:
            await oi_hist(oi)
        assert raised.value.remaining_ms == 300 * S
        before = rig.counters()
        for _ in range(3):
            with pytest.raises(ExtensionPaused):
                await funding_hist(funding)
            with pytest.raises(ExtensionPaused):
                await funding.fetch_server_time()
        assert rig.counters() == before
        await rig.t.advance(300 * S)
        await funding_hist(funding)

    run(scenario)


def test_a_429_does_not_change_the_other_datasets_own_limiter_state() -> None:
    async def scenario() -> None:
        rig = Rig()
        oi, funding = await rig.open()
        await funding_hist(funding, request_class=RequestClass.R)
        starts_before = rig.limiter.acquired
        rig.oi_exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused):
            await oi_hist(oi)
        assert rig.limiter.acquired == starts_before  # the funding limiter saw nothing of the open interest 429
        # and the reverse: the open interest budget counts only open interest starts
        budget_before = rig.budget.acquired
        await rig.t.advance(300 * S)
        rig.funding_exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused):
            await funding_hist(funding)
        assert rig.budget.acquired == budget_before

    run(scenario)


@pytest.mark.parametrize("first", ["funding", "open_interest"])
def test_a_418_on_either_dataset_stops_both_for_the_run(first: str) -> None:
    async def scenario() -> None:
        rig = Rig()
        oi, funding = await rig.open()
        if first == "funding":
            rig.funding_exchange.errors = [ddos(418, BAN_BODY)]
            with pytest.raises(ExtensionStopped):
                await funding_hist(funding)
        else:
            rig.oi_exchange.errors = [ddos(418, BAN_BODY)]
            with pytest.raises(ExtensionStopped):
                await oi_hist(oi)
        before = rig.counters()
        await rig.t.advance(2 * 3600 * S)  # the ban does not expire
        with pytest.raises(ExtensionStopped):
            await oi_hist(oi)
        with pytest.raises(ExtensionStopped):
            await oi.fetch_server_time()
        with pytest.raises(ExtensionStopped):
            await funding_hist(funding)
        with pytest.raises(ExtensionStopped):
            await funding.fetch_server_time()
        assert rig.counters() == before

    run(scenario)


def test_a_request_that_waited_for_its_grant_while_the_other_dataset_got_a_429_sends_nothing() -> None:
    async def scenario() -> None:
        rig = Rig()
        oi, funding = await rig.open()
        rig.oi_exchange.hold = True  # the first open interest call stays in flight
        first = asyncio.ensure_future(oi_hist(oi))
        await rig.t.settle()
        rig.funding_exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused):
            await funding_hist(funding)  # the pause is now in force for open interest too
        rig.oi_exchange.hold = False
        rig.oi_exchange.release.set()
        await first  # in flight before the pause: it completes, but it neither shortens the pause nor resets it
        calls = len(rig.oi_exchange.calls)
        with pytest.raises(ExtensionPaused):
            await oi_hist(oi)
        assert len(rig.oi_exchange.calls) == calls

    run(scenario)
