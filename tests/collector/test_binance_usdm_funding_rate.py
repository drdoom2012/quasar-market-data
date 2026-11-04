import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import ccxt
import ccxt.async_support
import pytest

from quasar_market_data.collector import binance_usdm_funding_rate as module
from quasar_market_data.collector.binance_funding_rate import parse_funding_rate_history
from quasar_market_data.collector.binance_usdm import BinanceUsdmSource
from quasar_market_data.collector.binance_usdm_funding_rate import (
    MAX_IN_FLIGHT,
    BinanceUsdmFundingRateSource,
    FundingRateRequestRefused,
)
from quasar_market_data.collector.extension_pause import ExtensionPause, ExtensionPaused, ExtensionStopped
from quasar_market_data.collector.funding_rate_limiter import WINDOW_MS, FundingRateLimiter, RequestClass
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.funding_rate import FUNDING_RATE_PAGE_LIMIT
from quasar_market_data.domain.funding_rate_span import Interval, merge_span, page_coverage
from quasar_market_data.domain.timeframe import Timeframe
from tests.collector.manual_time import NS_PER_MS, ManualTime, assert_no_leftover_tasks
from tests.collector.test_binance_usdm import FakeExchange as FakeCandleExchange
from tests.collector.test_binance_usdm import usdm_markets

SYMBOL = "BTC/USDT:USDT"
MARKET_ID = "BTCUSDT"
P5 = Timeframe("5m")
HOUR = 3_600_000
AS_OF = 1_793_836_800_000 + 3  # a settlement time, 3 ms after a whole hour
FAPI_URL = "https://fapi.binance.com/fapi/v1"
S = 1000
R, C, H = RequestClass.R, RequestClass.C, RequestClass.H


def run(scenario: Callable[[], Coroutine[None, None, None]]) -> None:
    asyncio.run(scenario())


def funding_rows(
    times: list[int], market_id: str = MARKET_ID, rate_types: dict[int, list[str]] | None = None
) -> list[dict[str, Any]]:
    rows = []
    for time_ms in times:
        for rate_type in (rate_types or {}).get(time_ms, ["Regular"]):
            rows.append(
                {
                    "symbol": market_id,
                    "fundingTime": time_ms,
                    "fundingRate": "0.00010000",
                    "markPrice": "67123.45000000",
                    "rateType": rate_type,
                }
            )
    return rows


class FakeExchange:
    id = "binanceusdm"

    def __init__(self, clock: ManualTime | None = None) -> None:
        self.markets: dict[str, dict[str, Any]] | None = usdm_markets()
        self.urls: dict[str, Any] = {"api": {"fapiPublic": FAPI_URL}}
        self.last_response_headers: dict[str, str] = {}
        self.dataset: list[dict[str, Any]] = funding_rows([AS_OF - i * HOUR for i in range(100, -1, -1)])
        self.response: object | None = None  # None: answer like Binance from the dataset (oldest rows first)
        self.time_response: object = {"serverTime": AS_OF}
        self.errors: list[BaseException] = []  # the next calls raise these, in order
        self.error_always: BaseException | None = None
        self.hold = False  # calls wait on ``release`` until it is set
        self.release = asyncio.Event()
        self.calls: list[tuple[str, Any]] = []
        self.instants: list[int] = []
        self.in_flight = 0
        self.peak = 0
        self.closed = False
        self._clock = clock

    async def load_markets(self) -> None:
        self.calls.append(("load_markets", None))

    async def _call(self, name: str, params: Any, answer: Callable[[], object]) -> object:
        self.calls.append((name, params))
        if self._clock is not None:
            self.instants.append(self._clock.monotonic_ns())
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.hold:
                await self.release.wait()
            if self.errors:
                raise self.errors.pop(0)
            if self.error_always is not None:
                raise self.error_always
            return answer()
        finally:
            self.in_flight -= 1

    async def fapiPublicGetFundingRate(self, params: dict[str, Any]) -> object:
        def answer() -> object:
            if self.response is not None:
                return self.response
            inside = [
                row
                for row in self.dataset
                if row["symbol"] == params["symbol"] and params["startTime"] <= row["fundingTime"] <= params["endTime"]
            ]
            return inside[: params["limit"]]  # Binance returns the OLDEST limit rows, ascending (P-5 rule 1)

        return await self._call("hist", params, answer)

    async def fapiPublicGetTime(self) -> object:
        return await self._call("time", None, lambda: self.time_response)

    async def close(self) -> None:
        self.closed = True

    def called(self, name: str) -> int:
        return sum(1 for call_name, _ in self.calls if call_name == name)


