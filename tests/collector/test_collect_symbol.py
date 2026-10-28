import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.candle_writer import CandleWriteError, CandleWriteOutcomeUnknown, CandleWriter
from quasar_market_data.collector.collection_loop import (
    RetryPolicy,
    SymbolOutcome,
    SymbolResult,
    _WriteWatch,
    collect_symbol,
)
from quasar_market_data.collector.fetch_once import CandleSink, FetchOnceConfig
from quasar_market_data.collector.kline_source import KlineSource, SourceError
from quasar_market_data.domain.validation import ValidatedCandle
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.faulty_connection import Fault, open_faulty_writer
from tests.collector.fetch_fakes import (
    FIVE_MINUTES,
    SYMBOL,
    AdvancingSleep,
    ControlledSink,
    FakeClock,
    FakeSource,
    closed_rows,
    forming_rows,
)
from tests.collector.manual_time import assert_no_leftover_tasks
from tests.collector.test_binance_usdm import FakeExchange
from tests.collector.test_binance_usdm import open_source as open_binance_source
from tests.collector.test_candle_writer import AT

TARGET = int(closed_rows()[-1][0])
FORMING_TARGET = int(forming_rows()[-1][0])
RETRY = RetryPolicy()
WAITS = [1000, 2000, 4000, 8000, 8000]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "market.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=AT)
    conn.close()
    return path


def cfg() -> FetchOnceConfig:
    return FetchOnceConfig(symbol=SYMBOL, timeframe=FIVE_MINUTES, limit=100)


def stored_opens(db: Path) -> list[int]:
    conn = sqlite3.connect(db)
    try:
        return [row[0] for row in conn.execute("SELECT open_time_ms FROM candles ORDER BY open_time_ms")]
    finally:
        conn.close()


