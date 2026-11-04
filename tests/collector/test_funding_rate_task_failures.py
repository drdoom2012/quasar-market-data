import asyncio
import itertools
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import ccxt
import pytest

from quasar_market_data.collector.async_funding_rate_writer import FundingRatePageError, FundingRateWriteReport
from quasar_market_data.collector.extension_pause import ExtensionStopped
from quasar_market_data.collector.funding_rate_reader import FundingRateReadError, read_span
from quasar_market_data.collector.funding_rate_task import REQUEST_TIMEOUT_MS, FundingRateWriterUnusable
from quasar_market_data.collector.funding_rate_writer import (
    FundingRateWriteError,
    FundingSpanNotAdjacentError,
)
from quasar_market_data.domain.funding_rate_span import Interval
from tests.collector.funding_rate_task_fakes import (
    HOUR,
    VENUE,
    Harness,
    InMemorySink,
    make_rig,
    record_plans,
    reopen,
    start_task,
)
from tests.collector.manual_time import ManualTime, assert_no_leftover_tasks

S = 1000
B0 = 1_792_677_036_000 // HOUR * HOUR
BTC, ETH = "BTC/USDT:USDT", "ETH/USDT:USDT"
PARTIAL = FundingRateWriteReport(0, 0, (), (), (), 0, 0, (), None, None)


def run(scenario: Callable[[Path], Coroutine[None, None, None]], timeout: float = 300) -> None:
    async def guarded() -> None:
        with tempfile.TemporaryDirectory() as directory:
            await asyncio.wait_for(scenario(Path(directory)), timeout)

    asyncio.run(guarded())


def t0() -> ManualTime:
    return ManualTime(wall_start_ms=B0 + 3_000)


def warnings(h: Harness, prefix: str) -> list[str]:
    return [line for line in h.lines if line.startswith(prefix)]


def test_three_failures_suspend_a_series_for_6_then_12_then_24_hours_and_the_others_go_on() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, symbols=(BTC, ETH), t=t0())
        rig.exchange.symbol_errors[BTC] = ccxt.NetworkError("down")
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(60 * S, step_ms=S)
        assert [line.split()[1:3] for line in h.lines_starting("funding-suspended")] == [["symbol=" + BTC, "hours=6"]]
        assert (await read_span(rig.db, VENUE, ETH))[0] is not None  # the other series was seeded meanwhile
        for hours in (12, 24, 24):
            seen = len(h.lines_starting("funding-suspended"))
            while len(h.lines_starting("funding-suspended")) == seen:  # until the next probe has failed
                await h.run_for(10 * 60 * S, step_ms=10 * 60 * S)
            assert h.lines_starting("funding-suspended")[-1].split()[2] == f"hours={hours}"
        assert len(h.lines_starting("funding-probe")) >= 3
        rig.exchange.symbol_errors.clear()
        await h.run_for(26 * HOUR, step_ms=20 * 60 * S)  # the probe succeeds: everything is cleared
        assert (await read_span(rig.db, VENUE, BTC))[0] is not None
        await h.finish()

    run(scenario, timeout=900)


def test_a_restart_clears_a_suspension() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,), t=t0())
        rig.exchange.symbol_errors[BTC] = ccxt.NetworkError("down")
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(60 * S, step_ms=S)
        assert h.lines_starting("funding-suspended")
        await h.finish()
        rig.exchange.symbol_errors.clear()
        h2 = await start_task(tmp_path, rig=await reopen(rig), backfill_days=1)
        await h2.run_for(90 * S, step_ms=5 * S)
        assert (await read_span(rig.db, VENUE, BTC))[0] is not None and not h2.lines_starting("funding-suspended")
        await h2.finish()

    run(scenario)


def test_a_failure_is_retried_after_one_and_then_two_seconds_inside_the_same_share() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,), t=t0())
        rig.exchange.errors = [ccxt.NetworkError("x"), ccxt.NetworkError("x"), None]
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(90 * S, step_ms=250)
        starts = [served.at_ns for served in rig.exchange.served]
        gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
        assert gaps[:2] == [1_000_000_000, 2_000_000_000]  # 1 s after the first failure, 2 s after the second
        assert not h.lines_starting("funding-suspended") and (await read_span(rig.db, VENUE, BTC))[0] is not None
        await h.finish()

    run(scenario)


