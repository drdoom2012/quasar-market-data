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
from quasar_market_data.config.settings import DEFAULT_CONFIG_PATH, load_settings
from quasar_market_data.domain.reconcile import steady_state_limit_needed
from quasar_market_data.domain.server_time import ServerTimeObservation
from tests.collector.fetch_fakes import (
    FIVE_MINUTES,
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
from tests.config_files import write_config
from tests.storage.test_migrate import foreign_file, sha

FIRST_TARGET = 1792676700000
TF = FIVE_MINUTES.milliseconds


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


def argv(db: Path, **values: Any) -> list[str]:
    settings: dict[str, Any] = {
        "database_path": db.name,
        "symbols": (SYMBOL,),
        "max_concurrency": 1,
        "backfill_enabled": False,
        **values,
    }
    return ["run", str(write_config(db.parent, **settings))]


def run_cli(
    db: Path,
    source: FakeSource,
    clock: FakeClock,
    sleep: AdvancingSleep,
    *,
    opened: list[FakeSource] | None = None,
    **values: Any,
) -> int:
    return main(argv(db, **values), open_source=opener(source, opened), clock=clock, sleep=sleep)


def candle_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT count(*) FROM candles").fetchone()[0])
    finally:
        conn.close()


def candle_rows(db: Path, symbol: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT * FROM candles WHERE symbol = ? ORDER BY open_time_ms", (symbol,)).fetchall()
    finally:
        conn.close()


def round_lines(out: str) -> list[str]:
    """The S14 round report, without the config line and the S15 reconcile lines around it."""
    skipped = ("config ", "reconcile", "  hole ", "  still_missing ")
    return [line for line in out.splitlines() if not line.startswith(skipped)]


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
    assert all_lines[0].startswith("config path=")
    assert all_lines[1].startswith(f"reconcile symbol={SYMBOL} window=no_baseline ")  # the pre-start read comes first
    assert all_lines[2].startswith("reconcile summary symbols=1 ")
    lines = round_lines(captured.out)
    assert (
        lines[0]
        == f"round target={FIRST_TARGET} collected=1 conflicted=0 gave_up=0 stopped=0 write_uncertain=0 lateness_ms=0 idle_overrun_ms=0 skipped_rounds=0 clock_step_ms=0 clock_lead_slots=0"
    )
    assert lines[1].startswith("round target=1792677000000 collected=0 conflicted=0 gave_up=0 stopped=1")
    assert "Traceback" not in captured.err
    assert candle_count(db) == 1
    assert source.closed


def test_the_request_spacing_is_applied(db: Path) -> None:
    clock = FakeClock()
    starts: list[int] = []

    class Timed(FakeSource):
        async def fetch_server_time(self) -> ServerTimeObservation:
            starts.append(clock.monotonic_ns())
            return await super().fetch_server_time()

    source = Timed(clock, forming_rows(), rtt_ms=0, on_klines=lambda: starts.append(clock.monotonic_ns()))
    sleep = stop_on_sleep(clock, 2)  # call 1 is the limiter's wait, call 2 the wait for the next boundary
    assert run_cli(db, source, clock, sleep, min_request_interval_ms=200) == 0
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


def test_a_missing_database_directory_exits_2_and_opens_nothing(tmp_path: Path) -> None:
    clock = FakeClock()
    opened: list[FakeSource] = []
    config = write_config(tmp_path, database_path="missing/market.db", symbols=(SYMBOL,))
    with pytest.raises(SystemExit) as raised:
        main(["run", str(config)], open_source=opener(FakeSource(clock, forming_rows()), opened), clock=clock)
    assert raised.value.code == 2 and opened == [] and not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    "values",
    [
        {"symbols": (SYMBOL, SYMBOL)},  # a duplicate
        {"max_concurrency": 0},
        {"min_request_interval_ms": -1},
        {"limit": 0},
        {"limit": steady_state_limit_needed(15_000, TF) - 1},  # the default delay of 15 s needs 5 rows (S15 Decision 9)
        {"publication_delay_ms": 300_001},
    ],
    ids=["duplicate_symbol", "zero_concurrency", "negative_interval", "zero_limit", "limit_below", "delay_too_long"],
)
def test_usage_errors_exit_2_and_open_nothing(db: Path, values: dict[str, Any]) -> None:
    clock = FakeClock()
    opened: list[FakeSource] = []
    with pytest.raises(SystemExit) as raised:
        run_cli(db, FakeSource(clock, forming_rows()), clock, AdvancingSleep(clock), opened=opened, **values)
    assert raised.value.code == 2 and opened == [] and not db.exists()


@pytest.mark.parametrize(
    "switch",
    [
        ["--db", "x.db"],
        ["--symbol", SYMBOL],
        ["--limit", "100"],
        ["--publication-delay-ms", "15000"],
        ["--max-concurrency", "1"],
        ["--min-request-interval-ms", "1"],
        ["--backfill-days", "3"],
        ["--no-backfill"],
    ],
)
def test_no_switch_is_accepted_any_more(db: Path, switch: list[str]) -> None:  # INV-063
    with pytest.raises(SystemExit) as raised:
        main(["run", str(write_config(db.parent)), *switch])
    assert raised.value.code == 2


@pytest.mark.parametrize("text", ["", "not toml [", "[database]\npath = 1\n"], ids=["empty", "not_toml", "wrong_type"])
def test_an_invalid_file_exits_2_and_opens_nothing(
    tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]
) -> None:  # INV-064
    config = tmp_path / "quasar.toml"
    config.write_text(text, encoding="utf-8")
    clock = FakeClock()
    opened: list[FakeSource] = []
    with pytest.raises(SystemExit) as raised:
        main(["run", str(config)], open_source=opener(FakeSource(clock, forming_rows()), opened), clock=clock)
    assert raised.value.code == 2 and opened == [] and list(tmp_path.glob("*.db*")) == []
    err = capsys.readouterr().err
    assert f"config {config}:" in err and "Traceback" not in err
    if text.startswith("[database]"):
        assert "database.path" in err


