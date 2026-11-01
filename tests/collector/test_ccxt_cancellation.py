"""CCXT cancellation facts (S16 plan, "Facts checked"), pinned against a local plain-HTTP server (no Binance, no TLS).

If CCXT changes either behaviour, these tests fail on purpose: the backfill's timeouts rely on both.
"""

import asyncio
import time
from collections.abc import Awaitable

import ccxt.async_support as ccxt_async
import pytest

from quasar_market_data.collector.binance_usdm import BinanceUsdmSource
from quasar_market_data.collector.clock import SystemClock
from quasar_market_data.domain.timeframe import Timeframe
from tests.collector.test_binance_usdm import SYMBOL, usdm_markets

BODY = b"[]"
RESPONSE = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\n" + BODY
)


open_writers: list[asyncio.StreamWriter] = []


async def serve(hang: bool) -> tuple[asyncio.Server, str]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        open_writers.append(writer)
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                if hang:
                    while await reader.read(1024):  # never answer; end when the peer goes away
                        pass
                    return
                writer.write(RESPONSE)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}/fapi/v1"


def client_for(url: str, *, interval_ms: float | None = None) -> ccxt_async.Exchange:
    client = ccxt_async.binanceusdm()
    client.urls["api"]["fapiPublic"] = url
    client.markets = usdm_markets()
    if interval_ms is not None:
        client.throttler.config["refillRate"] = 1 / interval_ms
    return client


async def stop(*servers: asyncio.Server) -> None:
    for server in servers:
        server.close()
    for writer in open_writers:
        writer.transport.abort()
    open_writers.clear()
    for server in servers:
        await server.wait_closed()


def leftover_tasks() -> list[asyncio.Task[object]]:
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


def test_cancelling_a_stalled_klines_request_returns_promptly_and_the_client_stays_usable() -> None:
    async def scenario() -> None:
        hanging, hang_url = await serve(hang=True)
        answering, ok_url = await serve(hang=False)
        try:
            client = client_for(hang_url)
            source = BinanceUsdmSource(client, SystemClock())
            task = asyncio.create_task(source.fetch_klines(SYMBOL, Timeframe("5m"), 10))
            await asyncio.sleep(0.05)
            started = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert time.monotonic() - started < 1.0
            client.urls["api"]["fapiPublic"] = ok_url
            assert await source.fetch_klines(SYMBOL, Timeframe("5m"), 10) == []
            await source.close()
            await stop(hanging, answering)  # the test's own server handlers are not the client's tasks
            await asyncio.sleep(0.05)
            assert leftover_tasks() == []
        finally:
            await stop(hanging, answering)

    asyncio.run(scenario())


def test_a_cancelled_request_keeps_its_ccxt_throttle_turn() -> None:
    interval_ms = 500.0

    async def total(cancel_second: bool) -> float:
        server, url = await serve(hang=False)
        try:
            client = client_for(url, interval_ms=interval_ms)
            assert client.throttler.config["refillRate"] == 1 / interval_ms  # fails loudly if CCXT renames it
            source = BinanceUsdmSource(client, SystemClock())
            started = time.monotonic()
            tasks: list[Awaitable[object]] = []
            made = [asyncio.ensure_future(source.fetch_klines(SYMBOL, Timeframe("5m"), 10)) for _ in range(4)]
            tasks.extend(made)
            await asyncio.sleep(0.05)
            if cancel_second:
                made[1].cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert [isinstance(r, asyncio.CancelledError) for r in results] == [False, cancel_second, False, False]
            elapsed = time.monotonic() - started
            await source.close()
            return elapsed
        finally:
            await stop(server)

    async def scenario() -> None:
        plain = await total(cancel_second=False)
        cancelled = await total(cancel_second=True)
        assert plain > 3 * interval_ms / 1000 * 0.8  # the throttle really was slow
        assert abs(cancelled - plain) <= 0.1 * plain  # the cancelled turn was not given back

    asyncio.run(scenario())