def test_a_429_pauses_every_start_for_the_pause_and_is_not_a_series_failure() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, symbols=(BTC, ETH), t=t0())
        rig.exchange.last_response_headers = {"Retry-After": "900"}
        rig.exchange.errors = [ccxt.DDoSProtection("binanceusdm 429 Too Many Requests {}")]
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(120 * S, step_ms=5 * S)
        calls = len(rig.exchange.served)
        await h.run_for(600 * S, step_ms=30 * S)
        assert len(rig.exchange.served) == calls  # silent for the whole 900 s pause
        await h.run_for(400 * S, step_ms=30 * S)
        assert (await read_span(rig.db, VENUE, BTC))[0] is not None and (await read_span(rig.db, VENUE, ETH))[
            0
        ] is not None
        assert not h.lines_starting("funding-suspended") and not h.lines_starting("funding-warning request-failed")
        await h.finish()

    run(scenario)


def test_a_418_ends_the_task_by_raising() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,), t=t0())
        rig.exchange.errors = [ccxt.DDoSProtection("binanceusdm 418 I'm a teapot {}")]
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(120 * S, step_ms=5 * S)
        assert h.task.done() and isinstance(h.task.exception(), ExtensionStopped)

    run(scenario)


@pytest.mark.parametrize(
    ("cause", "fatal"),
    [
        (FundingRateWriteError("disk image is malformed"), True),
        (FundingSpanNotAdjacentError("interval does not touch the span"), False),
    ],
    ids=["unusable_writer", "non_adjacent_merge"],
)
def test_an_unusable_writer_ends_the_task_but_a_refused_merge_only_fails_the_series(
    cause: FundingRateWriteError, fatal: bool
) -> None:
    async def scenario(tmp_path: Path) -> None:
        sink = InMemorySink()
        sink.fail_next.append(FundingRatePageError(cause, 0, PARTIAL))
        rig = await make_rig(tmp_path, symbols=(BTC,), t=t0(), sink=sink)
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(60 * S, step_ms=2 * S)
        if fatal:
            assert h.task.done() and isinstance(h.task.exception(), FundingRateWriterUnusable)
        else:
            assert not h.task.done() and BTC in sink.spans  # the series failed once, was re-read and then succeeded
            assert h.lines_starting("funding-warning merge-refused")
            await h.finish()

    run(scenario)


def test_a_failing_start_up_read_ends_the_task() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,), t=t0())
        rig.db.unlink()
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(5 * S, step_ms=S)
        assert h.task.done() and isinstance(h.task.exception(), FundingRateReadError)

    run(scenario)


def test_a_source_that_raises_for_one_series_does_not_stop_the_others() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, symbols=(BTC, ETH), t=t0())
        rig.exchange.symbol_errors[ETH] = ccxt.NetworkError("down")
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(120 * S, step_ms=5 * S)
        assert (await read_span(rig.db, VENUE, BTC))[0] is not None and not h.task.done()
        await h.finish()

    run(scenario)


def test_garbage_for_one_series_is_a_series_failure_not_the_end_of_the_task() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, symbols=(BTC, ETH), t=t0())
        rig.exchange.mutate = lambda rows: [{**row, "symbol": "WRONG"} for row in rows]
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(90 * S, step_ms=2 * S)
        assert not h.task.done() and h.lines_starting("funding-warning request-failed")
        await h.finish()

    run(scenario)


def test_a_hung_page_fails_with_the_request_timeout_and_cannot_stall_the_round_or_stop() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, symbols=(BTC, ETH), t=t0())
        rig.exchange.hang_symbols = {BTC, ETH}  # both slots held by hung pages
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(60 * S + REQUEST_TIMEOUT_MS + 5 * S, step_ms=2 * S)  # the round wakes after 57 s
        failed = h.lines_starting("funding-warning request-failed")
        assert failed and "TimeoutError" in failed[0]
        rig.exchange.hang_symbols = set()
        await h.run_for(90 * S, step_ms=5 * S)  # the retries then succeed
        assert (await read_span(rig.db, VENUE, BTC))[0] is not None
        rig.exchange.hang_symbols = {BTC, ETH}
        await h.run_for(B0 + 2 * HOUR + 120 * S - h.t.wall_ms(), step_ms=60 * S)  # a round with everything hanging
        assert len(round_count(h)) >= 2  # the rounds still closed at their deadlines
        await h.finish()  # and stop works with workers hung
        assert h.task.done()
        await assert_no_leftover_tasks()

    run(scenario, timeout=600)


def round_count(h: Harness) -> list[str]:
    return h.lines_starting("funding-round")


