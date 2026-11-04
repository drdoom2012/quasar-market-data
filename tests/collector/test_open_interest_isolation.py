"""Open interest never delays or stops candle collection (ADR-0003 P-9, P-15 "Independence" and "Contention")."""

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector.collection_loop import CandleWriteHooks
from quasar_market_data.collector.extension_pause import ExtensionPaused, ExtensionStopped
from quasar_market_data.collector.kline_source import SourceError
from tests.collector.fetch_fakes import ControlledSink
from tests.collector.open_interest_task_fakes import ManualOiSource, make_rig, stored_times
from tests.collector.test_collection_loop import Harness

BTC = "BTC/USDT:USDT"
ROUNDS = 6


def run(scenario: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    return asyncio.run(scenario())


def hang(source: ManualOiSource) -> None:
    source.hang_hist = True


def raising(source: ManualOiSource) -> None:
    source.symbol_errors[BTC] = SourceError("down")


def no_time(source: ManualOiSource) -> None:
    source.time_error = SourceError("no time")


def rate_limited(source: ManualOiSource) -> None:
    source.errors = [ExtensionPaused(300_000, 0)] * 50


def banned(source: ManualOiSource) -> None:
    source.errors = [ExtensionStopped()]


BEHAVIOURS: dict[str, Callable[[ManualOiSource], None] | None] = {
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
    rig = None
    if configure is not None:
        rig = await make_rig(tmp_path, symbols=(BTC,), t=h.t)
        configure(rig.source)
        h.run_kwargs["candle_write_hooks"] = CandleWriteHooks(rig.activity, rig.measurements.record_candle_write)
    h.start()
    await h.until_rounds(1)
    if rig is not None:
        rig.start()  # open interest starts after the candle loop's first round
    for index in range(2, ROUNDS + 1):
        await h.advance_to_next_boundary()
        await h.until_rounds(index)
    h.stop.set()
    await h.task
    if rig is not None:
        rig.stop.set()
        assert rig.task is not None
        await h.t.settle()
        await asyncio.gather(rig.task, return_exceptions=True)
    return [(r.target_open_ms, r.results, r.lateness_ms, r.skipped_rounds, r.uncertain_symbols) for r in h.rounds]


@pytest.mark.parametrize("behaviour", [name for name in BEHAVIOURS if name != "disabled"])
def test_a_misbehaving_open_interest_source_leaves_the_candle_rounds_unchanged(tmp_path: Path, behaviour: str) -> None:
    baseline_dir, with_dir = tmp_path / "a", tmp_path / "b"
    baseline_dir.mkdir()
    with_dir.mkdir()
    baseline = run(lambda: candle_rounds(baseline_dir, "disabled"))
    with_open_interest = run(lambda: candle_rounds(with_dir, behaviour))
    assert len(baseline) == ROUNDS and with_open_interest == baseline  # same candles, same lateness_ms, same skips
    assert all(lateness == 0 for _, _, lateness, _, _ in with_open_interest)


def test_no_open_interest_transaction_begins_while_a_candle_write_is_in_flight(tmp_path: Path) -> None:
    async def scenario() -> None:
        sink = ControlledSink(block=True)
        h = Harness(("A",), sink=sink)
        rig = await make_rig(tmp_path, symbols=(BTC,), t=h.t)
        h.run_kwargs["candle_write_hooks"] = CandleWriteHooks(rig.activity, rig.measurements.record_candle_write)
        h.start()
        await sink.write_started.wait()
        assert rig.activity.active
        rig.start()
        await h.t.settle()
        await h.t.advance(30_000)  # the open interest round has fetched and wants to write: it has to wait
        await h.t.settle()
        assert stored_times(rig.db) == [] and rig.writer is not None
        sink.release.set()  # the candle write ends
        await h.until_rounds(1)
        await rig.until_lines("oi-round", 1)
        assert len(stored_times(rig.db)) >= 1  # now the open interest rows commit
        h.stop.set()
        await h.task
        await rig.stop_and_wait()

    run(scenario)
