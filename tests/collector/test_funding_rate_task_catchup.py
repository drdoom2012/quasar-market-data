import asyncio
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import ccxt
import pytest

from quasar_market_data.collector.async_funding_rate_writer import FundingRatePageError, FundingRateWriteReport
from quasar_market_data.collector.funding_rate_reader import read_span
from quasar_market_data.collector.funding_rate_writer import FundingRateWriteOutcomeUnknown, SpanMerge
from quasar_market_data.domain.funding_rate_span import DAY_MS, HISTORY_CHUNK_MS, Interval, history_pending
from quasar_market_data.storage.connection import open_reader
from tests.collector.funding_rate_task_fakes import (
    HOUR,
    VENUE,
    Harness,
    InMemorySink,
    make_rig,
    record_plans,
    reopen,
    seed_state,
    start_task,
    symbols_of,
)
from tests.collector.manual_time import ManualTime

S = 1000
BASE_WALL = 1_792_677_036_000
B0 = BASE_WALL // HOUR * HOUR
SYMBOL = "BTC/USDT:USDT"


def run(scenario: Callable[[Path], Coroutine[None, None, None]], timeout: float = 300) -> None:
    async def guarded() -> None:
        with tempfile.TemporaryDirectory() as directory:
            await asyncio.wait_for(scenario(Path(directory)), timeout)

    asyncio.run(guarded())


async def seed_span(rig: Any, symbol: str, span: Interval, verified_at_ms: int | None = None) -> None:
    merge = SpanMerge(VENUE, symbol, span, verified_at_ms or rig.t.wall_ms())
    await rig.writer.write([], ingested_at_ms=rig.t.wall_ms(), merge=merge)


def rows_and_conflicts(db: Path) -> tuple[int, int]:
    conn = open_reader(db)
    try:
        return (
            int(conn.execute("SELECT count(*) FROM funding_rates").fetchone()[0]),
            int(conn.execute("SELECT count(*) FROM funding_rate_conflicts").fetchone()[0]),
        )
    finally:
        conn.close()


async def span(h: Harness | Any, symbol: str = SYMBOL) -> Interval | None:
    rig = h.rig if isinstance(h, Harness) else h
    return (await read_span(rig.db, VENUE, symbol))[0]


def test_a_behind_series_after_a_restart_is_caught_up_with_class_c_from_through_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(tmp_path: Path) -> None:
        t = ManualTime(wall_start_ms=B0 + 3_000)
        rig = await make_rig(tmp_path, symbols=(SYMBOL,), t=t)
        through = B0 - 10 * HOUR
        await seed_span(rig, SYMBOL, Interval(through - 3 * DAY_MS, through))
        plans = record_plans(monkeypatch)
        h = await start_task(tmp_path, symbols=(SYMBOL,), rig=rig, backfill_days=1)
        await h.run_for(120 * S, step_ms=10 * S)
        first = plans[0]
        assert (first.kind, first.start_ms) == ("C", through + 1)  # not an R request from through - recheck
        new = await span(h)
        assert new is not None and new.through_ms >= B0 - 2 * HOUR and new.from_ms == through - 3 * DAY_MS
        assert not [line for line in h.lines if "error" in line]
        await h.finish()

    run(scenario)


