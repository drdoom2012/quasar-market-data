import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import pytest

from quasar_market_data.collector.async_open_interest_writer import (
    AsyncOpenInterestWriter,
    OpenInterestPageError,
    OpenInterestWriteReport,
)
from quasar_market_data.collector.cli import main
from quasar_market_data.collector.clock import Clock, Sleep
from quasar_market_data.collector.fetch_open_interest_once import OpenInterestOnceSource
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.collector.open_interest_gate import OpenInterestBanned, OpenInterestPaused
from quasar_market_data.collector.open_interest_writer import (
    OpenInterestWriteBusy,
    OpenInterestWriteError,
    OpenInterestWriteOutcomeUnknown,
)
from quasar_market_data.domain.open_interest import OpenInterest
from quasar_market_data.storage.schema import SCHEMA_VERSION
from tests.collector.fetch_fakes import SYMBOL, FakeClock
from tests.collector.open_interest_fakes import STEP, FakeOiSource, default_values

PARTIAL = OpenInterestWriteReport(100, 0, (), (), (), 0, 0)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "market.db"


class WriterCloseSpy:
    closed = 0


@pytest.fixture(autouse=True)
def writer_close(monkeypatch: pytest.MonkeyPatch) -> WriterCloseSpy:
    spy = WriterCloseSpy()
    original = AsyncOpenInterestWriter.close

    async def close(self: AsyncOpenInterestWriter) -> None:
        spy.closed += 1
        await original(self)

    monkeypatch.setattr(AsyncOpenInterestWriter, "close", close)
    return spy


def opener(
    source: FakeOiSource, seen: list[tuple[Clock, Sleep]] | None = None
) -> Callable[[Clock, Sleep], Awaitable[OpenInterestOnceSource]]:
    async def open_oi_source(clock: Clock, sleep: Sleep) -> OpenInterestOnceSource:
        if seen is not None:
            seen.append((clock, sleep))
        return source

    return open_oi_source


def run_cli(db: Path, source: FakeOiSource, clock: FakeClock, *extra: str) -> int:
    argv = ["fetch-oi-once", "--db", str(db), "--symbol", SYMBOL, *extra]
    return main(argv, open_oi_source=opener(source), clock=clock)


def stored(db: Path) -> list[tuple[int, str, str]]:
    conn = sqlite3.connect(db)
    try:
        return [
            (int(t), str(oi), str(value))
            for t, oi, value in conn.execute(
                "SELECT time_ms, open_interest, open_interest_value FROM open_interest ORDER BY time_ms"
            )
        ]
    finally:
        conn.close()


def conflict_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT count(*) FROM open_interest_conflicts").fetchone()[0])
    finally:
        conn.close()