class CountingLimiter(FundingRateLimiter):
    def __init__(self, budget_per_5min: int, t: ManualTime) -> None:
        super().__init__(budget_per_5min, t, t.sleep)
        self.acquired = 0

    @asynccontextmanager
    async def grant(self, request_class: RequestClass) -> AsyncIterator[None]:
        async with super().grant(request_class):
            self.acquired += 1
            yield


class Rig:
    def __init__(self, b: int = 150) -> None:
        self.t = ManualTime()
        self.exchange = FakeExchange(self.t)
        self.limiter = CountingLimiter(b, self.t)
        self.pause = ExtensionPause(self.t)
        self.source: BinanceUsdmFundingRateSource | None = None

    async def open(self) -> BinanceUsdmFundingRateSource:
        self.source = await BinanceUsdmFundingRateSource.open(self.limiter, self.pause, self.t, self.exchange)
        return self.source


def ddos(status: int, body: str = '{"code":-1003,"msg":"Too many requests."}') -> ccxt.DDoSProtection:
    return ccxt.DDoSProtection(f"binanceusdm {status} Too Many Requests {body}")


async def hist(
    source: BinanceUsdmFundingRateSource,
    start: int = AS_OF - 24 * HOUR,
    end: int = AS_OF,
    request_class: RequestClass = R,
) -> list[object]:
    return await source.fetch_funding_rate_history(SYMBOL, start, end, request_class=request_class)


def test_request_carries_the_market_id_bounds_and_limit_1000_and_returns_the_raw_list() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rows = await hist(source)
        assert rig.exchange.calls[-1] == (
            "hist",
            {"symbol": MARKET_ID, "startTime": AS_OF - 24 * HOUR, "endTime": AS_OF, "limit": 1000},
        )
        assert len(rows) == 25 and isinstance(rows[0]["fundingRate"], str)  # type: ignore[index]  # untouched text
        response = funding_rows([AS_OF - HOUR, AS_OF])
        rig.exchange.response = response
        assert await hist(source) is response

    run(scenario)


def test_start_time_is_always_sent_and_limit_is_always_1000() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        for start, end in ((AS_OF, AS_OF), (AS_OF - HOUR, AS_OF), (946_684_800_000, AS_OF)):
            for request_class in (R, C, H):
                await hist(source, start, end, request_class)
        params = [call[1] for call in rig.exchange.calls if call[0] == "hist"]
        assert len(params) == 9
        assert all(set(p) == {"symbol", "startTime", "endTime", "limit"} and p["limit"] == 1000 for p in params)

    run(scenario)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (AS_OF, AS_OF - 1, FundingRateRequestRefused),
        (AS_OF, AS_OF - HOUR, FundingRateRequestRefused),
        (1, AS_OF, FundingRateRequestRefused),
        (AS_OF, 4_102_444_800_001, FundingRateRequestRefused),
        (True, AS_OF, TypeError),
        (AS_OF, 1.5, TypeError),
        ("1", AS_OF, TypeError),
    ],
    ids=["end_before_start_by_1", "end_before_start", "start_out_of_range", "end_out_of_range", "bool", "float", "str"],
)
def test_refusals_use_nothing(start: Any, end: Any, expected: type[Exception]) -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        checks: list[str] = []
        original = rig.pause.check

        def spy() -> None:
            checks.append("check")
            original()

        rig.pause.check = spy  # type: ignore[method-assign]
        with pytest.raises(expected):
            await source.fetch_funding_rate_history(SYMBOL, start, end, request_class=R)
        assert rig.exchange.called("hist") == 0
        assert rig.limiter.starts_in_window(rig.t.monotonic_ns()) == 0 and rig.limiter.acquired == 0
        assert checks == []
        await asyncio.gather(*(hist(source) for _ in range(2)))  # no slot was taken: both proceed

    run(scenario)


