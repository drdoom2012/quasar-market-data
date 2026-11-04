"""``run`` starts the funding rate task after round one and keeps it from touching the candle path (ADR-0004 P-9).

The task itself is tested in ``test_funding_rate_task_*``; here ``run_funding_rate_task`` is replaced by a stub so that
only the wiring is under test: when it starts, what it receives, what happens when it fails or hangs, the output
lines and the shared candle activity.
"""

import asyncio
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector import cli
from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.async_funding_rate_writer import AsyncFundingRateWriter
from quasar_market_data.collector.candle_write_activity import CandleWriteActivity
from quasar_market_data.collector.candle_writer import CandleWriteError
from quasar_market_data.collector.cli import main
from quasar_market_data.collector.clock import Clock
from quasar_market_data.collector.extension_pause import ExtensionPause, ExtensionStopped
from quasar_market_data.collector.fetch_funding_rate_once import FundingRateOnceSource
from quasar_market_data.collector.funding_rate_limiter import FundingRateLimiter
from tests.collector.fetch_fakes import SYMBOL, AdvancingSleep, FakeClock, FakeSource, forming_rows
from tests.collector.funding_rate_fakes import FakeFundingSource
from tests.collector.test_cli_run import candle_count
from tests.config_files import write_config

TABLE = (
    "\n[funding_rate]\nenabled = true\npublication_delay_ms = 30000\nround_offset_ms = 60000\n"
    "budget_per_5min = 150\nbackfill_days = 365\nrecheck_ms = 86400000\n"
)
OI_TABLE = (
    '\n[open_interest]\nenabled = true\nperiod = "5m"\npublication_delay_ms = 15000\n'
    "round_offset_ms = 200000\nbudget_per_5min = 250\n"
)


def config_path(db: Path, table: str | None) -> str:
    path = write_config(db.parent, database_path=db.name, symbols=(SYMBOL,), max_concurrency=1, backfill_enabled=False)
    if table is not None:
        path.write_text(path.read_text(encoding="utf-8") + table, encoding="utf-8")
    return str(path)


class Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.opened: list[FakeFundingSource] = []


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "market.db"


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


def stub_task(
    monkeypatch: pytest.MonkeyPatch, rec: Recorder, behaviour: Callable[..., Awaitable[None]] | None = None
) -> None:
    async def run_stub(
        source: Any,
        writer: Any,
        db: Path,
        config: Any,
        limiter: Any,
        pause: Any,
        clock: Any,
        sleep: Any,
        stop: asyncio.Event,
        emit: Callable[[str], None],
        measurements: Any,
    ) -> None:
        rec.events.append("task")
        rec.calls.append(
            {"source": source, "writer": writer, "config": config, "limiter": limiter, "pause": pause, "emit": emit}
        )
        if behaviour is not None:
            await behaviour(stop=stop, emit=emit)
            return
        emit("funding-round stub boundary=1")
        emit("funding-warning stub warning")
        await stop.wait()

    monkeypatch.setattr(cli, "run_funding_rate_task", run_stub)


def opener(
    rec: Recorder, clock: FakeClock, error: BaseException | None = None
) -> Callable[..., Awaitable[FundingRateOnceSource]]:
    async def open_funding(
        limiter: FundingRateLimiter, pause: ExtensionPause, received: Clock
    ) -> FundingRateOnceSource:
        rec.events.append("open")
        assert isinstance(limiter, FundingRateLimiter) and isinstance(pause, ExtensionPause) and received is clock
        if error is not None:
            raise error
        source = FakeFundingSource(clock)
        rec.opened.append(source)
        return source

    return open_funding


def stop_when_started(clock: FakeClock, rec: Recorder, *, wait: bool) -> AdvancingSleep:
    """SIGTERM on the second sleep, or, with ``wait``, on the first sleep after the funding task has started."""
    sleeps = [0]

    def hook(ms: int) -> None:
        sleeps[0] += 1
        started = any(event in rec.events for event in ("open", "task"))
        if sleeps[0] >= 2 and (not wait or started or sleeps[0] > 20_000):
            signal.raise_signal(signal.SIGTERM)

    return AdvancingSleep(clock, on_sleep=hook)


def run_cli(
    db: Path, rec: Recorder, table: str | None, *, error: BaseException | None = None, wait: bool = True
) -> int:
    clock = FakeClock()
    source = FakeSource(clock, forming_rows(), on_klines=lambda: rec.events.append("klines"))

    async def open_source() -> FakeSource:
        return source

    return main(
        ["run", config_path(db, table)],
        open_source=open_source,
        open_funding_source=opener(rec, clock, error),
        clock=clock,
        sleep=stop_when_started(clock, rec, wait=wait and table is not None and "enabled = true" in table),
    )


