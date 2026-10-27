import asyncio
import signal
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector import cli
from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.candle_writer import CandleWriteError, CandleWriteOutcomeUnknown, WriteReport
from quasar_market_data.collector.cli import main, run_exit_code
from quasar_market_data.collector.collection_loop import (
    FatalLoopError,
    LoopExit,
    RoundReport,
    SymbolOutcome,
    SymbolResult,
)
from quasar_market_data.collector.kline_source import KlineSource, SourceError
from tests.collector.fetch_fakes import (
    SYMBOL,
    AdvancingSleep,
    ControlledSink,
    FakeClock,
    FakeSource,
    forming_rows,
)
from tests.collector.manual_time import assert_no_leftover_tasks
from tests.collector.test_collection_loop import GRACE, Harness
from tests.collector.test_collection_loop import run as run_scenario

FIRST_TARGET = 1792676700000


class WriterCloseSpy:
    def __init__(self) -> None:
        self.error: Exception | None = None


@pytest.fixture(autouse=True)
def writer_close(monkeypatch: pytest.MonkeyPatch) -> WriterCloseSpy:
    spy = WriterCloseSpy()
    original = AsyncCandleWriter.close

    async def close(self: AsyncCandleWriter) -> None:
        await original(self)  # the real connection and thread are always released
        if spy.error is not None:
            raise spy.error

    monkeypatch.setattr(AsyncCandleWriter, "close", close)
    return spy


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
    return ["run", "--db", str(db), "--symbol", SYMBOL, "--max-concurrency", "1", *extra]


def run_cli(
    db: Path,
    source: FakeSource,
    clock: FakeClock,
    sleep: AdvancingSleep,
    *extra: str,
    opened: list[FakeSource] | None = None,
) -> int:
    return main(argv(db, *extra), open_source=opener(source, opened), clock=clock, sleep=sleep)


def candle_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT count(*) FROM candles").fetchone()[0])
    finally:
        conn.close()


def round_lines(out: str) -> list[str]:
    """The S14 round report, without the S15 reconcile lines around it."""
    return [line for line in out.splitlines() if not line.startswith(("reconcile", "  hole ", "  still_missing "))]


def raise_on_nth(n: int, sig: signal.Signals = signal.SIGTERM) -> Callable[[], None]:
    calls = [0]

    def hook() -> None:
        calls[0] += 1
        if calls[0] == n:
            signal.raise_signal(sig)

    return hook


def stop_on_sleep(clock: FakeClock, n: int = 1, sig: signal.Signals = signal.SIGTERM) -> AdvancingSleep:
    """A sleep that raises ``sig`` on its ``n``-th call. Time here advances at once, so a stop is delivered while the
    loop is waiting (between rounds or before a retry), never while a write is in flight."""
    hook = raise_on_nth(n, sig)
    return AdvancingSleep(clock, on_sleep=lambda ms: hook())