def test_a_wrong_request_class_is_a_type_error_and_sends_nothing() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        with pytest.raises(TypeError):
            await source.fetch_funding_rate_history(SYMBOL, AS_OF - HOUR, AS_OF, request_class="R")  # type: ignore[arg-type]
        assert rig.exchange.called("hist") == 0

    run(scenario)


@pytest.mark.parametrize(
    "change",
    [
        {"swap": False},
        {"linear": False},
        {"settle": "USD"},
        {"quote": "USD"},
        {"expiry": 1893456000000},
        {"id": None},
        {"id": ""},
        {"active": False},
        {"active": None},
        {"active": "true"},
    ],
    ids=["swap", "linear", "settle", "quote", "expiry", "no_id", "empty_id", "inactive", "active_none", "active_str"],
)
def test_a_symbol_that_is_not_an_active_usdt_perpetual_is_never_requested(change: dict[str, Any]) -> None:
    async def scenario() -> None:
        rig = Rig()
        rig.exchange.markets = {SYMBOL: {**usdm_markets()[SYMBOL], **change}}
        source = await rig.open()
        with pytest.raises(FundingRateRequestRefused):
            await hist(source)
        with pytest.raises(FundingRateRequestRefused):
            source.market_id(SYMBOL)
        assert rig.exchange.called("hist") == 0 and rig.limiter.acquired == 0

    run(scenario)


def test_a_missing_active_key_and_unknown_symbols_are_refused() -> None:
    async def scenario() -> None:
        rig = Rig()
        market = dict(usdm_markets()[SYMBOL])
        del market["active"]
        rig.exchange.markets = {SYMBOL: market}
        source = await rig.open()
        with pytest.raises(FundingRateRequestRefused):
            await hist(source)
        with pytest.raises(FundingRateRequestRefused, match="NOPE/USDT:USDT"):
            await source.fetch_funding_rate_history("NOPE/USDT:USDT", AS_OF - HOUR, AS_OF, request_class=R)
        assert rig.exchange.called("hist") == 0

    run(scenario)


def test_a_real_inactive_symbol_of_the_fixture_is_refused_and_an_active_one_is_requested() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        with pytest.raises(FundingRateRequestRefused, match="not active"):
            await source.fetch_funding_rate_history("1000WHY/USDT:USDT", AS_OF - HOUR, AS_OF, request_class=R)
        assert source.market_id(SYMBOL) == MARKET_ID
        await hist(source)
        assert rig.exchange.called("hist") == 1

    run(scenario)


def test_server_time_comes_from_the_funding_client_and_counts_in_r() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        observation = await source.fetch_server_time()
        assert observation.server_time_ms == AS_OF
        assert rig.exchange.called("time") == 1
        now = rig.t.monotonic_ns()
        assert (rig.limiter.starts_in_window(now, R), rig.limiter.starts_in_window(now, C)) == (1, 0)

    run(scenario)


def test_history_class_counts_in_its_group() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        for request_class in (R, C, H, H):
            await hist(source, request_class=request_class)
        now = rig.t.monotonic_ns()
        assert rig.limiter.starts_in_window(now, R) == 1
        assert rig.limiter.starts_in_window(now, C) == rig.limiter.starts_in_window(now, H) == 3

    run(scenario)


