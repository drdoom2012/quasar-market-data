"""``run`` starts the open interest task after round one and keeps it from touching the candle path (ADR-0003 P-9).

The task itself is tested in ``test_open_interest_task_*``; here ``run_open_interest_task`` is replaced by a stub so
that only the wiring is under test: when it starts, what it receives, what happens when it fails or hangs.
"""

import asyncio
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector import cli
from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.async_open_interest_writer import AsyncOpenInterestWriter
from quasar_market_data.collector.candle_write_activity import CandleWriteActivity
from quasar_market_data.collector.candle_writer import CandleWriteError
from quasar_market_data.collector.cli import main
from quasar_market_data.collector.clock import Clock
from quasar_market_data.collector.extension_pause import ExtensionPause, ExtensionStopped
from quasar_market_data.collector.fetch_open_interest_once import OpenInterestOnceSource
from quasar_market_data.collector.open_interest_budget import OpenInterestBudget
from tests.collector.fetch_fakes import SYMBOL, AdvancingSleep, FakeClock, FakeSource, forming_rows
from tests.collector.open_interest_fakes import FakeOiSource
from tests.collector.test_cli_run import candle_count
from tests.config_files import write_config

TABLE = (
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
        self.opened: list[FakeOiSource] = []


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
        budget: Any,
        activity: Any,
        clock: Any,
        sleep: Any,
        stop: asyncio.Event,
        emit: Callable[[str], None],
        measurements: Any,
    ) -> None:
        rec.events.append("task")
        rec.calls.append(
            {"source": source, "writer": writer, "config": config, "budget": budget, "activity": activity, "emit": emit}
        )
        if behaviour is not None:
            await behaviour(stop=stop, emit=emit)
            return
        emit("oi-round stub boundary=1")
        emit("oi-warning stub warning")
        await stop.wait()

    monkeypatch.setattr(cli, "run_open_interest_task", run_stub)


def oi_opener(
    rec: Recorder, clock: FakeClock, error: BaseException | None = None
) -> Callable[..., Awaitable[OpenInterestOnceSource]]:
    async def open_oi(budget: OpenInterestBudget, gate: ExtensionPause, received: Clock) -> OpenInterestOnceSource:
        rec.events.append("open")
        assert isinstance(budget, OpenInterestBudget) and isinstance(gate, ExtensionPause) and received is clock
        if error is not None:
            raise error
        source = FakeOiSource(clock)
        rec.opened.append(source)
        return source

    return open_oi


def stop_when_started(clock: FakeClock, rec: Recorder, *, wait: bool) -> AdvancingSleep:
    """SIGTERM on the second sleep, or, with ``wait``, on the first sleep after the open interest task has started.

    The task starts when round one's post-check (a real worker thread) has ended; on a slow machine that is later than
    the loop's second sleep, and a stop before that start is correct behaviour but not what these tests look at.
    """
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
        open_oi_source=oi_opener(rec, clock, error),
        clock=clock,
        sleep=stop_when_started(clock, rec, wait=wait and table is not None),
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
    assert "oi-" not in outputs[0][0] + outputs[0][1] + outputs[1][0] + outputs[1][1]


def test_enabled_starts_the_task_after_round_one_with_its_own_budget_and_closes_everything(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_task(monkeypatch, rec)
    assert run_cli(db, rec, TABLE) == 0
    assert rec.events.index("klines") < rec.events.index("open") < rec.events.index("task")  # never before round one
    (call,) = rec.calls
    assert call["config"].budget_per_5min == 250 and call["config"].symbols == (SYMBOL,)
    assert call["config"].publication_delay_ms == 15_000 and call["config"].round_offset_ms == 200_000
    assert isinstance(call["writer"], AsyncOpenInterestWriter) and isinstance(call["activity"], CandleWriteActivity)
    assert rec.opened[0].closed  # the source is closed on the way out
    captured = capsys.readouterr()
    assert "oi-round stub boundary=1" in captured.out and "oi-warning stub warning" in captured.err
    assert "oi-warning" not in captured.out and "oi-round" not in captured.err


def test_the_task_is_not_started_when_round_one_is_fatal(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_task(monkeypatch, rec)

    async def refuse(self: AsyncCandleWriter, *args: object, **kwargs: object) -> Any:
        raise CandleWriteError("batch rolled back; nothing was written: refused")

    monkeypatch.setattr(AsyncCandleWriter, "write", refuse)
    assert run_cli(db, rec, TABLE, wait=False) == 1
    assert "open" not in rec.events and "task" not in rec.events


@pytest.mark.parametrize("failure", ["task", "factory", "banned"])
def test_a_failing_open_interest_task_ends_only_itself(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    async def boom(*, stop: asyncio.Event, emit: Callable[[str], None]) -> None:
        raise ExtensionStopped if failure == "banned" else RuntimeError("boom")

    stub_task(monkeypatch, rec, boom if failure in ("task", "banned") else None)
    error = RuntimeError("no client") if failure == "factory" else None
    assert run_cli(db, rec, TABLE, error=error) == 0  # the exit status is the candle loop's
    err = capsys.readouterr().err
    expected = {"task": "RuntimeError: boom", "factory": "RuntimeError: no client", "banned": "ExtensionStopped"}[
        failure
    ]
    assert f"oi-error task stopped: {expected}" in err and "Traceback" not in err
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
    monkeypatch.setattr(cli, "OPEN_INTEREST_STOP_GRACE_MS", 20)
    assert run_cli(db, rec, TABLE) == 0
    assert cancelled == [True] and rec.opened[0].closed


def test_every_candle_write_enters_the_activity_when_open_interest_is_enabled(
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


def test_without_the_table_no_candle_write_enters_an_activity(
    db: Path, rec: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = [0]
    original_writing = CandleWriteActivity.writing

    def writing(self: CandleWriteActivity) -> Any:
        entered[0] += 1
        return original_writing(self)

    monkeypatch.setattr(CandleWriteActivity, "writing", writing)
    assert run_cli(db, rec, None) == 0
    assert entered[0] == 0