def test_run_exit_code_mapping() -> None:
    assert run_exit_code(LoopExit(())) == 0
    assert run_exit_code(LoopExit(("A",))) == 4
    assert run_exit_code(FatalLoopError(RuntimeError("x"), ())) == 1
    assert run_exit_code(FatalLoopError(RuntimeError("x"), ("A",))) == 4


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"])
def test_a_signal_stops_gracefully_and_exits_0(
    db: Path, sig: signal.Signals, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    source = FakeSource(clock, forming_rows())
    opened: list[FakeSource] = []
    assert run_cli(db, source, clock, stop_on_sleep(clock, 2, sig), opened=opened) == 0
    captured = capsys.readouterr()
    all_lines = captured.out.splitlines()
    assert all_lines[0].startswith(f"reconcile symbol={SYMBOL} window=no_baseline ")  # the pre-start read comes first
    assert all_lines[1].startswith("reconcile summary symbols=1 ")
    lines = round_lines(captured.out)
    assert lines[0] == f"round target={FIRST_TARGET} collected=1 conflicted=0 gave_up=0 stopped=0 write_uncertain=0"
    assert lines[1].startswith("round target=1792677000000 collected=0 conflicted=0 gave_up=0 stopped=1")
    assert "Traceback" not in captured.err
    assert candle_count(db) == 1
    assert source.closed


def test_the_request_spacing_is_applied(db: Path) -> None:
    clock = FakeClock()
    starts: list[int] = []

    class Timed(FakeSource):
        async def fetch_server_time_ms(self) -> int:
            starts.append(clock.monotonic_ns())
            return await super().fetch_server_time_ms()

    source = Timed(clock, forming_rows(), rtt_ms=0, on_klines=lambda: starts.append(clock.monotonic_ns()))
    sleep = stop_on_sleep(clock, 2)  # call 1 is the limiter's wait, call 2 the wait for the next boundary
    assert run_cli(db, source, clock, sleep, "--min-request-interval-ms", "200") == 0
    assert len(starts) == 2 and starts[1] - starts[0] >= 200_000_000
    assert 200 in sleep.calls


def test_a_fatal_storage_error_exits_1(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def refuse(self: AsyncCandleWriter, *args: object, **kwargs: object) -> WriteReport:
        raise CandleWriteError("batch rolled back; nothing was written: refused")

    monkeypatch.setattr(AsyncCandleWriter, "write", refuse)
    clock = FakeClock()
    assert run_cli(db, FakeSource(clock, forming_rows()), clock, AdvancingSleep(clock)) == 1
    err = capsys.readouterr().err
    assert "fatal error" in err and "Traceback" not in err


def fail_first_write_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    original = AsyncCandleWriter.write
    calls = [0]

    async def write(self: AsyncCandleWriter, *args: Any, **kwargs: Any) -> WriteReport:
        calls[0] += 1
        if calls[0] == 1:
            raise CandleWriteOutcomeUnknown("commit outcome unknown")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AsyncCandleWriter, "write", write)


def test_unknown_write_then_source_error_then_sigterm_exits_4(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fail_first_write_as_unknown(monkeypatch)
    clock = FakeClock()
    klines = [0]

    def on_klines() -> None:
        klines[0] += 1
        if klines[0] == 2:
            raise SourceError("klines down")

    source = FakeSource(clock, forming_rows(), on_klines=on_klines)
    stopper = raise_on_nth(2)
    sleep = AdvancingSleep(clock, on_sleep=lambda ms: stopper())  # the second retry wait: stop mid-retry
    assert run_cli(db, source, clock, sleep) == 4
    captured = capsys.readouterr()
    assert "write_uncertain=1" in captured.out
    assert SYMBOL in captured.err and "uncertain" in captured.err


def test_unknown_write_then_clean_rounds_still_exits_4(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fail_first_write_as_unknown(monkeypatch)
    clock = FakeClock()
    source = FakeSource(clock, forming_rows())
    sleeps = [0]

    def on_sleep(ms: int) -> None:
        sleeps[0] += 1
        source.server_time_ms = clock.wall_ms() + 1000  # later slots become confirmable
        if sleeps[0] == 3:  # retry wait, boundary wait, boundary wait
            signal.raise_signal(signal.SIGTERM)

    assert run_cli(db, source, clock, AdvancingSleep(clock, on_sleep=on_sleep)) == 4
    lines = round_lines(capsys.readouterr().out)
    assert "collected=1" in lines[0] and "write_uncertain=1" in lines[0]
    assert "collected=1" in lines[1] and "write_uncertain=1" in lines[1]  # clean round, flag persists


def test_writer_close_failure_on_a_clean_run_exits_1_and_is_an_error(
    db: Path, writer_close: WriterCloseSpy, capsys: pytest.CaptureFixture[str]
) -> None:
    writer_close.error = RuntimeError("close failed")
    clock = FakeClock()
    source = FakeSource(clock, forming_rows())
    assert run_cli(db, source, clock, stop_on_sleep(clock)) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "close failed" in err


def test_writer_close_failure_never_lowers_4(
    db: Path, writer_close: WriterCloseSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_first_write_as_unknown(monkeypatch)
    writer_close.error = RuntimeError("close failed")
    clock = FakeClock()
    source = FakeSource(clock, forming_rows())
    sleep = AdvancingSleep(clock, on_sleep=lambda ms: signal.raise_signal(signal.SIGTERM))
    assert run_cli(db, source, clock, sleep) == 4


def test_exchange_close_failure_is_only_a_warning(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class BadClose(FakeSource):
        async def close(self) -> None:
            raise RuntimeError("exchange close failed")

    clock = FakeClock()
    source = BadClose(clock, forming_rows())
    assert run_cli(db, source, clock, stop_on_sleep(clock)) == 0
    assert "warning: closing the exchange client failed" in capsys.readouterr().err


def test_a_missing_database_directory_exits_1_and_opens_nothing(tmp_path: Path) -> None:
    clock = FakeClock()
    opened: list[FakeSource] = []
    target = tmp_path / "missing" / "market.db"
    assert run_cli(target, FakeSource(clock, forming_rows()), clock, AdvancingSleep(clock), opened=opened) == 1
    assert opened == [] and not target.exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--symbol", SYMBOL],  # a duplicate
        ["--max-concurrency", "0"],
        ["--min-request-interval-ms", "-1"],
        ["--limit", "0"],
        ["--limit", "4"],  # the default delay of 15 s needs 5 rows (S15 Decision 9)
        ["--publication-delay-ms", "30000000"],
    ],
    ids=["duplicate_symbol", "zero_concurrency", "negative_interval", "zero_limit", "limit_below", "delay_too_long"],
)
def test_usage_errors_exit_2_and_open_nothing(db: Path, extra: list[str]) -> None:
    clock = FakeClock()
    opened: list[FakeSource] = []
    with pytest.raises(SystemExit) as raised:
        run_cli(db, FakeSource(clock, forming_rows()), clock, AdvancingSleep(clock), *extra, opened=opened)
    assert raised.value.code == 2 and opened == [] and not db.exists()


def test_run_without_a_symbol_is_a_usage_error(db: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["run", "--db", str(db)])
    assert raised.value.code == 2


def test_signal_handlers_are_removed_after_the_run(db: Path) -> None:
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    clock = FakeClock()
    assert run_cli(db, FakeSource(clock, forming_rows()), clock, stop_on_sleep(clock)) == 0
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before


def test_grace_timeout_exit_codes_and_the_unknown_count_rendering(capsys: pytest.CaptureFixture[str]) -> None:
    async def fetch_case() -> None:
        h = Harness()
        h.source.gate = asyncio.Event()
        h.start()
        await h.t.settle()
        h.stop.set()
        await h.t.until_waiting(1)
        await h.t.advance(GRACE)
        outcome = await h.task
        assert run_exit_code(outcome) == 0
        cli._print_round(h.rounds[0])
        await assert_no_leftover_tasks()

    async def write_case() -> None:
        h = Harness(sink=ControlledSink(block=True))
        h.start()
        await h.sink.write_started.wait()
        h.stop.set()
        await h.t.until_waiting(1)
        await h.t.advance(GRACE)
        assert run_exit_code(await h.task) == 4
        await assert_no_leftover_tasks()

    run_scenario(fetch_case)
    run_scenario(write_case)
    out = capsys.readouterr().out
    assert "attempts=unknown" in out and "attempts=0" not in out and "stopped=1" in out


def test_round_lines_list_conflicts_and_problems(capsys: pytest.CaptureFixture[str]) -> None:
    result = SymbolResult("A", SymbolOutcome.COLLECTED, 1, 2, None, (5, 7), None)
    gave_up = SymbolResult("B", SymbolOutcome.GAVE_UP, 6, 0, None, (), "target not confirmed (pending=1, invalid=0)")
    cli._print_round(RoundReport(None, 1, (result, gave_up), ("B",)))
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "round target=none collected=1 conflicted=0 gave_up=1 stopped=0 write_uncertain=1"
    assert lines[1] == "  A collected attempts=1 conflicts=5,7 problem=-"
    assert lines[2] == "  B gave_up attempts=6 conflicts=- problem=target not confirmed (pending=1, invalid=0)"