def test_open_creates_its_own_client_each_time_and_never_the_candle_sources() -> None:
    created: list[FakeExchange] = []

    def factory() -> FakeExchange:
        created.append(FakeExchange())
        return created[-1]

    async def scenario() -> None:
        t = ManualTime()
        candle = await BinanceUsdmSource.open()  # patched factory: the candle source's own client
        first = await BinanceUsdmFundingRateSource.open(FundingRateLimiter(150, t, t.sleep), ExtensionPause(t), t)
        second = await BinanceUsdmFundingRateSource.open(FundingRateLimiter(150, t, t.sleep), ExtensionPause(t), t)
        assert len(created) == 3
        assert len({id(client) for client in created}) == 3
        for source in (candle, first, second):
            await source.close()
        assert all(client.closed for client in created)

    original = ccxt.async_support.binanceusdm
    ccxt.async_support.binanceusdm = factory
    try:
        run(scenario)
    finally:
        ccxt.async_support.binanceusdm = original


def test_a_missing_fapi_public_url_is_a_source_error() -> None:
    async def scenario() -> None:
        rig = Rig()
        rig.exchange.urls = {"api": {}}
        with pytest.raises(SourceError):
            await rig.open()
        assert rig.exchange.closed

    run(scenario)


def test_a_1130_answer_is_a_source_error_not_a_crash_and_the_pause_is_unchanged() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.errors = [
            ccxt.BadRequest('binanceusdm {"code":-1130,"msg":"Invalid data sent for parameter \'startTime\'."}')
        ]
        with pytest.raises(SourceError, match="1130") as raised:
            await hist(source)
        assert not isinstance(raised.value, (FundingRateRequestRefused, ExtensionPaused, ExtensionStopped))
        rig.pause.check()  # neither paused nor stopped
        assert rig.limiter.acquired == 1  # the request was sent
        await hist(source)  # and the source keeps working

    run(scenario)


def test_a_saturated_r_group_does_not_hold_the_slots_against_c_and_the_reverse() -> None:
    async def scenario() -> None:
        for saturated, free in ((R, C), (C, R)):
            rig = Rig(b=50)
            source = await rig.open()
            for _ in range(25):
                await hist(source, request_class=saturated)
            waiting = [asyncio.create_task(hist(source, request_class=saturated)) for _ in range(3)]
            await rig.t.settle()
            assert not any(task.done() for task in waiting)
            calls = rig.exchange.called("hist")
            await hist(source, request_class=free)  # both slots must still be free
            assert rig.exchange.called("hist") == calls + 1
            await rig.t.advance(WINDOW_MS)
            await asyncio.gather(*waiting)

    run(scenario)


def test_a_fetch_cancelled_while_waiting_in_an_exhausted_limiter_sends_nothing() -> None:
    async def scenario() -> None:
        rig = Rig(b=50)
        source = await rig.open()
        for _ in range(25):
            await hist(source)
        waiting = asyncio.create_task(hist(source))
        await rig.t.settle()
        await rig.t.advance(10 * S)  # the caller's deadline passes
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert rig.exchange.called("hist") == 25 and rig.limiter.acquired == 25
        assert rig.limiter.starts_in_window(rig.t.monotonic_ns(), R) == 25
        await hist(source, request_class=C)  # no slot leaked

    run(scenario)


def test_an_exhausted_budget_makes_the_request_wait_and_it_sends_after_the_window() -> None:
    async def scenario() -> None:
        rig = Rig(b=50)
        source = await rig.open()
        for _ in range(25):
            await hist(source)
        first_send = rig.exchange.instants[0]
        waiting = asyncio.create_task(hist(source))
        await rig.t.settle()
        await rig.t.advance(WINDOW_MS - 1)
        assert not waiting.done()
        await rig.t.advance(1)
        await waiting
        assert rig.exchange.instants[-1] - first_send == WINDOW_MS * NS_PER_MS  # exactly 305 s, not 304.999

    run(scenario)