def test_stores_the_newest_eligible_rows_and_exits_zero(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    source = FakeOiSource(clock)
    assert run_cli(db, source, clock) == 0
    rows = stored(db)
    assert [t for t, _, _ in rows] == [source.hist_calls[0][1] + i * STEP for i in range(3)]
    assert rows[0][1:] == default_values(rows[0][0])
    out = capsys.readouterr().out.splitlines()
    start, end = source.hist_calls[0][1:3]
    assert out == [
        f"symbol={SYMBOL} period=5m start_ms={start} end_ms={end} fetched=3",
        "time=trusted",
        "inserted=3 duplicates=0 conflicts=0 pending=0",
    ]


def test_a_second_identical_run_adds_nothing(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    run_cli(db, FakeOiSource(clock), clock)
    capsys.readouterr()
    assert run_cli(db, FakeOiSource(clock), clock) == 0
    assert capsys.readouterr().out.splitlines()[2] == "inserted=0 duplicates=3 conflicts=0 pending=0"
    assert len(stored(db)) == 3 and conflict_count(db) == 0


def test_a_differing_redelivery_exits_three_and_never_overwrites(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    first = FakeOiSource(clock)
    run_cli(db, first, clock)
    before = stored(db)
    capsys.readouterr()
    changed_at = before[1][0]
    revised = FakeOiSource(clock, values=lambda t: ("9.5", "9.25") if t == changed_at else default_values(t))
    assert run_cli(db, revised, clock) == 3
    assert capsys.readouterr().out.splitlines()[2] == "inserted=0 duplicates=2 conflicts=1 pending=0"
    assert stored(db) == before
    assert conflict_count(db) == 1


def test_a_row_inside_the_publication_delay_is_pending(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    clock.advance_wall(-30_000)
    assert run_cli(db, FakeOiSource(clock), clock) == 0
    assert capsys.readouterr().out.splitlines()[2] == "inserted=2 duplicates=0 conflicts=0 pending=1"
    assert len(stored(db)) == 2


def test_untrusted_time_stores_nothing_and_exits_one(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    source = FakeOiSource(clock, time_error=SourceError("time request failed"))
    assert run_cli(db, source, clock) == 1
    captured = capsys.readouterr()
    assert "untrusted" in captured.err and "nothing was written" in captured.err
    assert captured.out.splitlines()[0].endswith("start_ms=none end_ms=none fetched=0")
    assert source.hist_calls == [] and stored(db) == []


@pytest.mark.parametrize("periods", ["0", "501", "x", "-1"])
def test_periods_outside_1_to_500_is_a_usage_error(db: Path, periods: str) -> None:
    clock = FakeClock()
    with pytest.raises(SystemExit) as raised:
        run_cli(db, FakeOiSource(clock), clock, "--periods", periods)
    assert raised.value.code == 2


def test_500_periods_are_one_request(db: Path) -> None:
    clock = FakeClock()
    source = FakeOiSource(clock)
    assert run_cli(db, source, clock, "--periods", "500") == 0
    assert len(source.hist_calls) == 1 and len(stored(db)) == 500


def test_a_missing_database_directory_fails_before_anything_else(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    source = FakeOiSource(clock)
    assert run_cli(tmp_path / "missing" / "market.db", source, clock) == 1
    assert "does not exist" in capsys.readouterr().err
    assert source.time_calls == 0


def test_the_database_is_migrated_before_the_first_request(db: Path) -> None:
    clock = FakeClock()
    seen: list[int] = []

    async def open_oi_source(_: Clock, __: Sleep) -> OpenInterestOnceSource:
        conn = sqlite3.connect(db)
        try:
            seen.append(int(conn.execute("SELECT count(*) FROM open_interest").fetchone()[0]))
        finally:
            conn.close()
        return FakeOiSource(clock)

    argv = ["fetch-oi-once", "--db", str(db), "--symbol", SYMBOL]
    assert main(argv, open_oi_source=open_oi_source, clock=clock) == 0
    assert seen == [0]
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_the_factory_gets_the_injected_clock_and_sleep(db: Path) -> None:
    clock = FakeClock()
    seen: list[tuple[Clock, Sleep]] = []

    async def sleep(ms: int) -> None:
        return None

    argv = ["fetch-oi-once", "--db", str(db), "--symbol", SYMBOL]
    assert main(argv, open_oi_source=opener(FakeOiSource(clock), seen), clock=clock, sleep=sleep) == 0
    assert seen == [(clock, sleep)]


@pytest.mark.parametrize(
    "error",
    [SourceError("boom"), OpenInterestPaused(300_000), OpenInterestBanned()],
    ids=["source", "paused", "banned"],
)
def test_a_source_failure_exits_one_with_nothing_written(
    db: Path, capsys: pytest.CaptureFixture[str], error: SourceError
) -> None:
    clock = FakeClock()
    assert run_cli(db, FakeOiSource(clock, hist_error=error), clock) == 1
    assert capsys.readouterr().err.startswith("error:")
    assert stored(db) == []


def test_a_rejected_response_exits_one_with_nothing_written(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    assert run_cli(db, FakeOiSource(clock, symbol_in_rows="ETHUSDT"), clock) == 1
    assert capsys.readouterr().err.startswith("error:")
    assert stored(db) == []


def failing_write(monkeypatch: pytest.MonkeyPatch, error: OpenInterestWriteError) -> None:
    async def write(self: AsyncOpenInterestWriter, rows: object, *, ingested_at_ms: int) -> OpenInterestWriteReport:
        raise error

    monkeypatch.setattr(AsyncOpenInterestWriter, "write", write)


def test_an_unknown_write_outcome_exits_four(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failing_write(monkeypatch, OpenInterestWriteOutcomeUnknown("commit failed"))
    clock = FakeClock()
    assert run_cli(db, FakeOiSource(clock), clock) == 4
    assert "unknown" in capsys.readouterr().err


def test_an_unknown_outcome_inside_a_page_error_exits_four(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failing_write(monkeypatch, OpenInterestPageError(OpenInterestWriteOutcomeUnknown("commit failed"), 0, PARTIAL))
    clock = FakeClock()
    assert run_cli(db, FakeOiSource(clock), clock) == 4


def test_a_page_that_failed_after_a_committed_transaction_exits_four_and_names_the_count(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failing_write(monkeypatch, OpenInterestPageError(OpenInterestWriteBusy("locked"), 1, PARTIAL))
    clock = FakeClock()
    assert run_cli(db, FakeOiSource(clock), clock, "--periods", "150") == 4
    err = capsys.readouterr().err
    assert "1 transaction" in err and "re-run" in err


def test_a_page_that_failed_in_its_first_transaction_exits_one(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failing_write(monkeypatch, OpenInterestPageError(OpenInterestWriteBusy("locked"), 0, PARTIAL))
    clock = FakeClock()
    assert run_cli(db, FakeOiSource(clock), clock) == 1
    assert capsys.readouterr().err.startswith("error:")


def test_a_real_page_failing_after_the_first_transaction_leaves_it_stored_and_exits_four(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quasar_market_data.collector import open_interest_writer as module

    real = module.OpenInterestWriter.write_transaction
    calls = {"n": 0}

    def write_transaction(
        self: module.OpenInterestWriter, rows: Sequence[OpenInterest], *, ingested_at_ms: int
    ) -> object:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OpenInterestWriteBusy("locked")
        return real(self, rows, ingested_at_ms=ingested_at_ms)

    monkeypatch.setattr(module.OpenInterestWriter, "write_transaction", write_transaction)
    clock = FakeClock()
    assert run_cli(db, FakeOiSource(clock), clock, "--periods", "150") == 4
    assert len(stored(db)) == 100  # the first transaction stays committed


def test_an_interrupt_exits_130(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    assert run_cli(db, FakeOiSource(clock, hist_error=KeyboardInterrupt()), clock) == 130
    assert "interrupted" in capsys.readouterr().err


@pytest.mark.parametrize("fails", [False, True])
def test_the_source_and_the_writer_are_closed_on_every_path(
    db: Path, writer_close: WriterCloseSpy, fails: bool
) -> None:
    clock = FakeClock()
    source = FakeOiSource(clock, hist_error=SourceError("boom") if fails else None)
    run_cli(db, source, clock)
    assert source.closed
    assert writer_close.closed == 1
