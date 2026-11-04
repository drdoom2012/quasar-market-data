import asyncio
import sqlite3
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import ccxt
import pytest

from quasar_market_data.collector.async_funding_rate_writer import FundingRatePageError, FundingRateWriteReport
from quasar_market_data.collector.binance_funding_rate import FundingRateParseError
from quasar_market_data.collector.fetch_funding_rate_once import FundingRateSelectionError
from quasar_market_data.collector.funding_rate_execution import FundingRateExecutionError, PageResult, execute_plan
from quasar_market_data.collector.funding_rate_reader import read_span
from quasar_market_data.collector.funding_rate_writer import (
    FundingRateWriteBusy,
    FundingRateWriteOutcomeUnknown,
    FundingSpanNotAdjacentError,
)
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.funding_rate import FundingRateKey
from quasar_market_data.domain.funding_rate_capacity import RequestClass
from quasar_market_data.domain.funding_rate_scheduler import HistoryChunk, Plan
from quasar_market_data.domain.funding_rate_span import Interval
from quasar_market_data.storage.connection import open_reader, open_writer
from tests.collector.faulty_connection import Fault, FaultyConnection, open_faulty_writer
from tests.collector.funding_rate_task_fakes import HOUR, VENUE, Rig, make_rig
from tests.collector.manual_time import ManualTime

SYMBOL = "BTC/USDT:USDT"
DELAY = 30_000
AT = 1_792_677_000_000
DAY = 24 * HOUR
R, C, H = RequestClass.R, RequestClass.C, RequestClass.H


def run(scenario: Callable[[], Coroutine[None, None, None]]) -> None:
    async def guarded() -> None:
        await asyncio.wait_for(scenario(), 60)

    asyncio.run(guarded())


def stored_times(db: Path, symbol: str = SYMBOL) -> list[int]:
    conn = open_reader(db)
    try:
        return [
            t
            for (t,) in conn.execute("SELECT funding_time_ms FROM funding_rates WHERE symbol = ? ORDER BY 1", (symbol,))
        ]
    finally:
        conn.close()


def conflict_count(db: Path) -> int:
    conn = open_reader(db)
    try:
        return int(conn.execute("SELECT count(*) FROM funding_rate_conflicts").fetchone()[0])
    finally:
        conn.close()


async def span_of(rig: Rig, symbol: str = SYMBOL) -> tuple[Interval | None, int | None]:
    return await read_span(rig.db, VENUE, symbol)


def b_now_of(t: ManualTime) -> int:
    return t.wall_ms() - DELAY


async def go(rig: Rig, plan: Plan, **overrides: Any) -> PageResult:
    arguments: dict[str, Any] = {
        "source": rig.source,
        "writer": rig.writer,
        "venue": VENUE,
        "delay_ms": DELAY,
        "b_now_ms": b_now_of(rig.t),
        "series_verified_at_ms": None,
        "span_planned": None,
        "clock": rig.t,
    }
    arguments.update(overrides)
    return await execute_plan(plan, **arguments)


def seed_plan(b_now: int, days: int = 1, symbol: str = SYMBOL) -> Plan:
    return Plan(symbol, "S", R, b_now - days * DAY, b_now, None)


async def advance_hours(rig: Rig, hours: int) -> None:
    await rig.t.advance(hours * HOUR)


def test_a_seed_page_creates_the_span_and_its_rows() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path)
        b_now = b_now_of(rig.t)
        result = await go(rig, seed_plan(b_now))
        span, verified = await span_of(rig)
        assert span == Interval(b_now - DAY, b_now) == result.span_after
        assert verified == rig.t.wall_ms() and result.verified_at_ms == verified
        times = stored_times(rig.db)
        assert result.rows == result.inserted == len(times) and len(times) in (24, 25)
        assert all(b_now - DAY <= t <= b_now for t in times) and result.truncated_next_start is None
        assert (result.late_events, result.conflicts, result.off_hour_rows) == ((), (), 0)

    run_with_tmp(scenario)


def run_with_tmp(scenario: Callable[[Path], Coroutine[None, None, None]]) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        run(lambda: scenario(Path(directory)))