def test_a_call_that_waited_in_the_limiter_sends_nothing_after_another_call_got_a_429() -> None:
    async def scenario() -> None:
        rig = Rig(b=50)
        source = await rig.open()
        for _ in range(25):
            await hist(source)
        rig.exchange.last_response_headers = {"Retry-After": "900"}
        rig.exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused):
            await hist(source, request_class=C)  # another class gets the 429
        waiting = asyncio.create_task(hist(source))  # R group is full: waits for the window
        await rig.t.settle()
        await rig.t.advance(WINDOW_MS)  # the limiter grants; the 900 s pause is still running
        with pytest.raises(ExtensionPaused):
            await waiting
        assert rig.exchange.called("hist") == 26

    run(scenario)


def test_after_the_pause_earlier_starts_still_count_in_the_limiter() -> None:
    async def scenario() -> None:
        rig = Rig(b=50)
        source = await rig.open()
        for _ in range(25):
            await hist(source)
        rig.exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused) as raised:
            await hist(source, request_class=C)
        await rig.t.advance(raised.value.remaining_ms)  # 300 s: the pause is over, the window (305 s) is not
        waiting = asyncio.create_task(hist(source))
        await rig.t.settle()
        assert not waiting.done()  # the 25 earlier R starts still count
        await rig.t.advance(5 * S)
        await waiting

    run(scenario)


def test_paging_rule_over_the_fake_source() -> None:
    """P-15 Paging row: the requests the fake accepts are the ones the FR-1 rule produces."""
    start = 1_793_000_000_000

    def drive(times: list[int], rate_types: dict[int, list[str]] | None = None) -> tuple[list[int], int, set[int]]:
        requested: list[int] = []
        stored: set[int] = set()
        span: Interval | None = None

        async def scenario() -> None:
            nonlocal span
            rig = Rig()
            rig.exchange.dataset = funding_rows(times, rate_types=rate_types)
            source = await rig.open()
            end = times[-1] + 7
            current = start
            while True:
                requested.append(current)
                response = await source.fetch_funding_rate_history(SYMBOL, current, end, request_class=H)
                rows = parse_funding_rate_history(
                    response, market_id=MARKET_ID, symbol=SYMBOL, start_time_ms=current, end_time_ms=end
                )
                stored.update(row.key.funding_time_ms for row in rows)
                coverage = page_coverage(rows, start_ms=current, end_ms=end)
                span = coverage.covered if span is None else merge_span(span, coverage.covered)
                if coverage.next_start_ms is None:
                    break
                current = coverage.next_start_ms
            assert span == Interval(start, end)
            assert all(c[1]["limit"] == FUNDING_RATE_PAGE_LIMIT for c in rig.exchange.calls if c[0] == "hist")

        run(scenario)
        return requested, len(stored), stored

    times = [start + i * 1_000 for i in range(999)]
    assert len(drive(times)[0]) == 1  # 999 rows: one request
    times = [start + i * 1_000 for i in range(1_000)]
    requested, _, _ = drive(times)
    assert requested == [start, times[-1]]  # exactly 1 000 rows: one extra request that returns one row
    times = [start + i * 1_000 for i in range(999)]
    shared = {times[-1]: ["Regular", "Special"]}  # 1 000 rows, the last two share a fundingTime
    requested, count, stored = drive(times, shared)
    assert requested == [start, times[-1]] and times[-1] in stored  # neither of the pair is skipped
    times = [start + i * 1_000 for i in range(2_500)]
    requested, count, stored = drive(times)
    assert requested == [start, times[999], times[1998]] and count == 2_500 and stored == set(times)


