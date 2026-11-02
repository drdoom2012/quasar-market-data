import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import ccxt
import ccxt.async_support
import pytest

from quasar_market_data.collector import binance_usdm_open_interest as module
from quasar_market_data.collector.binance_open_interest import parse_open_interest_hist
from quasar_market_data.collector.binance_usdm import BinanceUsdmSource
from quasar_market_data.collector.binance_usdm_open_interest import (
    MAX_IN_FLIGHT,
    BinanceUsdmOpenInterestSource,
    OpenInterestRequestRefused,
)
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.open_interest_budget import WINDOW_MS, OpenInterestBudget
from quasar_market_data.collector.open_interest_gate import OpenInterestBanned, OpenInterestGate, OpenInterestPaused
from quasar_market_data.domain.open_interest_request import RETRIEVAL_HORIZON_MS
from quasar_market_data.domain.timeframe import Timeframe
from tests.collector.manual_time import NS_PER_MS, ManualTime, assert_no_leftover_tasks
from tests.collector.test_binance_usdm import FakeExchange as FakeCandleExchange
from tests.collector.test_binance_usdm import usdm_markets

SYMBOL = "BTC/USDT:USDT"
MARKET_ID = "BTCUSDT"
P5 = Timeframe("5m")
STEP = P5.milliseconds
AS_OF = 1_793_620_800_000  # 2026-11-02T12:00:00Z, aligned to 5m
FAPI_URL = "https://fapi.binance.com/fapi/v1"
DATA_URL = "https://fapi.binance.com/futures/data"
S = 1000


def run(scenario: Callable[[], Coroutine[None, None, None]]) -> None:
    asyncio.run(scenario())