def test_an_hourly_check_extends_the_span_upward_and_a_history_row_is_not_late() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path)
        first = b_now_of(rig.t)
        await go(rig, seed_plan(first))
        await advance_hours(rig, 3)
        second = b_now_of(rig.t)
        span, verified = await span_of(rig)
        assert span is not None
        result = await go(
            rig,
            Plan(SYMBOL, "R", R, max(span.from_ms, span.through_ms - DAY), second, None),
            b_now_ms=second,
            span_planned=span,
        )
        assert result.span_after == Interval(first - DAY, second) and result.late_events == ()
        assert result.inserted == 3
        history = await go(
            rig,
            Plan(
                SYMBOL,
                "H",
                H,
                first - 2 * DAY,
                first - DAY - 1,
                HistoryChunk(first - 2 * DAY, first - DAY - 1, first - 2 * DAY),
            ),
            series_verified_at_ms=verified,
        )
        assert history.span_after == Interval(first - 2 * DAY, second) and history.late_events == ()
        assert history.verified_at_ms == verified  # a history merge leaves verified_at_ms unchanged

    run_with_tmp(scenario)


def test_a_late_event_is_a_new_row_inside_the_old_span() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path)
        b_now = b_now_of(rig.t)
        late = ((b_now - 5 * HOUR) // HOUR) * HOUR + 3
        rig.exchange.hidden.add((SYMBOL, late))  # Binance lists it only later
        await go(rig, seed_plan(b_now))
        span, _ = await span_of(rig)
        assert span is not None and late not in stored_times(rig.db)
        rig.exchange.hidden.clear()
        result = await go(rig, Plan(SYMBOL, "R", R, span.through_ms - DAY, b_now, None), span_planned=span)
        assert [key.funding_time_ms for key in result.late_events] == [late]
        assert result.inserted == 1 and result.span_after == span

    run_with_tmp(scenario)


def test_a_full_page_stores_the_row_at_last_and_class_c_continues_there() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, period_ms=1_000)
        rig.exchange.offset_ms = 0
        b_now = b_now_of(rig.t)
        start = b_now - 2_499_000  # 2 500 settlements
        first = await go(rig, Plan(SYMBOL, "S", R, start, b_now, None))
        assert first.rows == 1_000 and first.truncated_next_start is not None
        last = first.truncated_next_start
        assert first.span_after == Interval(start, last - 1) and last in stored_times(rig.db)
        second = await go(rig, Plan(SYMBOL, "C", C, last, b_now, None), series_verified_at_ms=1)
        assert second.rows == 1_000 and second.duplicates == 1  # the row at `last` is fetched again
        assert second.span_after == Interval(start, second.truncated_next_start - 1)  # type: ignore[operator]
        third = await go(rig, Plan(SYMBOL, "C", C, second.truncated_next_start, b_now, None))  # type: ignore[arg-type]
        assert third.truncated_next_start is None and third.span_after == Interval(start, b_now)
        assert len(stored_times(rig.db)) == 2_500 and conflict_count(rig.db) == 0

    run_with_tmp(scenario)


def test_an_empty_answer_extends_the_span() -> None:
    async def scenario(tmp_path: Path) -> None:
        t = ManualTime()
        rig = await make_rig(tmp_path, t=t, listing_ms=t.wall_ms() + 10 * DAY)  # nothing listed yet
        b_now = b_now_of(rig.t)
        result = await go(rig, seed_plan(b_now))
        assert result.rows == 0 and result.span_after == Interval(b_now - DAY, b_now)
        assert stored_times(rig.db) == []

    run_with_tmp(scenario)


def test_a_three_page_history_chunk_claims_the_chunk_only_with_its_last_page() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, period_ms=1_000)
        rig.exchange.offset_ms = 0
        b_now = b_now_of(rig.t)
        await go(rig, Plan(SYMBOL, "S", R, b_now - 10_000, b_now, None))
        span, verified = await span_of(rig)
        assert span is not None
        chunk_from, chunk_through = span.from_ms - 2_499_000, span.from_ms - 1
        start = chunk_from
        pages = []
        while True:
            plan = Plan(SYMBOL, "H", H, start, chunk_through, HistoryChunk(chunk_from, chunk_through, start))
            result = await go(rig, plan, series_verified_at_ms=verified)
            pages.append(result)
            after, _ = await span_of(rig)
            if result.truncated_next_start is None:
                assert after == Interval(chunk_from, span.through_ms)
                break
            assert after == span  # earlier pages store rows and claim nothing
            assert result.span_after is None and result.verified_at_ms is None
            start = result.truncated_next_start
        assert len(pages) == 3 and pages[-1].verified_at_ms == verified
        assert len(set(stored_times(rig.db))) == len(stored_times(rig.db))  # rows of every page, each stored once
        assert min(stored_times(rig.db)) == chunk_from

    run_with_tmp(scenario)