@pytest.mark.parametrize("error", [ccxt.RequestTimeout("timed out"), ddos(429)], ids=["timeout", "429"])
def test_ccxt_does_not_retry_a_failed_raw_call(error: ccxt.BaseError) -> None:
    async def scenario() -> None:
        client = ccxt.async_support.binanceusdm()  # a real client; no network: ``fetch`` is stubbed
        transport_calls: list[str] = []

        async def fetch(url: str, method: str = "GET", headers: Any = None, body: Any = None) -> object:
            transport_calls.append(url)
            raise error

        client.fetch = fetch
        try:
            with pytest.raises(type(error)):
                await client.fapiPublicGetFundingRate(
                    {"symbol": MARKET_ID, "startTime": AS_OF - HOUR, "endTime": AS_OF, "limit": 1000}
                )
        finally:
            await client.close()
        assert len(transport_calls) == 1
        assert transport_calls[0].startswith("https://fapi.binance.com/fapi/v1/fundingRate?")

    run(scenario)


def test_a_fresh_client_has_no_max_retries_on_failure_option_and_the_adapter_sends_no_such_param() -> None:
    async def scenario() -> None:
        client = ccxt.async_support.binanceusdm()
        try:
            options = client.options
            assert "maxRetriesOnFailure" not in options and "defaultMaxRetriesOnFailure" not in options
            assert "maxRetriesOnFailure" not in (options.get("fundingRate") or {})
        finally:
            await client.close()
        rig = Rig()
        source = await rig.open()
        await hist(source)
        assert set(rig.exchange.calls[-1][1]) == {"symbol", "startTime", "endTime", "limit"}

    run(scenario)


def test_candle_requests_continue_while_funding_is_paused_banned_raising_or_hanging() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        candle_exchange = FakeCandleExchange()
        candle = await BinanceUsdmSource.open(candle_exchange)
        rig.exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused):
            await hist(source)
        assert (await candle.fetch_server_time()).server_time_ms == 1792677035725
        assert len(await candle.fetch_klines(SYMBOL, P5, 100)) > 0
        rig.pause.record_banned()
        with pytest.raises(ExtensionStopped):
            await hist(source)
        assert len(await candle.fetch_klines(SYMBOL, P5, 100)) > 0
        # raising on every call
        raising = Rig()
        raising_source = await raising.open()
        raising.exchange.error_always = ccxt.NetworkError("connection reset")
        for _ in range(3):
            with pytest.raises(SourceError):
                await hist(raising_source)
        assert len(await candle.fetch_klines(SYMBOL, P5, 100)) > 0
        # hanging with both slots held
        hanging = Rig()
        hanging_source = await hanging.open()
        hanging.exchange.hold = True
        stuck = [asyncio.create_task(hist(hanging_source)) for _ in range(MAX_IN_FLIGHT + 1)]
        await hanging.t.settle()
        assert hanging.exchange.in_flight == MAX_IN_FLIGHT
        assert len(await candle.fetch_klines(SYMBOL, P5, 100)) > 0
        assert (await candle.fetch_server_time()).server_time_ms == 1792677035725
        hanging.exchange.hold = False
        hanging.exchange.release.set()
        await asyncio.gather(*stuck)

    run(scenario)


def test_the_module_does_not_import_the_candle_limiter_shared_server_time_or_open_interest() -> None:
    source = module.__file__
    assert source is not None
    text = Path(source).read_text()
    for forbidden in ("collector.rate_limiter", "shared_server_time", "open_interest"):
        assert forbidden not in text


PAUSE_CASES = {
    "429": ddos(429),
    "503_ddos": ddos(503),
    "rate_limit_exceeded": ccxt.RateLimitExceeded('binanceusdm {"code":-1003,"msg":"Too many requests."}'),
}


