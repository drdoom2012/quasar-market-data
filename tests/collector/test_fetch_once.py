import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.binance_klines import KlineParseError
from quasar_market_data.collector.candle_writer import CandleWriteOutcomeUnknown, WriteReport
from quasar_market_data.collector.fetch_once import (
    MAX_LIMIT,
    FetchOnceConfig,
    FetchOnceReport,
    fetch_once,
)
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.rate_limiter import RateLimitedSource, RateLimiter
from quasar_market_data.collector.shared_server_time import ServerTimeProvider
from quasar_market_data.domain.backfill import PageKind, PagePlan, PageViolation
from quasar_market_data.domain.server_time import TimeRejection
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.fetch_fakes import (
    FIVE_MINUTES,
    ROW0_END_MS,
    SYMBOL,
    AdvancingSleep,
    ControlledSink,
    FakeClock,
    FakeProvider,
    FakeSource,
    closed_rows,
    forming_rows,
    kline_row,
)
from tests.collector.test_candle_writer import AT
from tests.typing_helpers import untyped


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "market.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=AT)
    conn.close()
    return path


def config(limit: int = 100) -> FetchOnceConfig:
    return FetchOnceConfig(symbol=SYMBOL, timeframe=FIVE_MINUTES, limit=limit)


def run(
    source: FakeSource,
    db: Path,
    clock: FakeClock,
    cfg: FetchOnceConfig | None = None,
    server_time: ServerTimeProvider | None = None,
) -> FetchOnceReport:
    async def scenario() -> FetchOnceReport:
        writer = await AsyncCandleWriter.open(db)
        try:
            return await fetch_once(source, writer, cfg or config(), clock, server_time=server_time)
        finally:
            await writer.close()

    return asyncio.run(scenario())