def hist_rows(market_id: str, start_ms: int, end_ms: int, limit: int) -> list[dict[str, Any]]:
    """What Binance answers: every period end of the range, but only the NEWEST ``limit`` rows (P-5 point 1)."""
    first = -(-start_ms // STEP) * STEP
    ends = range(first, end_ms + 1, STEP)
    rows = [
        {
            "symbol": market_id,
            "sumOpenInterest": f"{1000 + index}.5",
            "sumOpenInterestValue": f"{(1000 + index) * 100}.25",
            "CMCCirculatingSupply": "19000.0",
            "timestamp": end,
        }
        for index, end in enumerate(ends)
    ]
    return rows[-limit:]


class FakeExchange:
    id = "binanceusdm"

    def __init__(self, clock: ManualTime | None = None) -> None:
        self.markets: dict[str, dict[str, Any]] | None = usdm_markets()
        self.urls: dict[str, Any] = {"api": {"fapiPublic": FAPI_URL, "fapiData": DATA_URL}}
        self.last_response_headers: dict[str, str] = {}
        self.response: object | None = None  # None: answer like Binance from the request
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

    async def fapiDataGetOpenInterestHist(self, params: dict[str, Any]) -> object:
        def answer() -> object:
            if self.response is not None:
                return self.response
            return hist_rows(params["symbol"], params["startTime"], params["endTime"], params["limit"])

        return await self._call("hist", params, answer)

    async def fapiPublicGetTime(self) -> object:
        return await self._call("time", None, lambda: self.time_response)

    async def close(self) -> None:
        self.closed = True

    def called(self, name: str) -> int:
        return sum(1 for call_name, _ in self.calls if call_name == name)


class CountingBudget(OpenInterestBudget):
    def __init__(self, budget_per_5min: int, t: ManualTime) -> None:
        super().__init__(budget_per_5min, t, t.sleep)
        self.acquired = 0

    async def acquire(self) -> None:
        await super().acquire()
        self.acquired += 1


class Rig:
    def __init__(self, b: int = 400) -> None:
        self.t = ManualTime()
        self.exchange = FakeExchange(self.t)
        self.budget = CountingBudget(b, self.t)
        self.gate = OpenInterestGate(self.t)
        self.source: BinanceUsdmOpenInterestSource | None = None

    async def open(self) -> BinanceUsdmOpenInterestSource:
        self.source = await BinanceUsdmOpenInterestSource.open(self.budget, self.gate, self.t, self.exchange)
        return self.source


def ddos(status: int, body: str = '{"code":-1003,"msg":"Too many requests."}') -> ccxt.DDoSProtection:
    return ccxt.DDoSProtection(f"binanceusdm {status} Too Many Requests {body}")


async def hist(source: BinanceUsdmOpenInterestSource, start: int = AS_OF - 2 * STEP, end: int = AS_OF) -> list[object]:
    return await source.fetch_open_interest_hist(SYMBOL, start, end, as_of_ms=AS_OF)


def test_request_carries_the_market_id_period_bounds_and_limit_and_returns_raw_rows() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rows = await hist(source)
        assert rig.exchange.calls[-1] == (
            "hist",
            {
                "symbol": MARKET_ID,
                "period": "5m",
                "startTime": AS_OF - 2 * STEP,
                "endTime": AS_OF,
                "limit": 3,
            },
        )
        assert [row["timestamp"] for row in rows] == [AS_OF - 2 * STEP, AS_OF - STEP, AS_OF]  # type: ignore[index]
        assert isinstance(rows[0]["sumOpenInterest"], str)  # type: ignore[index]  # the string Binance sent, untouched

    run(scenario)


def test_the_rows_are_the_exchange_response_object_itself() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        response = hist_rows(MARKET_ID, AS_OF - STEP, AS_OF, 2)
        rig.exchange.response = response
        assert await hist(source, AS_OF - STEP, AS_OF) is response

    run(scenario)


def test_500_period_ends_are_requested_once_with_limit_500_and_all_rows_parse() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        start = AS_OF - 499 * STEP
        rows = await hist(source, start, AS_OF)
        assert rig.exchange.called("hist") == 1
        assert rig.exchange.calls[-1][1]["limit"] == 500
        parsed = parse_open_interest_hist(
            rows, market_id=MARKET_ID, symbol=SYMBOL, period=P5, start_time_ms=start, end_time_ms=AS_OF
        )
        assert len(parsed) == 500

    run(scenario)


def test_the_newest_limit_fake_drops_the_oldest_row_of_an_over_long_range() -> None:
    """The fake models P-5 point 1: this is the silent loss the 500-slot rule exists to prevent."""
    over_long_start = AS_OF - 500 * STEP  # 501 period ends
    rows = hist_rows(MARKET_ID, over_long_start, AS_OF, 500)
    assert len(rows) == 500
    assert rows[0]["timestamp"] == over_long_start + STEP  # the first period end of the range is gone


def test_501_period_ends_are_refused_without_a_request_or_a_budget_start() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        with pytest.raises(OpenInterestRequestRefused, match="501"):
            await hist(source, AS_OF - 500 * STEP, AS_OF)
        assert rig.exchange.called("hist") == 0
        assert rig.budget.acquired == 0

    run(scenario)


@pytest.mark.parametrize(
    ("start", "end"),
    [(AS_OF - 2 * STEP + 1, AS_OF), (AS_OF - 2 * STEP, AS_OF + 1), (AS_OF, AS_OF - STEP)],
    ids=["unaligned_start", "unaligned_end", "start_after_end"],
)
def test_unaligned_or_reversed_ranges_are_refused_without_a_request(start: int, end: int) -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        with pytest.raises(OpenInterestRequestRefused):
            await hist(source, start, end)
        assert rig.exchange.called("hist") == 0
        assert rig.budget.acquired == 0

    run(scenario)


def test_a_start_older_than_the_horizon_is_never_sent_and_one_at_the_horizon_is() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        too_old = AS_OF - RETRIEVAL_HORIZON_MS - STEP
        with pytest.raises(OpenInterestRequestRefused, match="horizon"):
            await hist(source, too_old, too_old + STEP)
        assert rig.exchange.called("hist") == 0
        at_horizon = AS_OF - RETRIEVAL_HORIZON_MS
        await hist(source, at_horizon, at_horizon + STEP)
        assert rig.exchange.called("hist") == 1

    run(scenario)


def test_a_1130_answer_is_a_source_error_not_a_crash_and_the_gate_is_unchanged() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.errors = [
            ccxt.BadRequest('binanceusdm {"code":-1130,"msg":"Invalid data sent for parameter \'startTime\'."}')
        ]
        with pytest.raises(SourceError, match="1130") as raised:
            await hist(source)
        assert not isinstance(raised.value, (OpenInterestRequestRefused, OpenInterestPaused, OpenInterestBanned))
        rig.gate.check()  # neither paused nor banned
        assert rig.budget.acquired == 1  # the request was sent
        await hist(source)  # and the source keeps working

    run(scenario)


def test_an_unknown_symbol_is_refused_before_any_request() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        with pytest.raises(SourceError, match="NOPE/USDT:USDT"):
            await source.fetch_open_interest_hist("NOPE/USDT:USDT", AS_OF - STEP, AS_OF, as_of_ms=AS_OF)
        assert rig.exchange.called("hist") == 0
        assert rig.budget.acquired == 0

    run(scenario)


@pytest.mark.parametrize("change", [{"swap": False}, {"linear": False}, {"settle": "USD"}, {"expiry": 1893456000000}])
def test_a_market_that_is_not_a_usdt_perpetual_is_refused(change: dict[str, Any]) -> None:
    async def scenario() -> None:
        rig = Rig()
        rig.exchange.markets = {SYMBOL: {**usdm_markets()[SYMBOL], **change}}
        source = await rig.open()
        with pytest.raises(SourceError, match="USDT-M perpetual"):
            await hist(source)
        assert rig.exchange.called("hist") == 0

    run(scenario)


def test_server_time_comes_from_the_open_interest_client_and_is_counted_in_the_budget() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        observation = await source.fetch_server_time()
        assert observation.server_time_ms == AS_OF
        assert rig.exchange.called("time") == 1
        assert rig.budget.acquired == 1

    run(scenario)


def test_open_creates_its_own_client_each_time_and_never_the_candle_sources() -> None:
    created: list[FakeExchange] = []

    def factory() -> FakeExchange:
        created.append(FakeExchange())
        return created[-1]

    async def scenario() -> None:
        t = ManualTime()
        candle = await BinanceUsdmSource.open()  # patched factory: the candle source's own client
        first = await BinanceUsdmOpenInterestSource.open(OpenInterestBudget(10, t, t.sleep), OpenInterestGate(t), t)
        second = await BinanceUsdmOpenInterestSource.open(OpenInterestBudget(10, t, t.sleep), OpenInterestGate(t), t)
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


BAD_DATA_URLS: dict[str, object] = {
    "http_scheme": "http://fapi.binance.com/futures/data",
    "other_host": "https://evil.example/futures/data",
    "lookalike_host": "https://fapi.binance.com.evil.example/futures/data",
    "userinfo_trick": "https://fapi.binance.com@evil.example/futures/data",
    "other_path": "https://fapi.binance.com/futures/other",
    "extra_path": "https://fapi.binance.com/futures/data/extra",
    "explicit_port": "https://fapi.binance.com:8443/futures/data",
    "with_query": "https://fapi.binance.com/futures/data?x=1",
    "coin_m_host": "https://dapi.binance.com/futures/data",
    "malformed_port": "https://fapi.binance.com:abc/futures/data",
    "not_a_string": None,
}


@pytest.mark.parametrize("url", BAD_DATA_URLS.values(), ids=BAD_DATA_URLS.keys())
def test_the_fapi_data_url_must_be_exactly_the_usdm_data_endpoint(url: object) -> None:
    async def scenario() -> None:
        rig = Rig()
        rig.exchange.urls = {"api": {"fapiPublic": FAPI_URL, "fapiData": url}}
        with pytest.raises(SourceError):
            await rig.open()
        assert rig.exchange.called("load_markets") == 0  # not one request goes to an unexpected host
        assert rig.exchange.closed

    run(scenario)


@pytest.mark.parametrize("urls", [None, {}, {"api": {}}, {"api": {"fapiPublic": FAPI_URL}}, {"api": None}])
def test_a_missing_fapi_data_url_is_a_source_error(urls: object) -> None:
    async def scenario() -> None:
        rig = Rig()
        rig.exchange.urls = urls  # type: ignore[assignment]
        with pytest.raises(SourceError):
            await rig.open()
        assert rig.exchange.closed

    run(scenario)


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


def max_in_window(instants: list[int]) -> int:
    ordered = sorted(instants)
    worst, low = 0, 0
    window = WINDOW_MS * NS_PER_MS
    for high, instant in enumerate(ordered):
        while ordered[low] <= instant - window:
            low += 1
        worst = max(worst, high - low + 1)
    return worst


def test_b_100_is_never_exceeded_by_hist_and_time_calls_competing() -> None:
    async def scenario() -> None:
        rig = Rig(b=100)
        source = await rig.open()
        tasks = [asyncio.create_task(hist(source) if index % 3 else source.fetch_server_time()) for index in range(150)]
        while not all(task.done() for task in tasks):
            await rig.t.advance(10_000)
        await asyncio.gather(*tasks)
        assert len(rig.exchange.instants) == 150
        assert max_in_window(rig.exchange.instants) == 100

    run(scenario)


def test_the_in_flight_slot_is_taken_before_the_budget() -> None:
    """With b = 3 and two calls held for 400 s, ten queued calls must not use up budget while they wait."""

    async def scenario() -> None:
        rig = Rig(b=3)
        source = await rig.open()
        rig.exchange.hold = True
        held = [asyncio.create_task(hist(source)) for _ in range(2)]
        await rig.t.settle()
        queued = [asyncio.create_task(hist(source)) for _ in range(10)]
        await rig.t.settle()
        await rig.t.advance(400 * S)
        rig.exchange.hold = False
        rig.exchange.release.set()
        while not all(task.done() for task in held + queued):
            await rig.t.advance(10 * S)
        await asyncio.gather(*held, *queued)
        assert len(rig.exchange.instants) == 12
        assert max_in_window(rig.exchange.instants) <= 3

    run(scenario)


PAUSE_CASES = {
    "429": ddos(429),
    "503_ddos": ddos(503),
    "rate_limit_exceeded": ccxt.RateLimitExceeded('binanceusdm {"code":-1003,"msg":"Too many requests."}'),
}


@pytest.mark.parametrize("error", PAUSE_CASES.values(), ids=PAUSE_CASES.keys())
@pytest.mark.parametrize("endpoint", ["hist", "time"])
def test_429_and_the_other_rate_signals_pause_all_open_interest_requests(error: ccxt.BaseError, endpoint: str) -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.errors = [error]
        call = hist(source) if endpoint == "hist" else source.fetch_server_time()
        with pytest.raises(OpenInterestPaused) as raised:
            await call
        assert raised.value.remaining_ms == 300 * S
        calls_before, budget_before = len(rig.exchange.calls), rig.budget.acquired
        for _ in range(5):
            with pytest.raises(OpenInterestPaused):
                await hist(source)
            with pytest.raises(OpenInterestPaused):
                await source.fetch_server_time()
        assert (len(rig.exchange.calls), rig.budget.acquired) == (calls_before, budget_before)
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
            with pytest.raises(OpenInterestPaused) as raised:
                await hist(source)
            remaining.append(raised.value.remaining_ms)
            await rig.t.advance(raised.value.remaining_ms)
        assert remaining == [300 * S, 600 * S, 1200 * S]
        await hist(source)  # success resets the doubling
        rig.exchange.errors = [ddos(429)]
        with pytest.raises(OpenInterestPaused) as raised:
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
        with pytest.raises(OpenInterestPaused) as raised:
            await hist(source)
        assert raised.value.remaining_ms == expected_s * S

    run(scenario)


def test_418_stops_open_interest_for_the_run_on_both_endpoints() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.errors = [ddos(418, '{"code":-1003,"msg":"Way too many requests; IP banned."}')]
        with pytest.raises(OpenInterestBanned):
            await hist(source)
        calls_before = len(rig.exchange.calls)
        await rig.t.advance(2 * 3600 * S)
        with pytest.raises(OpenInterestBanned):
            await hist(source)
        with pytest.raises(OpenInterestBanned):
            await source.fetch_server_time()
        assert len(rig.exchange.calls) == calls_before

    run(scenario)


def test_418_on_the_time_request_also_bans() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        rig.exchange.errors = [ddos(418)]
        with pytest.raises(OpenInterestBanned):
            await source.fetch_server_time()
        with pytest.raises(OpenInterestBanned):
            await hist(source)

    run(scenario)


def test_a_call_that_waited_in_the_budget_sends_nothing_after_another_call_got_a_429() -> None:
    async def scenario() -> None:
        rig = Rig(b=1)
        source = await rig.open()
        rig.exchange.hold = True
        rig.exchange.last_response_headers = {"Retry-After": "900"}
        rig.exchange.errors = [ddos(429)]
        first = asyncio.create_task(hist(source))
        await rig.t.settle()
        second = asyncio.create_task(hist(source))  # waits in the budget (b = 1)
        await rig.t.settle()
        rig.exchange.hold = False
        rig.exchange.release.set()
        with pytest.raises(OpenInterestPaused):
            await first
        await rig.t.advance(WINDOW_MS)  # the budget frees its slot, the pause (900 s) is still running
        with pytest.raises(OpenInterestPaused):
            await second
        assert rig.exchange.called("hist") == 1

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
        assert all(isinstance(result, (OpenInterestPaused, OpenInterestBanned, list)) for result in results)
        assert isinstance(results[2], (OpenInterestPaused, OpenInterestBanned))
        assert rig.exchange.called("hist") == 2

    run(scenario)


def test_candle_requests_continue_while_open_interest_is_paused_and_banned() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        candle_exchange = FakeCandleExchange()
        candle = await BinanceUsdmSource.open(candle_exchange)
        rig.exchange.errors = [ddos(429)]
        with pytest.raises(OpenInterestPaused):
            await hist(source)
        assert (await candle.fetch_server_time()).server_time_ms == 1792677035725
        assert len(await candle.fetch_klines(SYMBOL, P5, 100)) > 0
        rig.gate.record_banned()
        with pytest.raises(OpenInterestBanned):
            await hist(source)
        assert len(await candle.fetch_klines(SYMBOL, P5, 100)) > 0

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
        assert not isinstance(raised.value, (OpenInterestPaused, OpenInterestBanned))
        rig.gate.check()

    run(scenario)


def test_close_closes_the_client() -> None:
    async def scenario() -> None:
        rig = Rig()
        source = await rig.open()
        await source.close()
        assert rig.exchange.closed
        await assert_no_leftover_tasks()

    run(scenario)


def test_the_module_does_not_import_the_candle_limiter_or_shared_server_time() -> None:
    source = module.__file__
    assert source is not None
    text = Path(source).read_text()
    assert "rate_limiter" not in text
    assert "shared_server_time" not in text