def test_at_most_two_requests_are_in_flight_time_included() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        assert MAX_IN_FLIGHT == 2
        rig.exchange.hold = True
        tasks = [asyncio.create_task(hist(source)) for _ in range(4)] + [
            asyncio.create_task(source.fetch_server_time())
        ]
        await rig.t.settle()
        assert rig.exchange.in_flight == 2
        rig.exchange.hold = False
        rig.exchange.release.set()
        await asyncio.gather(*tasks)
        assert rig.exchange.peak == 2
        assert rig.exchange.called("hist") + rig.exchange.called("time") == 5

    run(scenario)


def test_a_cancelled_call_releases_its_in_flight_slot() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.hold = True
        stuck = [asyncio.create_task(hist(source)) for _ in range(2)]
        await rig.t.settle()
        for task in stuck:
            task.cancel()
        await asyncio.gather(*stuck, return_exceptions=True)
        rig.exchange.hold = False
        await hist(source)  # would hang if a slot had leaked
        assert rig.exchange.in_flight == 0

    run(scenario)


@pytest.mark.parametrize("error", PAUSE_CASES.values(), ids=PAUSE_CASES.keys())
@pytest.mark.parametrize("endpoint", ["hist", "time"])
def test_429_and_the_other_rate_signals_pause_all_funding_requests(error: ccxt.BaseError, endpoint: str) -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.errors = [error]
        call = hist(source) if endpoint == "hist" else source.fetch_server_time()
        with pytest.raises(ExtensionPaused) as raised:
            await call
        assert raised.value.remaining_ms == 300 * S
        calls_before, budget_before = len(rig.exchange.calls), rig.limiter.acquired
        for _ in range(5):
            with pytest.raises(ExtensionPaused):
                await hist(source)
            with pytest.raises(ExtensionPaused):
                await source.fetch_server_time()
        assert (len(rig.exchange.calls), rig.limiter.acquired) == (calls_before, budget_before)
        await rig.t.advance(300 * S)
        await hist(source)  # the pause is over

    run(scenario)


def test_consecutive_429s_double_the_pause_and_a_success_resets_it() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        remaining = []
        for _ in range(3):
            rig.exchange.errors = [ddos(429)]
            with pytest.raises(ExtensionPaused) as raised:
                await hist(source)
            remaining.append(raised.value.remaining_ms)
            await rig.t.advance(raised.value.remaining_ms)
        assert remaining == [300 * S, 600 * S, 1200 * S]
        await hist(source)  # success resets the doubling
        rig.exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused) as raised:
            await hist(source)
        assert raised.value.remaining_ms == 300 * S

    run(scenario)


@pytest.mark.parametrize(
    ("headers", "expected_s"),
    [
        ({"Retry-After": "900"}, 900),
        ({"retry-after": "900"}, 900),
        ({"RETRY-AFTER": "60"}, 300),
        ({"Retry-After": "abc"}, 300),
        ({"Retry-After": "-1"}, 300),
        ({"Retry-After": "1.5"}, 300),
        ({"Retry-After": ""}, 300),
        ({}, 300),
    ],
)
def test_retry_after_is_read_case_insensitively_and_strictly(headers: dict[str, str], expected_s: int) -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.last_response_headers = headers
        rig.exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused) as raised:
            await hist(source)
        assert raised.value.remaining_ms == expected_s * S

    run(scenario)


@pytest.mark.parametrize(
    "error",
    [
        ddos(503),
        ccxt.RateLimitExceeded('binanceusdm {"code":-1003,"msg":"Too many requests."}'),
    ],
    ids=["other_ddos_protection", "rate_limit_exceeded"],
)
def test_retry_after_is_only_read_for_an_http_429(error: ccxt.BaseError) -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.last_response_headers = {"Retry-After": "900"}  # may belong to another response
        rig.exchange.errors = [error]
        with pytest.raises(ExtensionPaused) as raised:
            await hist(source)
        assert raised.value.remaining_ms == 300 * S

    run(scenario)