def test_absent_or_disabled_table_creates_nothing_and_prints_nothing_new(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_task(monkeypatch, rec)
    outputs = []
    for index, table in enumerate((None, TABLE.replace("enabled = true", "enabled = false"))):
        folder = db.parent / f"d{index}"
        folder.mkdir()
        assert run_cli(folder / "market.db", rec, table, wait=False) == 0
        captured = capsys.readouterr()
        outputs.append((captured.out, captured.err))
    assert rec.events.count("open") == 0 and rec.events.count("task") == 0
    assert outputs[0][0].splitlines()[1:] == outputs[1][0].splitlines()[1:]  # but for the config line's hash
    assert "funding-" not in outputs[0][0] + outputs[0][1] + outputs[1][0] + outputs[1][1]


def test_enabled_starts_the_task_after_round_one_with_its_own_limiter_and_closes_everything(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_task(monkeypatch, rec)
    assert run_cli(db, rec, TABLE) == 0
    assert rec.events.index("klines") < rec.events.index("open") < rec.events.index("task")  # never before round one
    (call,) = rec.calls
    config = call["config"]
    assert config.budget_per_5min == 150 and config.symbols == (SYMBOL,) and config.backfill_days == 365
    assert (config.publication_delay_ms, config.round_offset_ms, config.recheck_ms) == (30_000, 60_000, 86_400_000)
    assert isinstance(call["writer"], AsyncFundingRateWriter) and isinstance(call["limiter"], FundingRateLimiter)
    assert isinstance(call["pause"], ExtensionPause)
    assert rec.opened[0].closed  # the source is closed on the way out
    captured = capsys.readouterr()
    assert "funding-round stub boundary=1" in captured.out and "funding-warning stub warning" in captured.err
    assert "funding-warning" not in captured.out and "funding-round" not in captured.err
    assert "funding-budget funding=150 open_interest=off" in captured.out.splitlines()


def test_the_task_is_not_started_when_round_one_is_fatal(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_task(monkeypatch, rec)

    async def refuse(self: AsyncCandleWriter, *args: object, **kwargs: object) -> Any:
        raise CandleWriteError("batch rolled back; nothing was written: refused")

    monkeypatch.setattr(AsyncCandleWriter, "write", refuse)
    assert run_cli(db, rec, TABLE, wait=False) == 1
    assert "open" not in rec.events and "task" not in rec.events


@pytest.mark.parametrize("failure", ["task", "factory", "stopped"])
def test_a_failing_funding_task_ends_only_itself(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    async def boom(*, stop: asyncio.Event, emit: Callable[[str], None]) -> None:
        raise ExtensionStopped if failure == "stopped" else RuntimeError("boom")

    stub_task(monkeypatch, rec, boom if failure in ("task", "stopped") else None)
    error = RuntimeError("no client") if failure == "factory" else None
    assert run_cli(db, rec, TABLE, error=error) == 0  # the exit status is the candle loop's
    err = capsys.readouterr().err
    expected = {"task": "RuntimeError: boom", "factory": "RuntimeError: no client", "stopped": "ExtensionStopped"}[
        failure
    ]
    assert f"funding-error task stopped: {expected}" in err and "Traceback" not in err
    assert candle_count(db) == 1  # candle collection went on


def test_a_task_that_ignores_stop_is_cancelled_after_the_grace_and_the_run_still_exits(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled: list[bool] = []

    async def stubborn(*, stop: asyncio.Event, emit: Callable[[str], None]) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    stub_task(monkeypatch, rec, stubborn)
    monkeypatch.setattr(cli, "FUNDING_RATE_STOP_GRACE_MS", 20)
    assert run_cli(db, rec, TABLE) == 0
    assert cancelled == [True] and rec.opened[0].closed


def test_every_candle_write_enters_the_activity_when_funding_is_enabled(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_task(monkeypatch, rec)
    entered = [0]
    writes = [0]
    original_writing = CandleWriteActivity.writing
    original_write = AsyncCandleWriter.write

    def writing(self: CandleWriteActivity) -> Any:
        entered[0] += 1
        return original_writing(self)

    async def write(self: AsyncCandleWriter, *args: Any, **kwargs: Any) -> Any:
        writes[0] += 1
        return await original_write(self, *args, **kwargs)

    monkeypatch.setattr(CandleWriteActivity, "writing", writing)
    monkeypatch.setattr(AsyncCandleWriter, "write", write)
    assert run_cli(db, rec, TABLE) == 0
    assert writes[0] >= 1 and entered[0] == writes[0]


def test_both_extension_datasets_share_the_activity_and_both_measurement_collectors_get_the_durations(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    async def oi_stub(source: Any, writer: Any, db: Path, config: Any, budget: Any, activity: Any, *rest: Any) -> None:
        seen["oi_activity"] = activity
        seen["oi_measurements"] = rest[-1]
        await rest[3].wait()

    async def funding_stub(
        source: Any, writer: Any, db: Path, config: Any, limiter: Any, pause: Any, clock: Any, sleep: Any,
        stop: asyncio.Event, emit: Any, measurements: Any,
    ) -> None:  # fmt: skip
        seen["funding_measurements"] = measurements
        seen["funding_activity"] = writer._activity if hasattr(writer, "_activity") else None
        await stop.wait()

    monkeypatch.setattr(cli, "run_open_interest_task", oi_stub)
    monkeypatch.setattr(cli, "run_funding_rate_task", funding_stub)
    clock = FakeClock()
    source = FakeSource(clock, forming_rows(), on_klines=lambda: rec.events.append("klines"))

    async def open_source() -> FakeSource:
        return source

    from tests.collector.open_interest_fakes import FakeOiSource

    async def open_oi(budget: Any, gate: Any, received: Clock) -> Any:
        rec.events.append("open")
        return FakeOiSource(clock)

    code = main(
        ["run", config_path(db, TABLE + OI_TABLE)],
        open_source=open_source,
        open_oi_source=open_oi,
        open_funding_source=opener(rec, clock),
        clock=clock,
        sleep=stop_when_started(clock, rec, wait=True),
    )
    assert code == 0
    assert "oi_measurements" in seen and "funding_measurements" in seen
    assert seen["oi_measurements"] is not seen["funding_measurements"]
    out = capsys.readouterr().out
    assert "funding-budget funding=150 open_interest=250" in out.splitlines()


def test_both_extension_datasets_receive_the_same_pause_and_each_its_own_limiter(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Amendment A-3 to ADR-0003: one ``ExtensionPause`` per process, no shared token bucket (ADR-0004 P-9)."""
    from tests.collector.open_interest_fakes import FakeOiSource

    pauses: dict[str, Any] = {}
    limits: dict[str, Any] = {}

    async def oi_stub(source: Any, writer: Any, db: Path, config: Any, budget: Any, activity: Any, *rest: Any) -> None:
        await rest[3].wait()

    monkeypatch.setattr(cli, "run_open_interest_task", oi_stub)
    stub_task(monkeypatch, rec)
    clock = FakeClock()
    source = FakeSource(clock, forming_rows(), on_klines=lambda: rec.events.append("klines"))

    async def open_source() -> FakeSource:
        return source

    async def open_oi(budget: Any, pause: Any, received: Clock) -> Any:
        rec.events.append("open")
        pauses["oi"], limits["oi"] = pause, budget
        return FakeOiSource(clock)

    async def open_funding(limiter: Any, pause: Any, received: Clock) -> Any:
        rec.events.append("open")
        pauses["funding"], limits["funding"] = pause, limiter
        return FakeFundingSource(clock)

    code = main(
        ["run", config_path(db, TABLE + OI_TABLE)],
        open_source=open_source,
        open_oi_source=open_oi,
        open_funding_source=open_funding,
        clock=clock,
        sleep=stop_when_started(clock, rec, wait=True),
    )
    assert code == 0
    assert isinstance(pauses["oi"], ExtensionPause) and pauses["oi"] is pauses["funding"]
    assert limits["oi"] is not limits["funding"]  # each dataset keeps its own bounded budget


def test_the_capacity_warning_is_printed_at_start_only_for_an_enabled_table(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_task(monkeypatch, rec)
    heavy = TABLE.replace("budget_per_5min = 150", "budget_per_5min = 50")
    assert run_cli(db, rec, heavy) == 0
    err = capsys.readouterr().err
    assert "warning: funding_rate" not in err  # one symbol is far below 90 % of 274
    monkeypatch.setattr(
        "quasar_market_data.config.settings.Settings.funding_rate_capacity_warning",
        lambda self: "warning: funding_rate: stub capacity warning",
    )
    folder = db.parent / "second"
    folder.mkdir()
    assert run_cli(folder / "market.db", rec, TABLE) == 0
    assert "warning: funding_rate: stub capacity warning" in capsys.readouterr().err
