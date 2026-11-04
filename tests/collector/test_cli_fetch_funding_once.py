import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from quasar_market_data.collector.async_funding_rate_writer import (
    AsyncFundingRateWriter,
    FundingRatePageError,
    FundingRateWriteReport,
)
from quasar_market_data.collector.binance_usdm_funding_rate import FundingRateRequestRefused
from quasar_market_data.collector.cli import main
from quasar_market_data.collector.clock import Clock
from quasar_market_data.collector.extension_pause import ExtensionPause, ExtensionPaused, ExtensionStopped
from quasar_market_data.collector.fetch_funding_rate_once import FundingRateOnceSource
from quasar_market_data.collector.funding_rate_limiter import FundingRateLimiter, RequestClass
from quasar_market_data.collector.funding_rate_writer import (
    FundingRateWriteBusy,
    FundingRateWriteError,
    FundingRateWriteOutcomeUnknown,
)
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.storage.schema import SCHEMA_VERSION
from tests.collector.fetch_fakes import SYMBOL, FakeClock
from tests.collector.funding_rate_fakes import HOUR, FakeFundingSource, settlement_rows

PARTIAL = FundingRateWriteReport(100, 0, (), (), (), 0, 0, (), None, None)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "market.db"


class WriterCloseSpy:
    closed = 0


@pytest.fixture(autouse=True)
def writer_close(monkeypatch: pytest.MonkeyPatch) -> WriterCloseSpy:
    spy = WriterCloseSpy()
    original = AsyncFundingRateWriter.close

    async def close(self: AsyncFundingRateWriter) -> None:
        spy.closed += 1
        await original(self)

    monkeypatch.setattr(AsyncFundingRateWriter, "close", close)
    return spy


def opener(
    source: FakeFundingSource, seen: list[tuple[FundingRateLimiter, ExtensionPause, Clock]] | None = None
) -> Callable[[FundingRateLimiter, ExtensionPause, Clock], Awaitable[FundingRateOnceSource]]:
    async def open_funding_source(
        limiter: FundingRateLimiter, pause: ExtensionPause, clock: Clock
    ) -> FundingRateOnceSource:
        if seen is not None:
            seen.append((limiter, pause, clock))
        return source

    return open_funding_source


def run_cli(db: Path, source: FakeFundingSource, clock: FakeClock, *extra: str) -> int:
    argv = ["fetch-funding-once", "--db", str(db), "--symbol", SYMBOL, *extra]
    return main(argv, open_funding_source=opener(source), clock=clock)


def stored(db: Path) -> list[tuple[int, str, str | None]]:
    conn = sqlite3.connect(db)
    try:
        return [
            (int(t), str(rate), mark)
            for t, rate, mark in conn.execute(
                "SELECT funding_time_ms, funding_rate, mark_price FROM funding_rates ORDER BY funding_time_ms, rate_type"
            )
        ]
    finally:
        conn.close()


