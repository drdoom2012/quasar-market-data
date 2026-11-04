"""Funding rate never delays or stops candle collection (ADR-0004 P-9, P-15 "Independence" row)."""

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import ccxt
import pytest

from quasar_market_data.collector.collection_loop import CandleWriteHooks
from quasar_market_data.collector.funding_rate_measurements import FundingRateMeasurements
from tests.collector.funding_rate_task_fakes import FakeFundingExchange, make_rig, start_task
from tests.collector.test_collection_loop import Harness

BTC = "BTC/USDT:USDT"
ROUNDS = 6


def run(scenario: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    return asyncio.run(scenario())


def hang(exchange: FakeFundingExchange) -> None:
    exchange.hang = True


def raising(exchange: FakeFundingExchange) -> None:
    exchange.error_always = ccxt.NetworkError("down")


def no_time(exchange: FakeFundingExchange) -> None:
    exchange.time_error = ccxt.NetworkError("no time")


def rate_limited(exchange: FakeFundingExchange) -> None:
    exchange.errors = [ccxt.DDoSProtection("binanceusdm 429 Too Many Requests {}")] * 50


def banned(exchange: FakeFundingExchange) -> None:
    exchange.errors = [ccxt.DDoSProtection("binanceusdm 418 I'm a teapot {}")]


BEHAVIOURS: dict[str, Callable[[FakeFundingExchange], None] | None] = {
    "disabled": None,
    "hangs": hang,
    "raises": raising,
    "no-time": no_time,
    "429": rate_limited,
    "418": banned,
}


async def candle_rounds(tmp_path: Path, behaviour: str) -> list[tuple[Any, ...]]:
    h = Harness(("A",))
    configure = BEHAVIOURS[behaviour]
    task_harness = None
    if configure is not None:
        rig = await make_rig(tmp_path, symbols=(BTC,), t=h.t)
        configure(rig.exchange)
        h.run_kwargs["candle_write_hooks"] = CandleWriteHooks(
            rig.activity, FundingRateMeasurements().record_candle_write
        )
    h.start()
    await h.until_rounds(1)
    if configure is not None:
        task_harness = await start_task(tmp_path, rig=rig, backfill_days=1)  # funding starts after the first round
    for index in range(2, ROUNDS + 1):
        await h.advance_to_next_boundary()
        await h.until_rounds(index)
    h.stop.set()
    await h.task
    if task_harness is not None:
        task_harness.stop.set()
        await h.t.settle()
        await asyncio.gather(task_harness.task, return_exceptions=True)
    return [(r.target_open_ms, r.results, r.lateness_ms, r.skipped_rounds, r.uncertain_symbols) for r in h.rounds]


@pytest.mark.parametrize("behaviour", [name for name in BEHAVIOURS if name != "disabled"])
def test_a_misbehaving_funding_source_leaves_the_candle_rounds_unchanged(tmp_path: Path, behaviour: str) -> None:
    baseline_dir, with_dir = tmp_path / "a", tmp_path / "b"
    baseline_dir.mkdir()
    with_dir.mkdir()
    baseline = run(lambda: candle_rounds(baseline_dir, "disabled"))
    with_funding = run(lambda: candle_rounds(with_dir, behaviour))
    assert len(baseline) == ROUNDS and with_funding == baseline  # same candles, same lateness_ms, same skips
    assert all(lateness == 0 for _, _, lateness, _, _ in with_funding)