def count(db: Path, table: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def collect_with(
    sink_factory: Callable[[], Awaitable[CandleSink]],
    source: FakeSource,
    clock: FakeClock,
    sleep: AdvancingSleep,
    target: int | None = TARGET,
    stop: asyncio.Event | None = None,
    retry: RetryPolicy = RETRY,
) -> SymbolResult:
    async def scenario() -> SymbolResult:
        sink = await sink_factory()
        return await collect_symbol(source, sink, cfg(), target, retry, clock, sleep, stop or asyncio.Event())

    return asyncio.run(scenario())


def collect_real(
    db: Path,
    source: KlineSource,
    clock: FakeClock,
    sleep: AdvancingSleep | None = None,
    target: int | None = TARGET,
) -> SymbolResult:
    async def scenario() -> SymbolResult:
        writer = await AsyncCandleWriter.open(db)
        try:
            return await collect_symbol(
                source, writer, cfg(), target, RETRY, clock, sleep or AdvancingSleep(clock), asyncio.Event()
            )
        finally:
            await writer.close()

    return asyncio.run(scenario())


def revised(rows: list[list[Any]], index: int) -> list[list[Any]]:
    rows[index][4] = rows[index][3] if rows[index][3] != rows[index][4] else rows[index][2]  # valid, different close
    return rows


async def sink_of(sink: CandleSink) -> CandleSink:
    return sink


def test_collected_means_the_target_is_in_a_returned_write_report(db: Path) -> None:
    clock = FakeClock()
    result = collect_real(db, FakeSource(clock, closed_rows()), clock)
    assert result.outcome is SymbolOutcome.COLLECTED
    assert result.attempts == 1 and result.inserted == 100
    assert result.last_write is not None and result.last_write.conflicts == ()
    assert TARGET in stored_opens(db)


def test_duplicate_target_is_collected(db: Path) -> None:
    clock = FakeClock()
    collect_real(db, FakeSource(clock, closed_rows()), clock)
    result = collect_real(db, FakeSource(clock, closed_rows()), clock)
    assert result.outcome is SymbolOutcome.COLLECTED
    assert result.inserted == 0
    assert result.last_write is not None and result.last_write.duplicates == 100


def test_target_conflict_is_conflicted_and_not_retried(db: Path) -> None:
    clock = FakeClock()
    collect_real(db, FakeSource(clock, closed_rows()), clock)
    sleep = AdvancingSleep(clock)
    result = collect_real(db, FakeSource(clock, revised(closed_rows(), -1)), clock, sleep)
    assert result.outcome is SymbolOutcome.CONFLICTED
    assert result.attempts == 1 and sleep.calls == []
    assert result.conflict_open_times == (TARGET,)
    assert count(db, "candle_conflicts") == 1


def test_older_slot_conflict_is_listed_and_the_target_is_collected(db: Path) -> None:
    clock = FakeClock()
    collect_real(db, FakeSource(clock, closed_rows()), clock)
    older = int(closed_rows()[10][0])
    result = collect_real(db, FakeSource(clock, revised(closed_rows(), 10)), clock)
    assert result.outcome is SymbolOutcome.COLLECTED
    assert result.conflict_open_times == (older,)


def test_a_failing_write_that_stored_nothing_propagates_without_a_retry() -> None:
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    sink = ControlledSink(raises=[CandleWriteError("nothing was written")])
    with pytest.raises(CandleWriteError):
        collect_with(lambda: sink_of(sink), FakeSource(clock, closed_rows()), clock, sleep)
    assert len(sink.calls) == 1 and sleep.calls == []


def test_pending_target_is_retried_with_the_exact_waits(db: Path) -> None:
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    result = collect_real(db, FakeSource(clock, forming_rows()), clock, sleep, target=FORMING_TARGET)
    assert sleep.calls == WAITS
    assert result.attempts == 6 and result.outcome is SymbolOutcome.GAVE_UP
    assert result.last_problem == "target not confirmed (pending=1, invalid=0)"
    assert FORMING_TARGET not in stored_opens(db)


def test_pending_then_confirmable_stops_retrying(db: Path) -> None:
    clock = FakeClock()
    calls: list[int] = []
    source = FakeSource(clock, closed_rows()[:-1])

    def on_klines() -> None:
        calls.append(1)
        if len(calls) == 3:
            source.rows = closed_rows()

    source.on_klines = on_klines
    result = collect_real(db, source, clock)
    assert result.attempts == 3 and result.outcome is SymbolOutcome.COLLECTED


def test_untrusted_time_is_retried_then_gives_up(db: Path) -> None:
    clock = FakeClock()
    result = collect_real(db, FakeSource(clock, closed_rows(), time_error=SourceError("time down")), clock)
    assert result.outcome is SymbolOutcome.GAVE_UP and result.attempts == 6
    assert result.last_problem is not None and result.last_problem.startswith("time untrusted")
    assert stored_opens(db) == []


def test_a_server_time_in_seconds_from_the_binance_adapter_is_untrusted_not_fatal(db: Path) -> None:
    exchange = FakeExchange()
    exchange.time_response = {"serverTime": 1_700_000_000}  # seconds, not milliseconds
    clock = FakeClock()
    result = collect_real(db, open_binance_source(exchange), clock)  # no exception escapes
    assert result.outcome is SymbolOutcome.GAVE_UP and result.attempts == 6
    assert result.last_problem == "time untrusted: NO_OBSERVATION"
    assert stored_opens(db) == []


def malformed() -> list[list[Any]]:
    rows = closed_rows()
    rows[3][1] = 100.5
    return rows


@pytest.mark.parametrize(
    ("rows", "klines_error", "message"),
    [
        (closed_rows(), SourceError("klines down"), "klines down"),
        (malformed(), None, ""),
    ],
    ids=["source_error", "parse_error"],
)
def test_source_and_parse_errors_are_retried(
    db: Path, rows: list[list[Any]], klines_error: SourceError | None, message: str
) -> None:
    clock = FakeClock()
    result = collect_real(db, FakeSource(clock, rows, klines_error=klines_error), clock)
    assert result.outcome is SymbolOutcome.GAVE_UP and result.attempts == 6
    assert result.last_problem is not None and message in result.last_problem
    assert stored_opens(db) == []


def test_unknown_write_outcome_then_retry_returns_a_confirmed_result() -> None:
    clock = FakeClock()
    sink = ControlledSink(raises=[CandleWriteOutcomeUnknown("unknown"), None])
    watch = _WriteWatch(sink)
    result = collect_with(lambda: sink_of(watch), FakeSource(clock, closed_rows()), clock, AdvancingSleep(clock))
    assert result.outcome is SymbolOutcome.COLLECTED and result.attempts == 2
    assert watch.uncertain is True  # the confirmed target does not clear it


def test_unknown_write_outcome_with_a_real_writer_stores_each_row_once(db: Path) -> None:
    class SyncSink:
        def __init__(self, writer: CandleWriter) -> None:
            self.writer = writer

        async def write(self, candles: Sequence[ValidatedCandle], *, ingested_at_ms: int) -> Any:
            return self.writer.write(candles, ingested_at_ms=ingested_at_ms)

    conn = open_faulty_writer(db, AT)
    conn.faults.append(Fault("COMMIT", sqlite3.OperationalError("disk I/O error"), after_execution=True))
    clock = FakeClock()
    sink = SyncSink(CandleWriter(conn))
    result = collect_with(lambda: sink_of(sink), FakeSource(clock, closed_rows()), clock, AdvancingSleep(clock))
    conn.close()
    assert result.outcome is SymbolOutcome.COLLECTED and result.attempts == 2
    assert stored_opens(db) == sorted(int(r[0]) for r in closed_rows())


def test_every_attempt_unknown_is_gave_up_and_flagged() -> None:
    clock = FakeClock()
    sink = ControlledSink(raises=[CandleWriteOutcomeUnknown("unknown")] * 6)
    watch = _WriteWatch(sink)
    result = collect_with(lambda: sink_of(watch), FakeSource(clock, closed_rows()), clock, AdvancingSleep(clock))
    assert result.outcome is SymbolOutcome.GAVE_UP and result.attempts == 6
    assert watch.uncertain is True


def test_other_errors_propagate_without_a_retry() -> None:
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    with pytest.raises(RuntimeError):
        collect_with(
            lambda: sink_of(ControlledSink()),
            FakeSource(clock, closed_rows(), klines_error=RuntimeError("bug")),
            clock,
            sleep,
        )
    assert sleep.calls == []


def test_nothing_expected_makes_one_attempt() -> None:
    clock = FakeClock()
    source = FakeSource(clock, closed_rows())
    result = collect_with(lambda: sink_of(ControlledSink()), source, clock, AdvancingSleep(clock), target=None)
    assert result.outcome is SymbolOutcome.NOTHING_EXPECTED and result.attempts == 1
    assert len(source.kline_calls) == 1


def test_stop_before_the_first_attempt() -> None:
    clock = FakeClock()
    source = FakeSource(clock, closed_rows())
    stop = asyncio.Event()
    stop.set()
    result = collect_with(lambda: sink_of(ControlledSink()), source, clock, AdvancingSleep(clock), stop=stop)
    assert result.outcome is SymbolOutcome.STOPPED and result.attempts == 0
    assert source.kline_calls == []


def test_stop_during_the_retry_wait() -> None:
    clock = FakeClock()
    stop = asyncio.Event()
    sleep = AdvancingSleep(clock, on_sleep=lambda ms: stop.set())
    source = FakeSource(clock, closed_rows()[:-1])
    result = collect_with(lambda: sink_of(ControlledSink()), source, clock, sleep, stop=stop)
    assert result.outcome is SymbolOutcome.STOPPED and result.attempts == 1
    assert len(source.kline_calls) == 1


def test_waits_leave_no_task_behind() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        stop = asyncio.Event()
        sleep = AdvancingSleep(clock, on_sleep=lambda ms: stop.set())
        await collect_symbol(
            FakeSource(clock, closed_rows()[:-1]), ControlledSink(), cfg(), TARGET, RETRY, clock, sleep, stop
        )
        await collect_symbol(
            FakeSource(clock, closed_rows()[:-1]),
            ControlledSink(),
            cfg(),
            TARGET,
            RETRY,
            clock,
            AdvancingSleep(clock),
            asyncio.Event(),
        )
        await assert_no_leftover_tasks()

    asyncio.run(scenario())


def test_write_watch_flags_unknown_outcome_and_cancellation_and_never_clears() -> None:
    async def scenario() -> None:
        unknown = _WriteWatch(ControlledSink(raises=[CandleWriteOutcomeUnknown("x"), None]))
        with pytest.raises(CandleWriteOutcomeUnknown):
            await unknown.write((), ingested_at_ms=AT)
        assert unknown.uncertain is True
        await unknown.write((), ingested_at_ms=AT)
        assert unknown.uncertain is True  # a later returned write does not clear it

        blocked = ControlledSink(block=True)
        cancelled = _WriteWatch(blocked)
        task = asyncio.create_task(cancelled.write((), ingested_at_ms=AT))
        await blocked.write_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.uncertain is True

        plain = _WriteWatch(ControlledSink(raises=[CandleWriteError("nothing written")]))
        with pytest.raises(CandleWriteError):
            await plain.write((), ingested_at_ms=AT)
        assert plain.uncertain is False

    asyncio.run(scenario())


def test_retry_policy_validation_and_delays() -> None:
    policy = RetryPolicy()
    assert [policy.delay_ms(n) for n in range(1, 6)] == WAITS
    bad_types: list[dict[str, Any]] = [{"max_attempts": True}, {"initial_delay_ms": 1.5}, {"max_delay_ms": "8"}]
    for kwargs in bad_types:
        with pytest.raises(TypeError):
            RetryPolicy(**kwargs)
    bad_values: list[dict[str, Any]] = [
        {"max_attempts": 0},
        {"initial_delay_ms": -1},
        {"initial_delay_ms": 5, "max_delay_ms": 4},
    ]
    for kwargs in bad_values:
        with pytest.raises(ValueError):
            RetryPolicy(**kwargs)
    assert replace(policy, max_attempts=1).max_attempts == 1