def test_stop_mid_round_cancels_the_workers_and_leaves_no_task_and_no_half_claimed_span() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, symbols=(BTC, ETH), t=t0(), request_ms=5_000)
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(60 * S, step_ms=S)  # inside the first round with requests in flight
        await h.finish()
        assert h.task.done() and h.task.exception() is None
        await assert_no_leftover_tasks()
        for symbol in (BTC, ETH):
            span, _ = await read_span(rig.db, VENUE, symbol)
            assert span is None or isinstance(span, Interval)

    run(scenario)


def test_a_refused_request_is_logged_as_such_and_fails_only_that_series() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, symbols=(BTC, ETH), t=t0())
        rig.exchange.markets[ETH]["active"] = False
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(90 * S, step_ms=2 * S)
        assert h.lines_starting("funding-error request-refused")
        assert (await read_span(rig.db, VENUE, BTC))[0] is not None
        await h.finish()

    run(scenario)


def test_the_hourly_measurement_line_is_emitted_and_a_last_one_on_stop() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, symbols=(BTC,), t=t0())
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(HOUR + 30 * S, step_ms=60 * S)
        line = h.lines_starting("funding-measurement")[0]
        assert "peak_starts_305s=" in line and "funding100_tx=" in line and "window_s=" in line
        before = len(h.lines_starting("funding-measurement"))
        await h.run_for(120 * S, step_ms=30 * S)
        await h.finish()
        assert len(h.lines_starting("funding-measurement")) >= before  # a last line only when something was collected
        _unused: Any = None
        del _unused

    run(scenario)


def test_during_a_429_pause_the_dispatcher_starts_no_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(tmp_path: Path) -> None:
        plans = record_plans(monkeypatch)
        rig = await make_rig(tmp_path, symbols=(BTC, ETH), t=t0())
        rig.exchange.errors = [ccxt.DDoSProtection("binanceusdm 429 Too Many Requests {}")]
        h = await start_task(tmp_path, rig=rig, backfill_days=1)
        await h.run_for(120 * S, step_ms=5 * S)
        started = len(plans)
        await h.run_for(200 * S, step_ms=5 * S)
        assert len(plans) == started  # nothing is even started (and so nothing spins) while the pause lasts
        await h.finish()

    run(scenario)


def test_a_round_request_still_running_at_the_deadline_is_cancelled_not_failed_and_the_series_is_re_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(tmp_path: Path) -> None:
        from quasar_market_data.collector.funding_rate_reader import read_span as original

        rereads: list[str] = []

        async def counting(db: Path, venue: str, symbol: str) -> Any:
            rereads.append(symbol)
            return await original(db, venue, symbol)

        monkeypatch.setattr("quasar_market_data.collector.funding_rate_task.read_span", counting)
        rig = await make_rig(tmp_path, symbols=(BTC,), t=ManualTime(wall_start_ms=B0 + 3_570_000), request_ms=20_000)
        rig.exchange.time_request_ms = 0  # the observation is quick; the seed page takes 20 s
        h = await start_task(tmp_path, rig=rig, backfill_days=1)  # the first round has 15 s left
        await h.run_for(14 * S, step_ms=S)
        assert rig.exchange.active_by_symbol.get(BTC) == 1  # the seed request is in flight (it takes 20 s)
        await h.run_for(4 * S, step_ms=S)  # past the deadline
        assert rig.exchange.active_by_symbol.get(BTC) == 0  # cancelled at the deadline, not left running
        assert not h.lines_starting("funding-warning request-failed") and not h.lines_starting("funding-suspended")
        (line,) = h.lines_starting("funding-round")
        assert "failed=0" in line
        assert rereads == [BTC]  # its outcome is unknown: the span is read before it is planned again
        await h.finish()

    run(scenario)


def test_a_success_between_failures_clears_the_count_so_four_failures_do_not_suspend() -> None:
    async def scenario(tmp_path: Path) -> None:
        err = ccxt.NetworkError("x")
        rig = await make_rig(tmp_path, symbols=(BTC,), t=t0())
        await rig.writer.write([], ingested_at_ms=rig.t.wall_ms(), merge=None)
        from quasar_market_data.collector.funding_rate_writer import SpanMerge

        await rig.writer.write(
            [],
            ingested_at_ms=rig.t.wall_ms(),
            merge=SpanMerge(VENUE, BTC, Interval(B0 - 2 * HOUR, B0 - HOUR), rig.t.wall_ms()),
        )
        rig.exchange.errors = [err, err, None, err, err, None]  # two failures, a success, two failures, a success
        h = await start_task(tmp_path, rig=rig, backfill_days=365)
        await h.run_for(200 * S, step_ms=S)
        assert len(rig.exchange.served) >= 6
        assert not h.lines_starting("funding-suspended")
        await h.finish()

    run(scenario)