def test_a_config_that_is_a_directory_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    opened: list[FakeSource] = []
    with pytest.raises(SystemExit) as raised:
        main(["run", str(tmp_path)], open_source=opener(FakeSource(clock, forming_rows()), opened), clock=clock)
    assert raised.value.code == 2 and opened == []
    err = capsys.readouterr().err
    assert "cannot read" in err and "Traceback" not in err


def test_a_config_symlink_loop_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    loop = tmp_path / "loop.toml"
    loop.symlink_to(loop)
    clock = FakeClock()
    opened: list[FakeSource] = []
    with pytest.raises(SystemExit) as raised:
        main(["run", str(loop)], open_source=opener(FakeSource(clock, forming_rows()), opened), clock=clock)
    assert raised.value.code == 2 and opened == []
    err = capsys.readouterr().err
    assert "cannot read" in err and "Traceback" not in err


def test_run_prints_the_config_line_first(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    arguments = argv(db)
    code = main(
        arguments, open_source=opener(FakeSource(clock, forming_rows())), clock=clock, sleep=stop_on_sleep(clock)
    )
    assert code == 0
    first = capsys.readouterr().out.splitlines()[0]
    loaded = load_settings(Path(arguments[1]))
    assert first == loaded.start_line() and f"database={db}" in first


def test_the_default_config_path_is_relative_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    write_config(tmp_path / "config", symbols=(SYMBOL,), max_concurrency=1, backfill_enabled=False)
    clock = FakeClock()
    opened: list[FakeSource] = []
    code = main(
        ["run"], open_source=opener(FakeSource(clock, forming_rows()), opened), clock=clock, sleep=stop_on_sleep(clock)
    )
    assert code == 0 and len(opened) == 1
    assert (tmp_path / "config" / "market.db").exists()
    assert DEFAULT_CONFIG_PATH == Path("config/quasar.toml")
    assert f"config path={(tmp_path / DEFAULT_CONFIG_PATH).resolve()} " in capsys.readouterr().out


def test_the_database_path_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # INV-065
    (tmp_path / "conf").mkdir()
    (tmp_path / "elsewhere").mkdir()
    config = write_config(tmp_path / "conf", symbols=(SYMBOL,))
    seen: list[Path] = []

    def spy(settings: Any, open_source: Any, clock: Any, sleep: Any) -> int:
        seen.append(settings.database_path)
        return 0

    monkeypatch.setattr(cli, "_run_command", spy)
    for cwd in (tmp_path, tmp_path / "elsewhere"):
        monkeypatch.chdir(cwd)
        assert main(["run", str(config)]) == 0
    assert seen == [tmp_path / "conf" / "market.db"] * 2


def test_a_removed_symbol_keeps_its_rows(db: Path, capsys: pytest.CaptureFixture[str]) -> None:  # INV-067
    eth = "ETH/USDT:USDT"
    clock = FakeClock()
    seed_rows = FakeSource(clock, forming_rows())
    assert run_cli(db, seed_rows, clock, stop_on_sleep(clock), symbols=(eth,)) == 0
    before = candle_rows(db, eth)
    assert before

    clock = FakeClock()
    source = FakeSource(clock, forming_rows())
    assert run_cli(db, source, clock, stop_on_sleep(clock), symbols=(SYMBOL,)) == 0
    assert candle_rows(db, eth) == before
    assert {symbol for symbol, _, _ in source.kline_calls} == {SYMBOL}
    assert candle_rows(db, SYMBOL)


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
    assert (
        lines[0]
        == "round target=none collected=1 conflicted=0 gave_up=1 stopped=0 write_uncertain=1 lateness_ms=0 idle_overrun_ms=0 skipped_rounds=0 clock_step_ms=0 clock_lead_slots=-"
    )
    assert lines[1] == "  A collected attempts=1 conflicts=5,7 problem=-"
    assert lines[2] == "  B gave_up attempts=6 conflicts=- problem=target not confirmed (pending=1, invalid=0)"


def test_a_foreign_database_file_exits_1_untouched_and_opens_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "foreign.db"
    foreign_file(db, "delete", "unknown_tables")
    before = sha(db)
    clock = FakeClock()
    opened: list[FakeSource] = []
    assert run_cli(db, FakeSource(clock, forming_rows()), clock, AdvancingSleep(clock), opened=opened) == 1
    assert sha(db) == before and opened == []
    assert "not a quasar-market-data database" in capsys.readouterr().err