@pytest.mark.parametrize(
    "bad_time", [{"serverTime": "x"}, {"serverTime": 1}, {}], ids=["str", "out_of_range", "missing"]
)
def test_a_time_answer_the_adapter_rejects_still_counts_as_a_success_for_the_gate(bad_time: object) -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused):
            await hist(source)
        await rig.t.advance(300 * S)
        rig.exchange.time_response = bad_time
        with pytest.raises(SourceError) as raised:
            await source.fetch_server_time()
        assert not isinstance(raised.value, (ExtensionPaused, ExtensionStopped))
        rig.exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused) as again:
            await hist(source)
        assert again.value.remaining_ms == 300 * S  # the answer arrived: the doubling was reset

    run(scenario)


def test_the_pause_signal_names_the_end_of_the_pause() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        started = rig.t.monotonic_ns()
        rig.exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused) as raised:
            await hist(source)
        assert raised.value.until_monotonic_ns == started + 300 * S * NS_PER_MS

    run(scenario)


def test_a_request_that_started_before_a_pause_cannot_reset_the_doubling() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.hold = True
        slow = asyncio.create_task(hist(source))  # starts, then waits on the exchange
        await rig.t.settle()
        rig.exchange.hold = False
        rig.exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused):
            await hist(source)  # a second request gets the 429 meanwhile
        await rig.t.advance(300 * S)  # the pause is over
        rig.exchange.release.set()
        await slow  # the slow request succeeds only now: it started before the pause
        rig.exchange.errors = [ddos(429)]
        with pytest.raises(ExtensionPaused) as raised:
            await hist(source)
        assert raised.value.remaining_ms == 600 * S

    run(scenario)


def test_418_stops_funding_for_the_run_on_both_endpoints() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.errors = [ddos(418, '{"code":-1003,"msg":"Way too many requests; IP banned."}')]
        with pytest.raises(ExtensionStopped):
            await hist(source)
        calls_before = len(rig.exchange.calls)
        await rig.t.advance(2 * 3600 * S)
        with pytest.raises(ExtensionStopped):
            await hist(source)
        with pytest.raises(ExtensionStopped):
            await source.fetch_server_time()
        assert len(rig.exchange.calls) == calls_before

    run(scenario)


def test_418_on_the_time_request_also_bans() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.errors = [ddos(418)]
        with pytest.raises(ExtensionStopped):
            await source.fetch_server_time()
        with pytest.raises(ExtensionStopped):
            await hist(source)

    run(scenario)


@pytest.mark.parametrize("status", [429, 418])
def test_a_call_that_waited_for_an_in_flight_slot_sends_nothing_after_a_429_or_418(status: int) -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.hold = True
        rig.exchange.errors = [ddos(status)]
        running = [asyncio.create_task(hist(source)) for _ in range(2)]
        await rig.t.settle()
        waiting = asyncio.create_task(hist(source))  # waits for an in-flight slot
        await rig.t.settle()
        rig.exchange.hold = False
        rig.exchange.release.set()
        results = await asyncio.gather(*running, waiting, return_exceptions=True)
        assert all(isinstance(result, (ExtensionPaused, ExtensionStopped, list)) for result in results)
        assert isinstance(results[2], (ExtensionPaused, ExtensionStopped))
        assert rig.exchange.called("hist") == 2

    run(scenario)


def test_a_response_that_is_not_a_list_is_a_source_error() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.response = {"code": -1}
        with pytest.raises(SourceError, match="list"):
            await hist(source)

    run(scenario)


def test_other_failures_are_source_errors_and_do_not_pause() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.errors = [ccxt.NetworkError("connection reset")]
        with pytest.raises(SourceError, match="connection reset") as raised:
            await hist(source)
        assert not isinstance(raised.value, (ExtensionPaused, ExtensionStopped))
        rig.pause.check()

    run(scenario)


def test_close_closes_the_client() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        await source.close()
        assert rig.exchange.closed
        await assert_no_leftover_tasks()

    run(scenario)