def count(db: Path, table: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def test_stores_the_eligible_rows_prints_the_report_and_exits_zero(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    source = FakeFundingSource(clock)
    assert run_cli(db, source, clock) == 0
    (_, start, end, request_class) = source.hist_calls[0]
    assert request_class is RequestClass.R and source.time_calls == [RequestClass.R]
    rows = stored(db)
    assert rows and all(start <= t <= end for t, _, _ in rows)
    assert rows[0][1:] == ("0.0001", "67123.45")
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"symbol={SYMBOL} start_ms={start} end_ms={end} fetched={len(rows)}",
        "time=trusted",
        f"inserted={len(rows)} duplicates=0 conflicts=0 pending=0 off_hour_rows=0 full_page=false",
    ]
    assert count(db, "funding_rate_verified") == 0  # the command claims no coverage


def test_a_second_identical_run_adds_nothing(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    run_cli(db, FakeFundingSource(clock), clock)
    first = stored(db)
    capsys.readouterr()
    clock = FakeClock()
    assert run_cli(db, FakeFundingSource(clock), clock) == 0
    assert capsys.readouterr().out.splitlines()[2] == (
        f"inserted=0 duplicates={len(first)} conflicts=0 pending=0 off_hour_rows=0 full_page=false"
    )
    assert stored(db) == first and count(db, "funding_rate_conflicts") == 0


def test_a_differing_redelivery_exits_three_and_never_overwrites(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    run_cli(db, FakeFundingSource(clock), clock)
    before = stored(db)
    capsys.readouterr()
    clock = FakeClock()
    probe = FakeFundingSource(clock)
    revised = [dict(row) for row in probe.dataset]
    victim = before[1][0]
    for row in revised:
        if row["fundingTime"] == victim:
            row["fundingRate"] = "0.00090000"
    assert run_cli(db, FakeFundingSource(clock, dataset=revised), clock) == 3
    assert "conflicts=1 pending=0" in capsys.readouterr().out.splitlines()[2]
    assert stored(db) == before and count(db, "funding_rate_conflicts") == 1


def test_an_off_hour_row_is_stored_counted_and_warned_about(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    hour = clock.wall_ms() // HOUR * HOUR - 5 * HOUR
    row = settlement_rows(hour, hour)[0]
    source = FakeFundingSource(clock, dataset=[{**row, "fundingTime": hour + 1_800_000}])
    assert run_cli(db, source, clock) == 0
    captured = capsys.readouterr()
    assert "off_hour_rows=1" in captured.out.splitlines()[2]
    assert "warning: 1 off-hour row(s)" in captured.err and "stored" in captured.err
    assert len(stored(db)) == 1


def test_a_full_page_is_reported(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    hour = clock.wall_ms() // HOUR * HOUR - 5 * HOUR
    dataset = [{**settlement_rows(hour, hour)[0], "fundingTime": hour - i * 1_000} for i in range(1_000, 0, -1)]
    assert run_cli(db, FakeFundingSource(clock, dataset=dataset), clock, "--hours", "500") == 0
    assert capsys.readouterr().out.splitlines()[2].endswith("full_page=true")


def test_untrusted_time_stores_nothing_and_exits_one(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    source = FakeFundingSource(clock, time_error=SourceError("time request failed"))
    assert run_cli(db, source, clock) == 1
    captured = capsys.readouterr()
    assert "untrusted" in captured.err and "nothing was written" in captured.err
    assert captured.out.splitlines()[0].endswith("start_ms=none end_ms=none fetched=0")
    assert source.hist_calls == [] and stored(db) == []


@pytest.mark.parametrize("hours", ["0", "501", "x", "-1"])
def test_hours_outside_1_to_500_is_a_usage_error(db: Path, hours: str) -> None:
    clock = FakeClock()
    with pytest.raises(SystemExit) as raised:
        run_cli(db, FakeFundingSource(clock), clock, "--hours", hours)
    assert raised.value.code == 2


@pytest.mark.parametrize("delay", ["-1", "595001", "x"])
def test_a_publication_delay_outside_0_to_595000_is_a_usage_error(db: Path, delay: str) -> None:
    clock = FakeClock()
    with pytest.raises(SystemExit) as raised:
        run_cli(db, FakeFundingSource(clock), clock, "--publication-delay-ms", delay)
    assert raised.value.code == 2


def test_a_missing_symbol_is_a_usage_error(db: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["fetch-funding-once", "--db", str(db)], clock=FakeClock())
    assert raised.value.code == 2


def test_500_hours_are_one_request(db: Path) -> None:
    clock = FakeClock()
    source = FakeFundingSource(clock)
    assert run_cli(db, source, clock, "--hours", "500") == 0
    assert len(source.hist_calls) == 1 and len(stored(db)) >= 500


def test_a_missing_database_directory_fails_before_anything_else(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    source = FakeFundingSource(clock)
    assert run_cli(tmp_path / "missing" / "market.db", source, clock) == 1
    assert "does not exist" in capsys.readouterr().err
    assert source.time_calls == []


def test_the_database_is_migrated_before_the_first_request(db: Path) -> None:
    clock = FakeClock()
    seen: list[int] = []

    async def open_funding_source(_: FundingRateLimiter, __: ExtensionPause, ___: Clock) -> FundingRateOnceSource:
        conn = sqlite3.connect(db)
        try:
            seen.append(int(conn.execute("SELECT count(*) FROM funding_rates").fetchone()[0]))
        finally:
            conn.close()
        return FakeFundingSource(clock)

    argv = ["fetch-funding-once", "--db", str(db), "--symbol", SYMBOL]
    assert main(argv, open_funding_source=open_funding_source, clock=clock) == 0
    assert seen == [0]
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_the_factory_gets_a_funding_limiter_with_b_150_a_pause_and_the_injected_clock(db: Path) -> None:
    clock = FakeClock()
    seen: list[tuple[FundingRateLimiter, ExtensionPause, Clock]] = []

    async def sleep(ms: int) -> None:
        return None

    argv = ["fetch-funding-once", "--db", str(db), "--symbol", SYMBOL]
    assert main(argv, open_funding_source=opener(FakeFundingSource(clock), seen), clock=clock, sleep=sleep) == 0
    ((limiter, pause, received),) = seen
    assert isinstance(limiter, FundingRateLimiter) and isinstance(pause, ExtensionPause) and received is clock
    assert limiter.starts_in_window(0) == 0 and pause.stopped is False
    # b = 150 (not the open interest 400): the round group holds m = 75 starts per window, the other group 75
    assert limiter._rounds.cap == 75 and limiter._other.cap == 75


@pytest.mark.parametrize(
    "error",
    [SourceError("boom"), ExtensionPaused(300_000, 1), ExtensionStopped()],
    ids=["source", "paused", "stopped"],
)
def test_a_source_failure_exits_one_with_nothing_written(
    db: Path, capsys: pytest.CaptureFixture[str], error: SourceError
) -> None:
    clock = FakeClock()
    assert run_cli(db, FakeFundingSource(clock, hist_error=error), clock) == 1
    assert capsys.readouterr().err.startswith("error:")
    assert stored(db) == []


@pytest.mark.parametrize("error", [ExtensionPaused(300_000, 1), ExtensionStopped()], ids=["paused", "stopped"])
def test_a_429_or_418_on_the_time_request_is_reported_as_such_not_as_untrusted_time(
    db: Path, capsys: pytest.CaptureFixture[str], error: SourceError
) -> None:
    clock = FakeClock()
    source = FakeFundingSource(clock, time_error=error)
    assert run_cli(db, source, clock) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "untrusted" not in err and str(error) in err
    assert source.hist_calls == [] and stored(db) == []


def test_an_inactive_symbol_exits_one_before_any_request(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    source = FakeFundingSource(clock, market_error=FundingRateRequestRefused("the market is not active"))
    assert run_cli(db, source, clock) == 1
    assert capsys.readouterr().err.startswith("error:") and source.time_calls == [] and stored(db) == []


def test_a_rejected_response_exits_one_with_nothing_written(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    source = FakeFundingSource(clock, response={"code": -1130, "msg": "Invalid data sent for parameter 'startTime'."})
    assert run_cli(db, source, clock) == 1
    assert capsys.readouterr().err.startswith("error:")
    assert stored(db) == []


def failing_write(monkeypatch: pytest.MonkeyPatch, error: FundingRateWriteError) -> None:
    async def write(
        self: AsyncFundingRateWriter, rows: object, *, ingested_at_ms: int, merge: object = None
    ) -> FundingRateWriteReport:
        raise error

    monkeypatch.setattr(AsyncFundingRateWriter, "write", write)


def test_an_unknown_outcome_inside_a_page_error_exits_four(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failing_write(monkeypatch, FundingRatePageError(FundingRateWriteOutcomeUnknown("commit failed"), 0, PARTIAL))
    clock = FakeClock()
    assert run_cli(db, FakeFundingSource(clock), clock) == 4


def test_a_page_that_failed_after_a_committed_transaction_exits_four_and_names_the_count(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failing_write(monkeypatch, FundingRatePageError(FundingRateWriteBusy("locked"), 1, PARTIAL))
    clock = FakeClock()
    assert run_cli(db, FakeFundingSource(clock), clock) == 4
    err = capsys.readouterr().err
    assert "1 transaction(s) committed" in err and "re-run" in err


def test_a_page_that_failed_before_any_commit_exits_one(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failing_write(monkeypatch, FundingRatePageError(FundingRateWriteBusy("locked"), 0, PARTIAL))
    clock = FakeClock()
    assert run_cli(db, FakeFundingSource(clock), clock) == 1
    assert capsys.readouterr().err.startswith("error:")


def test_the_writer_is_closed_after_a_normal_run(db: Path, writer_close: WriterCloseSpy) -> None:
    clock = FakeClock()
    assert run_cli(db, FakeFundingSource(clock), clock) == 0
    assert writer_close.closed == 1


def test_the_source_is_closed_and_an_interrupt_exits_130(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    source = FakeFundingSource(clock, hist_error=KeyboardInterrupt())
    assert run_cli(db, source, clock) == 130
    assert source.closed
    assert "interrupted" in capsys.readouterr().err


def test_the_command_passes_no_merge_to_the_writer(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []
    original = AsyncFundingRateWriter.write

    async def write(
        self: AsyncFundingRateWriter, rows: object, *, ingested_at_ms: int, merge: object = None
    ) -> FundingRateWriteReport:
        seen.append(merge)
        return await original(self, rows, ingested_at_ms=ingested_at_ms, merge=merge)  # type: ignore[arg-type]

    monkeypatch.setattr(AsyncFundingRateWriter, "write", write)
    clock = FakeClock()
    assert run_cli(db, FakeFundingSource(clock), clock) == 0
    assert seen == [None]