def query(db: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def stored_open_times(db: Path) -> list[int]:
    return [row[0] for row in query(db, "SELECT open_time_ms FROM candles ORDER BY open_time_ms")]


def test_closed_candles_are_stored_and_the_forming_one_is_not(db: Path) -> None:
    # INV-007, trusted case: the last row of the response is still forming and must never reach the table.
    clock = FakeClock()
    report = run(FakeSource(clock, forming_rows()), db, clock)

    assert report.fetched == 2
    assert len(report.selection.eligible) == 1
    assert len(report.selection.pending) == 1
    assert report.write is not None and report.write.inserted == 1
    assert stored_open_times(db) == [1792676700000]
    assert 1792677000000 not in stored_open_times(db)


@pytest.mark.parametrize(
    ("reason", "source_args"),
    [
        (TimeRejection.NO_OBSERVATION, {"time_error": SourceError("time endpoint down")}),
        (TimeRejection.RTT_TOO_LARGE, {"rtt_ms": 3000}),
    ],
    ids=["time_request_failed", "round_trip_too_slow"],
)
def test_untrusted_time_stores_nothing(db: Path, reason: TimeRejection, source_args: dict[str, Any]) -> None:
    clock = FakeClock()
    source = FakeSource(clock, closed_rows(), **source_args)  # every row would be eligible if time were trusted

    report = run(source, db, clock)

    assert stored_open_times(db) == []
    assert report.selection.untrusted_reason is reason
    assert len(report.selection.pending) == report.fetched == 100
    assert report.write is None
    assert len(source.kline_calls) == 1  # the selection stays the single gate (plan decision 2)


@pytest.mark.parametrize(
    ("margin_ms", "stored"), [(20, 0), (60, 1)], ids=["pending_at_send_time", "eligible_at_send_time"]
)
def test_eligibility_is_judged_at_the_request_time_not_after_the_response(
    db: Path, margin_ms: int, stored: int
) -> None:
    # The klines request is sent at the instant the time observation is received (age 0), so the bound is
    # S - 50 ms. The response then takes 10 s: a bound taken after it would confirm row 0 in both cases.
    clock = FakeClock()
    server_time = ROW0_END_MS + 15_000 + margin_ms
    source = FakeSource(
        clock, forming_rows(), server_time_ms=server_time, rtt_ms=100, on_klines=lambda: clock.advance(10_000)
    )

    run(source, db, clock)

    assert len(stored_open_times(db)) == stored


def test_malformed_batch_stores_nothing(db: Path) -> None:
    rows = closed_rows()
    rows[3][1] = 100.5  # a JSON number in a decimal-text slot
    clock = FakeClock()
    with pytest.raises(KlineParseError):
        run(FakeSource(clock, rows), db, clock)
    assert stored_open_times(db) == []


def test_source_failure_on_klines_propagates_and_stores_nothing(db: Path) -> None:
    clock = FakeClock()
    with pytest.raises(SourceError, match="klines down"):
        run(FakeSource(clock, closed_rows(), klines_error=SourceError("klines down")), db, clock)
    assert stored_open_times(db) == []


def test_an_unknown_write_outcome_propagates_unchanged(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unknown = CandleWriteOutcomeUnknown("outcome unknown")

    async def raise_unknown(self: AsyncCandleWriter, *args: object, **kwargs: object) -> WriteReport:
        raise unknown

    monkeypatch.setattr(AsyncCandleWriter, "write", raise_unknown)
    clock = FakeClock()
    with pytest.raises(CandleWriteOutcomeUnknown) as caught:
        run(FakeSource(clock, closed_rows()), db, clock)
    assert caught.value is unknown


def test_rerunning_is_idempotent_and_survives_a_restart(db: Path) -> None:
    clock = FakeClock()
    first = run(FakeSource(clock, closed_rows()), db, clock)
    before = query(db, "SELECT * FROM candles ORDER BY open_time_ms")

    second = run(FakeSource(clock, closed_rows()), db, clock)  # a new writer on the same file: a restart

    assert first.write is not None and second.write is not None
    assert (second.write.inserted, second.write.duplicates) == (0, first.write.inserted)
    assert query(db, "SELECT * FROM candles ORDER BY open_time_ms") == before


def test_a_revised_value_is_recorded_as_a_conflict_not_overwritten(db: Path) -> None:
    clock = FakeClock()
    run(FakeSource(clock, closed_rows()), db, clock)
    stored_close = query(db, "SELECT close FROM candles WHERE open_time_ms = ?".replace("?", str(closed_rows()[10][0])))

    revised = closed_rows()
    revised[10][4] = (
        revised[10][3] if revised[10][3] != revised[10][4] else revised[10][2]
    )  # a valid but different close
    assert revised[10][4] != closed_rows()[10][4]
    report = run(FakeSource(clock, revised), db, clock)

    assert report.write is not None and len(report.write.conflicts) == 1
    assert len(query(db, "SELECT * FROM candle_conflicts")) == 1
    assert (
        query(db, "SELECT close FROM candles WHERE open_time_ms = ?".replace("?", str(closed_rows()[10][0])))
        == stored_close
    )


def test_invalid_eligible_candle_is_reported_and_the_rest_is_stored(db: Path) -> None:
    rows = closed_rows()
    rows[5][2] = "1"  # high far below open, low and close
    clock = FakeClock()

    report = run(FakeSource(clock, rows), db, clock)

    assert len(report.selection.invalid) == 1
    assert report.write is not None and report.write.inserted == 99
    assert rows[5][0] not in stored_open_times(db)


def test_the_request_uses_the_configured_symbol_timeframe_and_limit(db: Path) -> None:
    clock = FakeClock()
    source = FakeSource(clock, forming_rows())
    run(source, db, clock, config(limit=2))
    assert source.kline_calls == [(SYMBOL, FIVE_MINUTES, 2)]


@pytest.mark.parametrize("limit", [0, MAX_LIMIT + 1, True, "5", None], ids=["zero", "too_large", "bool", "str", "none"])
def test_config_rejects_a_bad_limit(limit: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        FetchOnceConfig(symbol=SYMBOL, timeframe=FIVE_MINUTES, limit=untyped(limit))


@pytest.mark.parametrize("symbol", ["", 5, None], ids=["empty", "int", "none"])
def test_config_rejects_a_bad_symbol(symbol: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        FetchOnceConfig(symbol=untyped(symbol), timeframe=FIVE_MINUTES, limit=10)


# --- backfill pages (S16): judged before anything is selected or written --------------------------------------

TF_MS = FIVE_MINUTES.milliseconds


def page_rows() -> list[list[Any]]:
    return closed_rows()[10:20]


def page_plan(limit: int = 10) -> PagePlan:
    rows = closed_rows()
    return PagePlan(rows[10][0], rows[10 + limit - 1][0], limit)


def page_config(limit: int = 10) -> FetchOnceConfig:
    return FetchOnceConfig(symbol=SYMBOL, timeframe=FIVE_MINUTES, limit=limit, page=page_plan(limit))


def run_page(source: FakeSource, sink: ControlledSink, clock: FakeClock, cfg: FetchOnceConfig) -> FetchOnceReport:
    return asyncio.run(fetch_once(source, sink, cfg, clock))


def test_fetch_once_requests_the_page_range() -> None:
    clock = FakeClock()
    plan = page_plan()
    source = FakeSource(clock, page_rows())
    sink = ControlledSink()
    run_page(source, sink, clock, page_config())
    assert source.kline_ranges == [(plan.start_ms, plan.end_ms)]
    assert source.kline_calls == [(SYMBOL, FIVE_MINUTES, plan.limit)]


def _bad_pages() -> dict[str, tuple[list[list[Any]], type[Exception] | None, dict[str, Any]]]:
    good = page_rows()
    before = [closed_rows()[9], *good[:-1]]
    after = [*good[:-1], closed_rows()[20]]
    dup = [*good[:-1], good[-2]]
    desc = [*good[:-2], good[-1], good[-2]]
    mis = [*good[:-1], [good[-1][0] + 7, *good[-1][1:]]]
    too_many = [*good, closed_rows()[20]]
    one_bad = [*good[:4], kline_row(good[3][0] + 30 * TF_MS, TF_MS)]  # four valid rows, then one beyond the end
    parser = [*good[:-1], [*good[-1][:6], good[-1][6] + 1, *good[-1][7:]]]
    return {
        "row_before_start": (before, PageViolation, {}),
        "row_after_end": (after, PageViolation, {}),
        "duplicate": (dup, KlineParseError, {}),
        "descending": (desc, KlineParseError, {}),
        "misaligned": (mis, KlineParseError, {}),
        "more_than_limit": (too_many, PageViolation, {}),
        "one_bad_among_valid": (one_bad, PageViolation, {}),
        "parser_close_time": (parser, KlineParseError, {}),
        "untrusted_time": (good, None, {"rtt_ms": 3000}),
    }


@pytest.mark.parametrize("case", list(_bad_pages()))
def test_an_invalid_page_writes_nothing(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    rows, error, source_args = _bad_pages()[case]
    gate_calls: list[int] = []
    import quasar_market_data.collector.fetch_once as module
    from quasar_market_data.domain.closed_candles import select_closed_candles as real

    def spy(*args: Any, **kwargs: Any) -> Any:
        gate_calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "select_closed_candles", spy)
    clock = FakeClock()
    sink = ControlledSink()
    source = FakeSource(clock, rows, **source_args)
    if error is None:
        report = run_page(source, sink, clock, page_config())
        assert report.write is None
    else:
        with pytest.raises(error):
            run_page(source, sink, clock, page_config())
        assert gate_calls == []  # the page was refused before the closed-candle gate
    assert sink.write_calls == 0


def test_a_valid_page_still_goes_through_the_closed_candle_selection() -> None:
    clock = FakeClock()
    # server time just after row 12 closed: the later rows of the page are still pending
    server_time = closed_rows()[12][6] + 15_000 + 100
    source = FakeSource(clock, page_rows(), server_time_ms=server_time, rtt_ms=100)
    sink = ControlledSink()
    report = run_page(source, sink, clock, page_config())
    assert report.page is not None and report.page.kind is PageKind.FULL
    assert len(report.selection.pending) > 0
    assert sink.write_calls == 1
    assert {c.key.open_time_ms for c in sink.calls[0]} == {c.key.open_time_ms for c in report.selection.eligible}
    assert len(report.selection.eligible) + len(report.selection.pending) == 10


def test_a_page_with_a_limit_that_differs_from_its_plan_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        FetchOnceConfig(symbol=SYMBOL, timeframe=FIVE_MINUTES, limit=500, page=PagePlan(1, 2, 1000))
    with pytest.raises(TypeError):
        FetchOnceConfig(symbol=SYMBOL, timeframe=FIVE_MINUTES, limit=10, page=(1, 2, 10))  # type: ignore[arg-type]


def test_without_a_page_fetch_once_reports_no_page_outcome(db: Path) -> None:
    clock = FakeClock()
    source = FakeSource(clock, forming_rows())
    report = run(source, db, clock)
    assert report.page is None
    assert source.kline_ranges == [(None, None)]


def test_time_spent_waiting_for_the_rate_limiter_is_not_round_trip_time(db: Path) -> None:
    """ADR-0001 P-4 measures RTT from the send of the request. A queue in front of the limiter (many symbols, high
    concurrency) delays the send, not the exchange's answer, and must not make time untrusted."""
    clock = FakeClock()
    limiter = RateLimiter(3000, clock, AdvancingSleep(clock))  # a wait above max_rtt_ms (2000) for every call
    source = RateLimitedSource(FakeSource(clock, forming_rows(), rtt_ms=270), limiter)

    async def scenario() -> FetchOnceReport:
        await limiter.acquire()  # the previous grant: the time request below must wait 3000 ms for its slot
        writer = await AsyncCandleWriter.open(db)
        try:
            return await fetch_once(source, writer, config(), clock)
        finally:
            await writer.close()

    report = asyncio.run(scenario())
    assert report.selection.untrusted_reason is None
    assert len(report.selection.eligible) >= 1


def test_fetch_once_uses_the_provider_instead_of_asking_the_source(db: Path) -> None:
    clock = FakeClock()
    source, provider = FakeSource(clock, closed_rows()), FakeProvider(clock)
    report = run(source, db, clock, server_time=provider)
    assert source.time_calls == 0 and provider.calls == 1 and provider.sources == [source]
    assert report.selection.untrusted_reason is None and len(report.selection.eligible) > 0


def test_a_provider_returning_none_is_no_observation(db: Path) -> None:
    clock = FakeClock()
    report = run(FakeSource(clock, closed_rows()), db, clock, server_time=FakeProvider(clock, none_first=1))
    assert report.selection.untrusted_reason is TimeRejection.NO_OBSERVATION
    assert report.write is None and stored_open_times(db) == []


def test_without_a_provider_every_call_asks_the_source(db: Path) -> None:
    clock = FakeClock()
    source = FakeSource(clock, closed_rows())
    run(source, db, clock)
    run(source, db, clock)
    assert source.time_calls == 2
