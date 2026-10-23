import os
import signal
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector import cli
from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.candle_writer import (
    CandleWriteError,
    CandleWriteOutcomeUnknown,
    CandleWriter,
    WriteReport,
)
from quasar_market_data.collector.cli import main
from quasar_market_data.collector.kline_source import KlineSource, SourceError
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import MigrationError, migrate
from tests.collector.fetch_fakes import SYMBOL, FakeClock, FakeSource, closed_rows, forming_rows
from tests.collector.test_candle_writer import AT

UNKNOWN_FRAGMENTS = ("unknown", "re-run")


class WriterCloseSpy:
    def __init__(self) -> None:
        self.closed = 0
        self.error: Exception | None = None


@pytest.fixture(autouse=True)
def writer_close(monkeypatch: pytest.MonkeyPatch) -> WriterCloseSpy:
    spy = WriterCloseSpy()
    original = AsyncCandleWriter.close

    async def close(self: AsyncCandleWriter) -> None:
        spy.closed += 1
        await original(self)  # the real connection and thread are always released
        if spy.error is not None:
            raise spy.error

    monkeypatch.setattr(AsyncCandleWriter, "close", close)
    return spy


@pytest.fixture
def migration_connections(monkeypatch: pytest.MonkeyPatch) -> list[sqlite3.Connection]:
    connections: list[sqlite3.Connection] = []
    real = open_writer

    def spy(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(cli, "open_writer", spy)
    return connections


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "market.db"


def opener(source: FakeSource, opened: list[FakeSource] | None = None) -> Callable[[], Awaitable[KlineSource]]:
    async def open_source() -> KlineSource:
        if opened is not None:
            opened.append(source)
        return source

    return open_source


def argv(db: Path, *extra: str) -> list[str]:
    return ["fetch-once", "--db", str(db), "--symbol", SYMBOL, *extra]


def run_cli(db: Path, source: FakeSource, clock: FakeClock, *extra: str, opened: list[FakeSource] | None = None) -> int:
    return main(argv(db, *extra), open_source=opener(source, opened), clock=clock)


def candle_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT count(*) FROM candles").fetchone()[0])
    finally:
        conn.close()


def test_fetch_once_stores_closed_candles_and_exits_zero(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    assert run_cli(db, FakeSource(clock, forming_rows()), clock) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "symbol=BTC/USDT:USDT timeframe=5m fetched=2",
        "time=trusted",
        "inserted=1 duplicates=0 conflicts=0 pending=1 invalid=0",
    ]
    assert candle_count(db) == 1


def test_second_run_reuses_the_database_and_reports_duplicates(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    run_cli(db, FakeSource(clock, forming_rows()), clock)
    capsys.readouterr()
    assert run_cli(db, FakeSource(clock, forming_rows()), clock) == 0
    assert capsys.readouterr().out.splitlines()[2] == "inserted=0 duplicates=1 conflicts=0 pending=1 invalid=0"
    assert candle_count(db) == 1


def test_conflict_exits_three_and_still_stores_the_rest(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    run_cli(db, FakeSource(clock, closed_rows()), clock)
    capsys.readouterr()
    revised = closed_rows()
    revised[10][4] = revised[10][3] if revised[10][3] != revised[10][4] else revised[10][2]

    assert run_cli(db, FakeSource(clock, revised), clock) == 3

    assert capsys.readouterr().out.splitlines()[2] == "inserted=0 duplicates=99 conflicts=1 pending=0 invalid=0"
    assert candle_count(db) == 100


def test_invalid_candle_exits_three(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows = closed_rows()
    rows[5][2] = "1"
    clock = FakeClock()
    assert run_cli(db, FakeSource(clock, rows), clock) == 3
    assert capsys.readouterr().out.splitlines()[2] == "inserted=99 duplicates=0 conflicts=0 pending=0 invalid=1"
    assert candle_count(db) == 99


def failing_source(kind: str, clock: FakeClock) -> FakeSource:
    if kind == "untrusted_time":
        return FakeSource(clock, closed_rows(), rtt_ms=3000)
    if kind == "source_error":
        return FakeSource(clock, closed_rows(), klines_error=SourceError("klines endpoint down"))
    if kind == "malformed_batch":
        rows = closed_rows()
        rows[3][1] = 100.5
        return FakeSource(clock, rows)
    return FakeSource(clock, closed_rows())


@pytest.mark.parametrize(
    "kind",
    ["untrusted_time", "source_error", "malformed_batch", "certain_write_error"],
)
def test_failures_exit_one_when_nothing_was_written(
    db: Path, kind: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if kind == "certain_write_error":

        async def refuse(self: AsyncCandleWriter, *args: object, **kwargs: object) -> WriteReport:
            raise CandleWriteError("batch rolled back; nothing was written: refused")

        monkeypatch.setattr(AsyncCandleWriter, "write", refuse)
    clock = FakeClock()
    source = failing_source(kind, clock)
    opened: list[FakeSource] = []

    assert run_cli(db, source, clock, opened=opened) == 1

    captured = capsys.readouterr()
    assert len([line for line in captured.err.splitlines() if line]) == 1
    assert "Traceback" not in captured.err
    assert candle_count(db) == 0
    assert source.closed


def test_a_missing_database_directory_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    opened: list[FakeSource] = []
    target = tmp_path / "missing" / "market.db"
    assert run_cli(target, FakeSource(clock, closed_rows()), clock, opened=opened) == 1
    assert "does not exist" in capsys.readouterr().err
    assert not target.exists()
    assert opened == []


def test_a_schema_from_the_future_exits_one(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conn = open_writer(db)
    migrate(conn, applied_at_ms=AT)
    conn.execute(
        "INSERT INTO schema_migrations (version, name, checksum_sha256, applied_at_ms) VALUES (2, 'x', 'y', 1)"
    )
    conn.close()
    clock = FakeClock()
    opened: list[FakeSource] = []

    assert run_cli(db, FakeSource(clock, closed_rows()), clock, opened=opened) == 1

    err = capsys.readouterr().err
    assert "newer than the code" in err and "Traceback" not in err
    assert candle_count(db) == 0
    assert opened == []


def patch_write_to_commit_then_report_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    real = CandleWriter.write

    def write_then_raise(self: CandleWriter, candles: Any, *, ingested_at_ms: int) -> WriteReport:
        real(self, candles, ingested_at_ms=ingested_at_ms)  # the commit took effect ...
        raise CandleWriteOutcomeUnknown("batch outcome unknown: simulated")  # ... but the writer cannot tell

    monkeypatch.setattr(CandleWriter, "write", write_then_raise)


def test_unknown_write_outcome_exits_four_and_never_claims_nothing_was_stored(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], writer_close: WriterCloseSpy
) -> None:
    patch_write_to_commit_then_report_unknown(monkeypatch)
    clock = FakeClock()
    source = FakeSource(clock, closed_rows())

    assert run_cli(db, source, clock) == 4

    captured = capsys.readouterr()
    assert captured.out == ""  # no complete report exists, so no counts are printed
    assert all(fragment in captured.err for fragment in UNKNOWN_FRAGMENTS)
    assert "nothing was written" not in captured.err and "Traceback" not in captured.err
    assert candle_count(db) == 100  # the batch was in fact stored
    assert source.closed and writer_close.closed == 1


def test_rerunning_after_an_unknown_outcome_reconciles(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    with monkeypatch.context() as patched:
        patch_write_to_commit_then_report_unknown(patched)
        assert run_cli(db, FakeSource(clock, closed_rows()), clock) == 4
    capsys.readouterr()

    assert run_cli(db, FakeSource(clock, closed_rows()), clock) == 0

    assert capsys.readouterr().out.splitlines()[2] == "inserted=0 duplicates=100 conflicts=0 pending=0 invalid=0"
    assert candle_count(db) == 100


@pytest.mark.parametrize("step", ["migrate_fails", "open_source_fails", "open_writer_fails", "fetch_fails"])
def test_resources_are_closed_when_initialization_fails(
    db: Path,
    step: str,
    monkeypatch: pytest.MonkeyPatch,
    migration_connections: list[sqlite3.Connection],
    writer_close: WriterCloseSpy,
) -> None:
    clock = FakeClock()
    source = FakeSource(clock, closed_rows())
    open_source = opener(source)
    if step == "migrate_fails":

        def fail_migrate(*args: object, **kwargs: object) -> None:
            raise MigrationError("migration refused")

        monkeypatch.setattr(cli, "migrate", fail_migrate)
    elif step == "open_source_fails":

        async def fail_open() -> KlineSource:
            raise SourceError("cannot open the exchange client")

        open_source = fail_open
    elif step == "open_writer_fails":

        async def fail_writer_open(
            cls: type[AsyncCandleWriter], /, *args: object, **kwargs: object
        ) -> AsyncCandleWriter:
            raise CandleWriteError("cannot open the writer")

        monkeypatch.setattr(AsyncCandleWriter, "open", classmethod(fail_writer_open))
    else:
        source.klines_error = SourceError("klines endpoint down")

    assert main(argv(db), open_source=open_source, clock=clock) == 1

    assert migration_connections, "the migration connection was never opened"
    for connection in migration_connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")  # closed
    if step in ("open_writer_fails", "fetch_fails"):
        assert source.closed
    if step == "fetch_fails":
        assert writer_close.closed == 1
    if step != "fetch_fails":
        assert writer_close.closed == 0


@pytest.mark.parametrize("primary", ["complete_report", "source_error"])
@pytest.mark.parametrize("failing_close", ["source", "writer"])
def test_a_failing_close_does_not_skip_the_other_closes_or_change_a_settled_result(
    db: Path, primary: str, failing_close: str, writer_close: WriterCloseSpy, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    source = FakeSource(clock, closed_rows())
    if primary == "source_error":
        source.klines_error = SourceError("klines endpoint down")
    if failing_close == "source":

        async def broken_close() -> None:
            source.closed = True
            raise RuntimeError("socket already gone")

        source.close = broken_close  # type: ignore[method-assign]
    else:
        writer_close.error = CandleWriteError("closing the connection failed: x")  # the type a write failure has too

    exit_code = run_cli(db, source, clock)

    assert exit_code == (0 if primary == "complete_report" else 1)
    assert source.closed and writer_close.closed == 1  # neither close was skipped
    assert "warning: closing" in capsys.readouterr().err
    assert candle_count(db) == (100 if primary == "complete_report" else 0)


@pytest.mark.parametrize("situation", ["interrupted", "unknown_outcome"])
def test_a_failing_close_never_downgrades_an_unknown_outcome(
    db: Path,
    situation: str,
    monkeypatch: pytest.MonkeyPatch,
    writer_close: WriterCloseSpy,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = FakeClock()
    source = FakeSource(clock, closed_rows())
    if situation == "interrupted":
        source.klines_error = KeyboardInterrupt()
    else:
        patch_write_to_commit_then_report_unknown(monkeypatch)
    writer_close.error = CandleWriteError("closing the connection failed: x")

    exit_code = run_cli(db, source, clock)

    assert exit_code == (130 if situation == "interrupted" else 4)
    err = capsys.readouterr().err
    assert all(fragment in err for fragment in UNKNOWN_FRAGMENTS) or "unknown" in err
    assert "warning: closing" in err


def test_keyboard_interrupt_exits_130_and_closes_resources(
    db: Path, writer_close: WriterCloseSpy, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    source = FakeSource(clock, closed_rows(), klines_error=KeyboardInterrupt())

    assert run_cli(db, source, clock) == 130

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown" in captured.err and "Traceback" not in captured.err
    assert source.closed and writer_close.closed == 1


def test_interrupt_during_an_in_flight_write_waits_for_it_and_reports_unknown(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if threading.current_thread() is not threading.main_thread() or signal.getsignal(signal.SIGINT) is not (
        signal.default_int_handler
    ):
        pytest.skip("needs the main thread with the default SIGINT handler so that only asyncio handles the signal")
    started, release = threading.Event(), threading.Event()
    real = CandleWriter.write

    def held_write(self: CandleWriter, candles: Any, *, ingested_at_ms: int) -> WriteReport:
        started.set()
        assert release.wait(10)
        return real(self, candles, ingested_at_ms=ingested_at_ms)

    monkeypatch.setattr(CandleWriter, "write", held_write)
    moments: dict[str, float] = {}

    def interrupter() -> None:
        assert started.wait(10)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.2)
        moments["released"] = time.monotonic()
        release.set()

    helper = threading.Thread(target=interrupter)
    helper.start()
    clock = FakeClock()

    exit_code = run_cli(db, FakeSource(clock, closed_rows()), clock)
    moments["returned"] = time.monotonic()
    helper.join(10)

    assert exit_code == 130
    assert moments["returned"] >= moments["released"]  # shutdown waited for the write instead of racing it
    captured = capsys.readouterr()
    assert captured.out == "" and "unknown" in captured.err  # the cancelled caller never learned the result
    assert candle_count(db) == 100  # the write that was still running did commit
    assert not [t for t in threading.enumerate() if t.name.startswith("candle-writer")]
    other = sqlite3.connect(db, timeout=0, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")  # no write lock left behind
        other.execute("ROLLBACK")
    finally:
        other.close()


@pytest.mark.parametrize(
    "extra",
    [["--limit", "0"], ["--limit", "1001"], ["--publication-delay-ms", "-1"], ["--limit", "abc"]],
    ids=["limit_zero", "limit_too_large", "negative_delay", "limit_not_a_number"],
)
def test_usage_errors_exit_two(db: Path, extra: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv(db, *extra))
    assert raised.value.code == 2


def test_the_required_arguments_are_enforced() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["fetch-once", "--symbol", SYMBOL])
    assert raised.value.code == 2