def test_conflicts_and_off_hour_rows_are_reported() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path)
        b_now = b_now_of(rig.t)
        await go(rig, seed_plan(b_now, days=1))
        victim = stored_times(rig.db)[3]
        rig.exchange.rate_override[(SYMBOL, victim)] = "0.00090000"
        span, _ = await span_of(rig)
        again = await go(rig, Plan(SYMBOL, "R", R, span.from_ms, b_now, None) if span else seed_plan(b_now))
        assert [c.incoming.key.funding_time_ms for c in again.conflicts] == [victim]
        assert again.conflicts[0].newly_recorded is True and conflict_count(rig.db) == 1

    run_with_tmp(scenario)


def test_off_hour_rows_are_counted() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path)
        rig.exchange.offset_ms = 1_800_000  # half past the hour
        result = await go(rig, seed_plan(b_now_of(rig.t)))
        assert result.off_hour_rows == result.rows > 0

    run_with_tmp(scenario)


def test_a_parse_error_a_selection_error_and_a_source_error_claim_nothing() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path)
        b_now = b_now_of(rig.t)
        rig.exchange.mutate = lambda rows: [{**rows[0], "symbol": "ETHUSDT"}, *rows[1:]]
        with pytest.raises(FundingRateParseError):
            await go(rig, seed_plan(b_now))
        rig.exchange.mutate = None
        with pytest.raises(FundingRateSelectionError):  # the plan ends after the bound the rows must respect
            await go(rig, Plan(SYMBOL, "S", R, b_now - DAY, b_now, None), b_now_ms=b_now - 3 * HOUR)
        rig.exchange.error_always = ccxt.NetworkError("connection reset")
        with pytest.raises(SourceError):
            await go(rig, seed_plan(b_now))
        assert await span_of(rig) == (None, None) and stored_times(rig.db) == []

    run_with_tmp(scenario)


def test_a_busy_writer_fails_the_page_and_a_retry_succeeds_without_conflicts() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, busy_timeout_ms=50)
        b_now = b_now_of(rig.t)
        holder = open_writer(rig.db)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(FundingRateExecutionError) as raised:
                await go(rig, seed_plan(b_now))
            assert isinstance(raised.value.cause, FundingRatePageError)
            assert isinstance(raised.value.cause.cause, FundingRateWriteBusy)
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert await span_of(rig) == (None, None)
        await go(rig, seed_plan(b_now))
        assert conflict_count(rig.db) == 0 and (await span_of(rig))[0] is not None

    run_with_tmp(scenario)


def test_a_non_adjacent_merge_is_refused_and_nothing_of_the_page_is_kept() -> None:
    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path)
        b_now = b_now_of(rig.t)
        await go(rig, seed_plan(b_now))
        before = stored_times(rig.db)
        await advance_hours(rig, 40)
        later = b_now_of(rig.t)
        far = Plan(SYMBOL, "C", C, later - 2 * HOUR, later, None)  # starts 2 days above the span
        with pytest.raises(FundingRateExecutionError) as raised:
            await go(rig, far, b_now_ms=later)
        assert isinstance(raised.value.cause.cause, FundingSpanNotAdjacentError)
        assert stored_times(rig.db) == before

    run_with_tmp(scenario)


def test_the_late_events_of_a_failed_page_are_classified_with_the_planned_span() -> None:
    class Failing:
        async def write(self, rows: Any, *, ingested_at_ms: int, merge: Any = None) -> FundingRateWriteReport:
            keys = tuple(row.key for row in rows[:3])
            partial = FundingRateWriteReport(3, 0, (), (), (), 0, 0, keys, None, None)
            raise FundingRatePageError(FundingRateWriteOutcomeUnknown("commit failed"), 1, partial)

    async def scenario(tmp_path: Path) -> None:
        rig = await make_rig(tmp_path, sink=Failing())
        b_now = b_now_of(rig.t)
        planned = Interval(b_now - 7 * HOUR, b_now - 5 * HOUR)  # only the oldest rows of the page lie inside
        plan = Plan(SYMBOL, "R", R, b_now - 6 * HOUR, b_now, None)
        with pytest.raises(FundingRateExecutionError) as raised:
            await go(rig, plan, span_planned=planned)
        inside = [k for k in raised.value.late_events if planned.from_ms <= k.funding_time_ms <= planned.through_ms]
        assert 1 <= len(inside) < 3 and raised.value.late_events == tuple(inside)
        assert raised.value.rows > 3
        assert all(isinstance(key, FundingRateKey) for key in raised.value.late_events)
        assert raised.value.cause.committed_transactions == 1

    run_with_tmp(scenario)