def test_restart_mid_forward_catch_up_continues_from_through_plus_one(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(tmp_path: Path) -> None:
        t = ManualTime(wall_start_ms=B0 + 3_000)
        rig = await make_rig(tmp_path, symbols=(SYMBOL,), t=t, period_ms=1_000, request_ms=500)
        rig.exchange.offset_ms = 0
        through = B0 - 12_000_000  # 12 000 s of one-second settlements are missing: twelve full pages, behind
        await seed_span(rig, SYMBOL, Interval(through - 2 * DAY_MS, through))
        plans = record_plans(monkeypatch)
        h = await start_task(tmp_path, symbols=(SYMBOL,), rig=rig, backfill_days=1)
        for _ in range(600):
            await h.t.advance(100)
            if len(plans) >= 1 and (await span(h)) != Interval(through - 2 * DAY_MS, through):
                break
        await h.finish()  # the process ends after page 1 of 3
        after_first = await span(h)
        assert after_first is not None and after_first.through_ms < B0 - 2_000_000
        again = await reopen(rig)
        plans.clear()
        h2 = await start_task(tmp_path, symbols=(SYMBOL,), rig=again, backfill_days=1)
        await h2.run_for(300 * S, step_ms=5 * S)
        assert (plans[0].kind, plans[0].start_ms) == ("C", after_first.through_ms + 1)
        final = await span(h2)
        assert final is not None and final.through_ms >= B0 - 60_000
        assert rows_and_conflicts(rig.db)[1] == 0
        await h2.finish()

    run(scenario)


def test_backward_history_progresses_chunk_by_chunk_survives_failures_and_a_crash_loop_still_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(tmp_path: Path) -> None:
        t = ManualTime(wall_start_ms=B0 + 3_000)
        rig = await make_rig(tmp_path, symbols=(SYMBOL,), t=t, request_ms=2_000)
        start_span = Interval(B0 - 2 * HOUR, B0 - HOUR)
        await seed_span(rig, SYMBOL, start_span)
        days = 365
        target = start_span.through_ms - days * DAY_MS
        edges = {start_span.from_ms - k * HISTORY_CHUNK_MS for k in range(6)} | {target}
        current = rig
        seen: set[int] = set()
        for run_number in range(30):  # a crash loop: each run is killed after it completed at most one chunk
            h = await start_task(tmp_path, symbols=(SYMBOL,), rig=current, backfill_days=days)
            before = (await span(h)).from_ms  # type: ignore[union-attr]
            for _ in range(400):
                await h.t.advance(500)
                now = await span(h)
                assert now is not None and now.from_ms in edges, now  # only whole chunks are ever claimed
                if now.from_ms != before:
                    break
            seen.add((await span(h)).from_ms)  # type: ignore[union-attr]
            await h.finish()
            if (await span(h)).from_ms == target:  # type: ignore[union-attr]
                break
            current = await reopen(current)
        final = await span(current)
        assert final is not None and final.from_ms == target and not history_pending(final, days)
        assert len(seen) >= 4  # several chunks, each in its own run
        assert rows_and_conflicts(rig.db)[1] == 0

    run(scenario, timeout=600)


def test_a_failed_page_inside_a_chunk_repeats_the_same_start_and_claims_nothing() -> None:
    async def scenario(tmp_path: Path) -> None:
        t = ManualTime(wall_start_ms=B0 + 3_000)
        rig = await make_rig(tmp_path, symbols=(SYMBOL,), t=t)
        start_span = Interval(B0 - 2 * HOUR, B0 - HOUR)
        await seed_span(rig, SYMBOL, start_span)
        rig.exchange.errors = [None, None, ccxt.NetworkError("reset")]  # the observation is not a funding request
        h = await start_task(tmp_path, symbols=(SYMBOL,), rig=rig, backfill_days=365)
        await h.run_for(60 * S, step_ms=2 * S)
        starts = [s.start_ms for s in rig.exchange.served]
        assert len(starts) != len(set(starts))  # the failed page was asked again from the same start
        assert not h.lines_starting("funding-suspended")  # one failure only
        await h.finish()

    run(scenario)


def test_raising_the_depth_continues_below_from_and_lowering_it_requests_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(tmp_path: Path) -> None:
        t = ManualTime(wall_start_ms=B0 + 3_000)
        rig = await make_rig(tmp_path, symbols=(SYMBOL,), t=t)
        await seed_span(rig, SYMBOL, Interval(B0 - 2 * HOUR, B0 - HOUR))
        h = await start_task(tmp_path, symbols=(SYMBOL,), rig=rig, backfill_days=90)
        await h.run_for(120 * S, step_ms=5 * S)
        done = await span(h)
        assert done is not None and not history_pending(done, 90)
        await h.finish()
        deeper = await reopen(rig)
        h2 = await start_task(tmp_path, symbols=(SYMBOL,), rig=deeper, backfill_days=180)
        await h2.run_for(120 * S, step_ms=5 * S)
        wider = await span(h2)
        assert wider is not None and wider.from_ms < done.from_ms and not history_pending(wider, 180)
        await h2.finish()
        plans = record_plans(monkeypatch)
        shallower = await reopen(deeper)
        h3 = await start_task(tmp_path, symbols=(SYMBOL,), rig=shallower, backfill_days=30)
        await h3.run_for(120 * S, step_ms=5 * S)
        assert not [p for p in plans if p.kind == "H"] and await span(h3) is not None
        await h3.finish()

    run(scenario, timeout=600)


def test_a_young_symbol_extends_the_span_with_empty_chunks_until_the_depth_is_reached() -> None:
    async def scenario(tmp_path: Path) -> None:
        t = ManualTime(wall_start_ms=B0 + 3_000)
        listing = B0 - 20 * DAY_MS
        rig = await make_rig(tmp_path, symbols=(SYMBOL,), t=t, listing_ms=listing)
        await seed_span(rig, SYMBOL, Interval(B0 - 2 * HOUR, B0 - HOUR))
        h = await start_task(tmp_path, symbols=(SYMBOL,), rig=rig, backfill_days=365)
        await h.run_for(300 * S, step_ms=5 * S)
        final = await span(h)
        assert final is not None and not history_pending(final, 365)
        assert final.through_ms - final.from_ms >= 365 * DAY_MS  # width claimed although nothing was listed before
        conn = open_reader(rig.db)
        try:
            oldest = conn.execute("SELECT min(funding_time_ms) FROM funding_rates").fetchone()[0]
        finally:
            conn.close()
        assert oldest is not None and oldest >= listing
        await h.finish()

    run(scenario, timeout=600)


def test_a_finished_history_stays_finished_as_through_moves_over_24_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(tmp_path: Path) -> None:
        t = ManualTime(wall_start_ms=B0 + 3_000)
        sink = InMemorySink()
        rig = await make_rig(tmp_path, symbols=(SYMBOL,), t=t, sink=sink)
        h = await start_task(tmp_path, symbols=(SYMBOL,), rig=rig, backfill_days=7)
        await h.run_for(300 * S, step_ms=10 * S)  # seed, then the whole depth
        assert not history_pending(sink.spans[SYMBOL], 7)
        plans = record_plans(monkeypatch)
        await h.run_for(24 * HOUR, step_ms=5 * 60 * S)
        assert plans and all(p.kind == "R" for p in plans) and len(plans) >= 23
        assert not history_pending(sink.spans[SYMBOL], 7)
        await h.finish()

    run(scenario, timeout=600)


def test_an_outage_of_forty_days_is_caught_up_and_nothing_is_reported_unrecoverable() -> None:
    async def scenario(tmp_path: Path) -> None:
        t = ManualTime(wall_start_ms=B0 + 3_000)
        rig = await make_rig(tmp_path, symbols=(SYMBOL,), t=t)
        through = B0 - 40 * DAY_MS
        await seed_span(rig, SYMBOL, Interval(through - 3 * DAY_MS, through))
        h = await start_task(tmp_path, symbols=(SYMBOL,), rig=rig, backfill_days=1)
        await h.run_for(HOUR + 200 * S, step_ms=30 * S)
        final = await span(h)
        assert final is not None and final.through_ms >= B0 - 2 * HOUR  # C pages, then the round's R request
        assert not [line for line in h.lines if "error" in line or "unrecoverable" in line]
        await h.finish()

    run(scenario)


class FlakyOnce:
    def __init__(self, inner: Any, committed: bool) -> None:
        self.inner, self.committed, self.failed = inner, committed, False

    async def write(self, rows: Any, *, ingested_at_ms: int, merge: Any = None) -> Any:
        if merge is not None and not self.failed:
            self.failed = True
            if self.committed:  # the transaction took effect but the answer was lost
                await self.inner.write(rows, ingested_at_ms=ingested_at_ms, merge=merge)
            partial = FundingRateWriteReport(0, 0, (), (), (), 0, 0, (), None, None)
            raise FundingRatePageError(FundingRateWriteOutcomeUnknown("commit failed"), 0, partial)
        return await self.inner.write(rows, ingested_at_ms=ingested_at_ms, merge=merge)

    @property
    def _executor(self) -> Any:
        return self.inner._executor


def test_the_unknown_outcome_of_a_final_transaction_converges_with_growth_committed_or_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(tmp_path: Path) -> None:
        for committed in (True, False):
            sub = tmp_path / str(committed)
            sub.mkdir()
            t = ManualTime(wall_start_ms=B0 + 3_000)
            rig = await make_rig(sub, symbols=(SYMBOL,), t=t)
            flaky = FlakyOnce(rig.writer, committed)
            rig.writer = flaky
            h = await start_task(sub, symbols=(SYMBOL,), rig=rig, backfill_days=1)
            await h.run_for(60 * S, step_ms=2 * S)
            final = await span(h)
            assert final is not None and final.through_ms >= B0 - HOUR and flaky.failed
            assert rows_and_conflicts(rig.db)[1] == 0
            await h.finish()

    run(scenario, timeout=600)


@pytest.mark.parametrize(
    "b",
    [50, pytest.param(150, marks=pytest.mark.simulation), pytest.param(250, marks=pytest.mark.simulation)],
)
def test_history_cannot_starve_behind_catch_up_at_the_task_level(b: int, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(tmp_path: Path) -> None:
        m = b // 2
        per_window = b - m
        h_per_window = per_window // 2
        c_symbols = symbols_of(200)
        h_symbols = tuple(f"H{i:04d}/USDT:USDT" for i in range(300))
        t = ManualTime(wall_start_ms=B0 + 3_000)
        sink = InMemorySink()
        rig = await make_rig(
            tmp_path, symbols=(*c_symbols, *h_symbols), t=t, sink=sink, budget_per_5min=b, period_ms=1_000
        )
        rig.exchange.offset_ms = 0
        for symbol in h_symbols:
            rig.exchange.periods[symbol] = 8 * HOUR  # a 90-day chunk is 270 rows: one page
        through = B0 - 3_600_000
        for symbol in c_symbols:  # 12 000 s of one-second settlements missing: a dozen full pages each, 200 series
            seed_state(
                rig, symbol, Interval(through - 3651 * DAY_MS, through - 12_000_000)
            )  # history done: only class C
        for symbol in h_symbols:
            seed_state(rig, symbol, Interval(through - 3 * HOUR, through))
        plans = record_plans(monkeypatch)
        h = await start_task(tmp_path, symbols=(*c_symbols, *h_symbols), rig=rig, budget_per_5min=b, backfill_days=3650)
        windows = 50
        await h.run_for(windows * 305 * S + 60 * S, step_ms=5 * S)
        ch = [p for p in plans if p.kind in ("C", "H")]
        assert len(ch) >= windows * per_window * 9 // 10
        for n in range(1, len(ch) + 1):  # every prefix: H gets at least floor(n / 2) while it has work
            assert sum(1 for p in ch[:n] if p.kind == "H") >= n // 2 - 1, n
        first_chunk: dict[str, int] = {}
        for index, plan in enumerate(ch):
            if plan.kind == "H":
                first_chunk.setdefault(plan.symbol, index)
        bound = 2 * len(h_symbols)  # H gets every second selection: P pending series are all served within 2P
        served_h = [index for index in first_chunk.values()]
        assert len(first_chunk) >= min(len(h_symbols), (len(ch) // 2) - 1) and max(served_h) <= bound + per_window
        assert sum(1 for p in ch if p.kind == "C") >= windows * h_per_window * 8 // 10  # C keeps its half
        await h.finish()

    run(scenario, timeout=1_800)


def test_a_history_chunk_whose_final_page_was_committed_with_an_unknown_answer_is_dropped_after_the_re_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(tmp_path: Path) -> None:
        t = ManualTime(wall_start_ms=B0 + 3_000)
        rig = await make_rig(tmp_path, symbols=(SYMBOL,), t=t)
        await seed_span(rig, SYMBOL, Interval(B0 - 2 * HOUR, B0 - HOUR))
        rig.writer = FlakyOnce(rig.writer, committed=True)  # the first merge (the final page of chunk one) is lost
        plans = record_plans(monkeypatch)
        h = await start_task(tmp_path, symbols=(SYMBOL,), rig=rig, backfill_days=365)
        await h.run_for(60 * S, step_ms=2 * S)
        first_chunk = next(p.chunk for p in plans if p.kind == "H")
        assert first_chunk is not None
        same_chunk = [p for p in plans if p.kind == "H" and p.chunk and p.chunk.through_ms == first_chunk.through_ms]
        assert len(same_chunk) == 3  # three pages, once: the claimed chunk is not requested a second time
        later = [p for p in plans if p.kind == "H" and p.chunk and p.chunk.through_ms < first_chunk.from_ms]
        assert later  # and the history went on below it
        assert rows_and_conflicts(rig.db)[1] == 0
        await h.finish()

    run(scenario)