class Gated(FaultyConnection):
    """Blocks the Nth COMMIT on the worker thread until released (the writer's transaction is then in flight)."""

    gate_on = 1
    armed = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.commits = 0
        self.reached = threading.Event()
        self.release = threading.Event()

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql == "COMMIT" and self.armed:
            self.commits += 1
            if self.commits == self.gate_on:
                self.reached.set()
                assert self.release.wait(10)
        return super().execute(sql, *args)


def test_a_cancellation_inside_the_final_transaction_leaves_rows_and_span_together() -> None:
    async def scenario(tmp_path: Path) -> None:
        made: list[Gated] = []

        def connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
            conn = open_faulty_writer(path, AT, factory=Gated)
            assert isinstance(conn, Gated)
            conn.armed = True
            made.append(conn)
            return conn

        rig = await make_rig(tmp_path, connect=connect)
        task = asyncio.ensure_future(go(rig, seed_plan(b_now_of(rig.t))))
        assert await asyncio.to_thread(made[0].reached.wait, 10)  # the final COMMIT is blocked on the worker thread
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        made[0].release.set()
        await rig.writer.close()
        span, _ = await span_of(rig)
        times = stored_times(rig.db)
        assert (span is None) == (times == [])  # both present or both absent, never a span without its rows
        if span is not None:
            assert all(t in times for t in times if span.from_ms <= t <= span.through_ms) and len(times) >= 24

    run_with_tmp(scenario)


FAULT_POINTS = {
    "chunk2_row": (Fault("INSERT INTO funding_rates (", sqlite3.OperationalError("injected"), nth=150), 100),
    "final_first_row": (Fault("INSERT INTO funding_rates (", sqlite3.OperationalError("injected"), nth=201), 200),
    "span_update": (Fault("UPDATE funding_rate_verified", sqlite3.OperationalError("injected")), 200),
    "commit_before": (Fault("COMMIT", sqlite3.OperationalError("injected"), nth=3), 200),
}


@pytest.mark.parametrize("point", sorted(FAULT_POINTS))
def test_restart_after_a_failure_point_of_a_250_row_page_converges_with_no_conflict(point: str) -> None:
    async def scenario(tmp_path: Path) -> None:
        fault, stored_before_restart = FAULT_POINTS[point]
        holder: list[FaultyConnection] = []

        def connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
            conn = open_faulty_writer(path, AT)
            conn.faults.append(Fault(fault.prefix, fault.exception, fault.nth))
            holder.append(conn)
            return conn

        rig = await make_rig(tmp_path, period_ms=1_000, connect=connect)
        rig.exchange.offset_ms = 0
        b_now = b_now_of(rig.t)
        plan = Plan(SYMBOL, "S", R, b_now - 249_000, b_now, None)  # 250 settlements, one page
        with pytest.raises(FundingRateExecutionError):
            await go(rig, plan)
        assert len(stored_times(rig.db)) == stored_before_restart and (await span_of(rig))[0] is None
        await rig.writer.close()
        (tmp_path / "clean").mkdir()
        clean = await make_rig(tmp_path / "clean", period_ms=1_000, t=rig.t)
        clean.exchange.offset_ms = 0
        await go(clean, plan)
        again = await make_rig_for_db(rig)
        await go(again, plan)
        assert stored_times(again.db) == stored_times(clean.db) and conflict_count(again.db) == 0
        assert (await span_of(again))[0] == (await span_of(clean))[0]

    run_with_tmp(scenario)


async def make_rig_for_db(previous: Rig) -> Rig:
    """A fresh writer on the same database file: the restart."""
    from quasar_market_data.collector.async_funding_rate_writer import AsyncFundingRateWriter

    writer = await AsyncFundingRateWriter.open(previous.db, activity=previous.activity)
    previous.t.track_executor(writer._executor)
    return Rig(
        previous.t,
        previous.exchange,
        previous.source,
        previous.limiter,
        previous.pause,
        previous.activity,
        previous.db,
        writer,
    )
