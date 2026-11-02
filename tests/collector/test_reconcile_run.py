"""``quasar-collector run`` with the restart reconcile (S15 Task 4): pre-check, first round, post-check.

Numbers follow "Reach arithmetic" of ``docs/superpowers/plans/2026-10-27-s15-checkpoints-and-restart-reconcile.md``.
``FakeClock`` starts at ``F0 + 36 s``, so with the default 15 s delay the target is ``T = F0 - tf`` and the window
starts at ``WS = F0 - 997·tf``. Every claim is checked against the database, never against a write report.
"""

import shutil
import signal
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector import cli, reconcile_reader
from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.binance_klines import parse_klines
from quasar_market_data.collector.candle_writer import CandleWriter, WriteReport
from quasar_market_data.collector.kline_source import KlineSource
from quasar_market_data.collector.reconcile_reader import ReconcileError
from quasar_market_data.collector.reconcile_reader import reconcile as REAL_RECONCILE
from quasar_market_data.domain.reconcile import (
    HistoryStatus,
    SeriesReconcile,
    WindowStatus,
    steady_state_limit_needed,
)
from quasar_market_data.domain.timed_coverage import GapRun, SlotState
from quasar_market_data.domain.validation import require_valid
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.fetch_fakes import (
    FIVE_MINUTES,
    SYMBOL,
    AdvancingSleep,
    FakeClock,
    FakeSource,
    kline_row,
)
from tests.collector.statement_proxy import StatementProxy
from tests.config_files import write_config

TF = FIVE_MINUTES.milliseconds
F0 = 1_792_677_000_000  # the slot of FakeClock's start (F0 + 36 s)
T = F0 - TF
WS = F0 - 997 * TF
AT = 1_792_600_000_000
VENUE = "binance-usdm"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "market.db"


# --- seeding --------------------------------------------------------------------------------------------------


def seed(db: Path, open_times: Sequence[int], *, symbol: str = SYMBOL, **values: str) -> None:
    conn = open_writer(db)
    try:
        migrate(conn, applied_at_ms=AT)
        rows = [kline_row(o, TF, **values) for o in sorted(open_times)]
        candles = [require_valid(c) for c in parse_klines(rows, symbol, FIVE_MINUTES)]
        if candles:
            CandleWriter(conn).write(candles, ingested_at_ms=AT)
    finally:
        conn.close()


def seed_many(db: Path, series: dict[str, Sequence[int]]) -> None:
    conn = open_writer(db)
    try:
        migrate(conn, applied_at_ms=AT)
        candles = []
        for symbol, open_times in series.items():
            rows = [kline_row(o, TF) for o in sorted(open_times)]
            candles += [require_valid(c) for c in parse_klines(rows, symbol, FIVE_MINUTES)]
        CandleWriter(conn).write(candles, ingested_at_ms=AT)
    finally:
        conn.close()


def conflict_at(db: Path, open_ms: int) -> None:
    """Record a conflict through a differing re-delivery of a stored candle."""
    seed(db, [open_ms], close="82768.30")


def mark_unavailable(db: Path, open_ms: int, *, symbol: str = SYMBOL) -> None:
    conn = open_writer(db)
    try:
        conn.execute(
            "INSERT INTO unavailable_candles VALUES (?, ?, ?, ?, 'EXCHANGE_NO_DATA', ?)",
            (VENUE, symbol, FIVE_MINUTES.label, open_ms, AT),
        )
    finally:
        conn.close()


def slots(first: int, last: int) -> list[int]:
    """Open times from ``first`` to ``last`` inclusive."""
    return list(range(first, last + TF, TF))


def stored(db: Path) -> set[int]:
    conn = sqlite3.connect(db)
    try:
        return {row[0] for row in conn.execute("SELECT open_time_ms FROM candles WHERE symbol = ?", (SYMBOL,))}
    finally:
        conn.close()


def table_rows(db: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db)
    try:
        return sorted(conn.execute(sql).fetchall())
    finally:
        conn.close()


CANDLE_ROWS = "SELECT venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover FROM candles"
CONFLICT_ROWS = (
    "SELECT venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover FROM candle_conflicts"
)


# --- running --------------------------------------------------------------------------------------------------


@dataclass
class Run:
    code: int
    out: str
    err: str
    source: FakeSource

    def lines(self, prefix: str) -> list[str]:
        return [line for line in self.out.splitlines() if line.startswith(prefix)]

    def line(self, prefix: str) -> dict[str, str]:
        found = self.lines(prefix)
        assert len(found) == 1, (prefix, self.out)
        return fields(found[0])

    @property
    def pre(self) -> dict[str, str]:
        return self.line(f"reconcile symbol={SYMBOL} ")

    @property
    def after(self) -> dict[str, str]:
        return self.line(f"reconcile-after symbol={SYMBOL} ")

    @property
    def limits(self) -> list[int]:
        return [limit for _, _, limit in self.source.kline_calls]


def fields(line: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in line.split() if "=" in part)


def assert_no_complete(text: str) -> None:
    for line in text.splitlines():
        if "reconcile" in line:
            assert "complete" not in line.lower(), line


def stop_at_sleep(clock: FakeClock, n: int = 1, **kwargs: Any) -> AdvancingSleep:
    calls = [0]

    def on_sleep(ms: int) -> None:
        calls[0] += 1
        if calls[0] == n:
            signal.raise_signal(signal.SIGTERM)

    return AdvancingSleep(clock, on_sleep=on_sleep)


def invoke(
    db: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    clock: FakeClock | None = None,
    source: FakeSource | None = None,
    sleep: AdvancingSleep | None = None,
    symbols: Sequence[str] = (SYMBOL,),
    opened: list[FakeSource] | None = None,
    **values: Any,
) -> Run:
    clock = clock or FakeClock()
    source = source or FakeSource(clock, [], honour_limit=True)
    sleep = sleep or stop_at_sleep(clock)

    async def open_source() -> KlineSource:
        if opened is not None:
            opened.append(source)
        return source

    settings: dict[str, Any] = {
        "database_path": db.name,
        "symbols": tuple(symbols),
        "max_concurrency": 1,
        "backfill_enabled": False,
        **values,
    }
    argv = ["run", str(write_config(db.parent, **settings))]
    code = cli.main(argv, open_source=open_source, clock=clock, sleep=sleep)
    captured = capsys.readouterr()
    assert_no_complete(captured.out + captured.err)
    assert "Traceback" not in captured.err
    return Run(code, captured.out, captured.err, source)


def advance_after_read(monkeypatch: pytest.MonkeyPatch, clock: FakeClock, ms: int) -> None:
    """``cli.reconcile`` runs for real; after the first (pre-start) read the clock moves on by ``ms``."""
    calls = [0]

    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, SeriesReconcile]:
        result = await REAL_RECONCILE(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 1:
            clock.advance(ms)
        return result

    monkeypatch.setattr(cli, "reconcile", wrapper)


class Partial(FakeSource):
    """Returns only the newest ``keep`` rows whatever was requested."""

    keep = 30

    def limited_rows(self, limit: int, tf_ms: int) -> list[list[Any]]:
        return super().limited_rows(limit, tf_ms)[-self.keep :]


class WriterSpy:
    def __init__(self) -> None:
        self.opened = 0
        self.reports: list[WriteReport] = []


@pytest.fixture
def writer_spy(monkeypatch: pytest.MonkeyPatch) -> WriterSpy:
    spy = WriterSpy()
    real_open = AsyncCandleWriter.open.__func__  # type: ignore[attr-defined]
    real_write = AsyncCandleWriter.write

    async def open_(cls: type[AsyncCandleWriter], /, path: Path, **kwargs: Any) -> AsyncCandleWriter:
        spy.opened += 1
        writer: AsyncCandleWriter = await real_open(cls, path, **kwargs)
        return writer

    async def write(self: AsyncCandleWriter, *args: Any, **kwargs: Any) -> WriteReport:
        report = await real_write(self, *args, **kwargs)
        spy.reports.append(report)
        return report

    monkeypatch.setattr(AsyncCandleWriter, "open", classmethod(open_))
    monkeypatch.setattr(AsyncCandleWriter, "write", write)
    return spy


# --- restart and catch-up -------------------------------------------------------------------------------------


def test_restart_after_an_outage_recovers_the_reachable_gap_and_the_database_proves_it(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db, slots(T - 70 * TF, T - 60 * TF))
    run = invoke(db, capsys, limit=10)
    assert run.code == 0
    assert run.pre["wanted_from"] == str(T - 59 * TF) and run.pre["catch_up_limit"] == "63"
    assert run.limits[0] == 63
    assert set(slots(T - 59 * TF, T)) <= stored(db)
    after = run.after
    assert (after["missing"], after["due"], after["stale"], after["window"]) == ("0", "0", "0", "window_no_gaps")
    assert after["all_present"] == "true" and after["absent_before"] == "60"


def test_old_gap_stays_visible_after_the_window_moves_past_it(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed(db, slots(T - 3000 * TF, T - 2000 * TF))
    run = invoke(db, capsys)
    pre = run.pre
    assert pre["unreachable_tail"] == "1003" and pre["needs_s16"] == "-"  # the history is read by the post-check
    assert pre["history"] == "unknown" and pre["wanted_from"] == str(WS)
    assert run.limits[0] == 1000
    assert set(slots(T - 998 * TF, T)) <= stored(db)
    after = run.after
    assert after["window"] == "window_no_gaps" and after["history"] == "holes"
    assert after["history_holes"] == "1001" and after["holes_older_than_window"] == "1001"
    assert after["needs_s16"] == "1001" and after["unknown_before"] == str(T - 3000 * TF)

    second = invoke(db, capsys)  # same database, a clock at the same instant
    assert second.lines(f"reconcile symbol={SYMBOL} ") == []  # the window is fine; the history is not read yet
    after = second.after  # the old hole is still seen, by the post-check of the second start
    assert (after["window"], after["history"], after["history_holes"], after["needs_s16"]) == (
        "window_no_gaps",
        "holes",
        "1001",
        "1001",
    )
    assert f"  hole {T - 1999 * TF}..{T - 998 * TF} (raw)" in second.out.splitlines()
    assert "history_complete" not in second.out + second.err


def test_partial_exchange_data_is_reported_not_claimed(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed(db, slots(T - 70 * TF, T - 60 * TF))
    clock = FakeClock()
    run = invoke(db, capsys, limit=10, clock=clock, source=Partial(clock, [], honour_limit=True))
    assert run.code == 0 and run.limits[0] == 63
    after = run.after
    assert after["absent_before"] == "60" and after["missing"] == "31"
    assert (after["due"], after["stale"], after["window"]) == ("0", "0", "window_gaps")
    assert run.lines("  still_missing") == [f"  still_missing {SYMBOL} {T - 59 * TF}..{T - 28 * TF} missing"]
    assert set(slots(T - 28 * TF, T)) <= stored(db) and T - 29 * TF not in stored(db)


def test_a_write_report_is_not_evidence(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db, slots(T - 70 * TF, T - 60 * TF))

    class Liar:
        async def write(self, candles: Sequence[object], *, ingested_at_ms: int) -> WriteReport:
            return WriteReport(inserted=len(candles), duplicates=0, conflicts=())

        async def close(self) -> None:
            pass

    async def open_(cls: type[AsyncCandleWriter], /, path: Path, **kwargs: Any) -> Liar:
        return Liar()

    monkeypatch.setattr(AsyncCandleWriter, "open", classmethod(open_))  # the class cli uses
    run = invoke(db, capsys, limit=10)
    pre, after = run.pre, run.after
    assert "collected=1" in run.out  # the loop believed the write report
    assert (after["missing"], after["due"], after["stale"]) == (pre["missing"], pre["due"], pre["stale"])
    assert int(after["due"]) + int(after["stale"]) == 60 and after["all_present"] == "false"
    assert "recovered" not in run.out.lower()


def test_run_stopped_after_a_partial_catch_up_and_started_again_converges(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db, slots(T - 70 * TF, T - 60 * TF))
    conflict_at(db, T - 65 * TF)
    reference = tmp_path / "reference.db"
    shutil.copy(db, reference)

    clock = FakeClock()
    assert invoke(db, capsys, limit=10, clock=clock, source=Partial(clock, [], honour_limit=True)).code == 0
    assert invoke(db, capsys, limit=10).code == 0
    assert invoke(reference, capsys, limit=10).code == 0

    assert table_rows(db, CANDLE_ROWS) == table_rows(reference, CANDLE_ROWS)
    assert table_rows(db, CONFLICT_ROWS) == table_rows(reference, CONFLICT_ROWS)


def test_second_start_adds_nothing(db: Path, writer_spy: WriterSpy, capsys: pytest.CaptureFixture[str]) -> None:
    seed(db, slots(T - 70 * TF, T - 60 * TF))
    conflict_at(db, T - 65 * TF)
    assert invoke(db, capsys, limit=10).code == 0
    full = "SELECT * FROM candles"
    conflicts = "SELECT * FROM candle_conflicts"
    before = (table_rows(db, full), table_rows(db, conflicts))
    writer_spy.reports.clear()

    clock = FakeClock()
    clock.advance(1_000)  # same slot, a different ingestion instant: a rewrite would show
    assert invoke(db, capsys, limit=10, clock=clock).code == 0
    assert (table_rows(db, full), table_rows(db, conflicts)) == before
    assert writer_spy.reports and all(report.inserted == 0 for report in writer_spy.reports)


# --- reach and boundaries -------------------------------------------------------------------------------------


def test_a_boundary_between_the_read_and_the_round_still_reaches_the_wanted_slot(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db, slots(F0 - 1100 * TF, F0 - 991 * TF))
    clock = FakeClock()
    advance_after_read(monkeypatch, clock, TF)
    run = invoke(db, capsys, clock=clock)
    assert run.pre["wanted_from"] == str(F0 - 990 * TF) and run.pre["catch_up_limit"] == "993"
    assert run.limits[0] == 994
    assert F0 - 990 * TF in stored(db)
    assert run.after["missing"] == "0" and run.after["history_holes"] == "0"


def test_three_boundaries_in_a_long_round_cost_exactly_one_edge_slot(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db, slots(F0 - 1100 * TF, T - 997 * TF))
    clock = FakeClock()
    advance_after_read(monkeypatch, clock, 3 * TF)
    run = invoke(db, capsys, clock=clock)
    assert run.pre["wanted_from"] == str(WS)
    assert run.limits[0] == 1000
    after = run.after
    assert (after["missing"], after["due"], after["stale"], after["window"]) == ("0", "0", "0", "window_no_gaps")
    assert (after["history_holes"], after["holes_older_than_window"], after["needs_s16"]) == ("1", "1", "1")
    assert WS not in stored(db) and set(slots(WS + TF, F0 + 2 * TF)) <= stored(db)
    assert run.lines("  still_missing") == []


def test_a_boundary_during_the_round_gives_no_false_still_missing(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed(db, slots(T - 70 * TF, T - 60 * TF))
    clock = FakeClock()
    calls = [0]

    def on_klines() -> None:
        calls[0] += 1
        if calls[0] == 1:
            clock.advance(TF)

    run = invoke(
        db, capsys, limit=10, clock=clock, source=FakeSource(clock, [], on_klines=on_klines, honour_limit=True)
    )
    assert run.limits[0] == 63
    after = run.after
    assert (after["missing"], after["due"], after["stale"], after["all_present"]) == ("0", "0", "0", "true")
    assert run.lines("  still_missing") == []


def test_a_300_000_ms_publication_delay_collects_and_the_window_ends_before_the_pending_slots(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = F0 - 2 * TF  # the newest slot with T + tf + 300 s <= F0 + 36 s
    seed(db, slots(F0 - 20 * TF, target - TF))
    run = invoke(db, capsys, publication_delay_ms=300_000)
    assert run.code == 0
    assert f"round target={target} collected=1" in run.out
    assert run.pre["wanted_from"] == str(target)
    assert (run.pre["due"], run.pre["stale"]) == ("1", "0")  # expected at T + tf + 300 s = F0, 36 s ago: due
    after = run.after
    assert (after["window"], after["all_present"], after["missing"]) == ("window_no_gaps", "true", "0")
    assert max(stored(db)) == target  # F0 - tf came back pending and were not stored


def test_the_configured_lag_threshold_turns_a_due_slot_into_a_stale_one(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = F0 - 2 * TF
    seed(db, slots(F0 - 20 * TF, target - TF))
    # the pending slot was expected 36 s ago: due under the committed 60 s, stale under 30 s
    run = invoke(db, capsys, publication_delay_ms=300_000, lag_threshold_ms=30_000)
    assert run.code == 0
    assert (run.pre["due"], run.pre["stale"]) == ("0", "1")


def test_a_delay_no_round_can_serve_is_a_usage_error(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    needed = steady_state_limit_needed(300_000, TF)
    clock = FakeClock()
    opened: list[FakeSource] = []
    with pytest.raises(SystemExit) as raised:
        invoke(db, capsys, publication_delay_ms=300_000, limit=needed - 1, clock=clock, opened=opened)
    assert raised.value.code == 2
    assert f"needs limit >= {needed}" in capsys.readouterr().err
    assert opened == [] and not db.exists()

    run = invoke(db, capsys, publication_delay_ms=300_000, limit=needed, opened=opened)
    assert run.code == 0 and len(opened) == 1 and run.limits[0] == needed


def test_new_symbol_is_no_baseline_and_a_young_series_is_not_backfilled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty.db"
    run = invoke(empty, capsys)
    assert run.limits[0] == 100 and run.pre["window"] == "no_baseline"

    young = tmp_path / "young.db"
    seed(young, slots(T - 10 * TF, T - TF))
    run = invoke(young, capsys)
    assert run.limits[0] == 100
    assert (run.pre["missing"], run.pre["history"]) == ("0", "unknown")
    assert run.after["history"] == "contiguous_from_oldest"
    assert run.pre["catch_up_limit"] == "100"  # the S14 request size: no larger request for a young series


def test_unavailable_and_conflicted_slots_next_to_a_real_gap(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed(db, [o for o in slots(T - 450 * TF, T) if o not in (T - 400 * TF, T - 200 * TF)])
    mark_unavailable(db, T - 400 * TF)
    conflict_at(db, T - 300 * TF)
    run = invoke(db, capsys)
    pre = run.pre
    assert (pre["missing"], pre["conflicted"], pre["unavailable"]) == ("1", "1", "1")
    assert pre["wanted_from"] == str(T - 200 * TF) and pre["catch_up_limit"] == "204"
    assert run.limits[0] == 204
    after = run.after
    assert (after["window"], after["all_present"], after["missing"]) == ("window_no_gaps", "false", "0")
    assert (after["conflicted"], after["unavailable"], after["history"]) == ("1", "1", "contiguous_from_oldest")


# --- failures and stops ---------------------------------------------------------------------------------------


def test_reconcile_failure_exits_1_before_any_network_call_or_writer_open(
    db: Path, writer_spy: WriterSpy, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> dict[str, SeriesReconcile]:
        raise ReconcileError("restart reconcile failed: boom")

    monkeypatch.setattr(cli, "reconcile", fail)
    handlers = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    opened: list[FakeSource] = []
    run = invoke(db, capsys, opened=opened)
    err = "\n".join(line for line in run.err.splitlines() if not line.startswith("migrated to"))  # OI-2 line
    assert run.code == 1 and "boom" in err and err.startswith("error:")
    assert opened == [] and writer_spy.opened == 0
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == handlers


def test_a_non_sqlite_file_is_a_reconcile_error_not_a_traceback(
    db: Path, writer_spy: WriterSpy, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db.write_bytes(b"this is not a database" * 200)
    opened: list[FakeSource] = []
    run = invoke(db, capsys, opened=opened)  # the migration, which runs first, refuses it
    assert run.code == 1 and run.err.startswith("error:") and opened == []

    monkeypatch.setattr(cli, "_migrate", lambda db, clock: (2, 2))  # the reconcile read itself refuses it as well
    run = invoke(db, capsys, opened=opened)
    assert run.code == 1 and "restart reconcile failed" in run.err
    assert opened == [] and writer_spy.opened == 0
    assert [line.split()[0] for line in run.out.splitlines()] == ["config"]  # nothing after the config line


def test_post_check_failure_withholds_the_claim_and_keeps_collecting(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db, slots(T - 70 * TF, T - 60 * TF))
    calls = [0]

    async def second_fails(*args: Any, **kwargs: Any) -> dict[str, SeriesReconcile]:
        calls[0] += 1
        if calls[0] == 2:
            raise ReconcileError("restart reconcile failed: disk gone")
        return await REAL_RECONCILE(*args, **kwargs)

    monkeypatch.setattr(cli, "reconcile", second_fails)
    clock = FakeClock()
    run = invoke(db, capsys, limit=10, clock=clock, sleep=stop_at_sleep(clock, 2))
    assert run.code == 0
    assert "reconcile-after status=unverified error=restart reconcile failed: disk gone" in run.err.splitlines()
    assert run.lines("reconcile-after") == []
    assert len(run.lines("round target=")) == 2 and "collected=1" in run.lines("round target=")[1]


def test_a_signal_during_the_pre_check_exits_0_without_a_source_call(
    db: Path, writer_spy: WriterSpy, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def signalled(*args: Any, **kwargs: Any) -> dict[str, SeriesReconcile]:
        signal.raise_signal(signal.SIGTERM)
        return await REAL_RECONCILE(*args, **kwargs)

    monkeypatch.setattr(cli, "reconcile", signalled)
    opened: list[FakeSource] = []
    run = invoke(db, capsys, opened=opened)
    assert run.code == 0 and opened == [] and writer_spy.opened == 0
    assert run.lines("round") == []


def test_a_signal_during_the_post_check_stops_after_the_read(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db, slots(T - 70 * TF, T - 60 * TF))
    calls = [0]

    async def signalled(*args: Any, **kwargs: Any) -> dict[str, SeriesReconcile]:
        calls[0] += 1
        if calls[0] == 2:
            signal.raise_signal(signal.SIGTERM)
        return await REAL_RECONCILE(*args, **kwargs)

    monkeypatch.setattr(cli, "reconcile", signalled)
    clock = FakeClock()
    sleep = AdvancingSleep(clock)
    run = invoke(db, capsys, limit=10, clock=clock, sleep=sleep)
    assert run.code == 0 and run.after["missing"] == "0"
    assert len(run.lines("round target=")) == 1 and sleep.calls == []


# --- flags and statements -------------------------------------------------------------------------------------


def test_clock_behind_stored_data_is_flagged_and_the_window_is_still_classified(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db, [o for o in slots(T - 20 * TF, T + 2 * TF) if o != T - 5 * TF])
    run = invoke(db, capsys, limit=5)
    pre = run.pre
    assert (pre["clock_behind_stored"], pre["missing"], pre["window"]) == ("true", "1", "window_gaps")
    assert (pre["history"], pre["history_holes"]) == ("unknown", "-")  # read by the post-check, after round one
    assert pre["wanted_from"] == str(T - 5 * TF) and pre["catch_up_limit"] == "9"
    assert run.limits[0] == 9  # (F0 - (T - 5·tf))/tf + 1 + 2


def seed_due(db: Path) -> str:
    seed(db, slots(T - 30 * TF, T - TF))
    return "due"


def seed_conflicted(db: Path) -> str:
    seed(db, slots(T - 30 * TF, T))
    conflict_at(db, T - 10 * TF)
    return "conflicted"


def seed_unavailable(db: Path) -> str:
    seed(db, [o for o in slots(T - 30 * TF, T) if o != T - 10 * TF])
    mark_unavailable(db, T - 10 * TF)
    return "unavailable"


@pytest.mark.parametrize("seeder", [seed_due, seed_conflicted, seed_unavailable], ids=["due", "conflict", "unavail"])
def test_due_conflicted_and_unavailable_slots_never_print_all_present_true(
    db: Path, seeder: Callable[[Path], str], capsys: pytest.CaptureFixture[str]
) -> None:
    count = seeder(db)
    run = invoke(db, capsys)
    pre = run.pre
    assert (pre["window"], pre["all_present"]) == ("window_no_gaps", "false")
    assert int(pre[count]) > 0
    summary = run.line("reconcile summary ")
    assert summary["not_all_present"] == "1" and summary["window_gaps"] == "0"


def test_no_reconcile_line_says_complete(db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # ``invoke`` checks every run of this module; this runs the cases most tempted to say so.
    seed(db, slots(T - 3000 * TF, T - 2000 * TF))
    outputs = [invoke(db, capsys), invoke(db, capsys)]
    full = tmp_path / "full.db"
    seed(full, slots(T - 30 * TF, T))
    outputs.append(invoke(full, capsys))
    for run in outputs:
        text = run.out + run.err
        assert any(line.startswith("reconcile") for line in text.splitlines())
        assert all("complete" not in line.lower() for line in text.splitlines() if "reconcile" in line)


def test_a1_exceeded_the_missed_slot_is_reported_from_the_database(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db, slots(F0 - 1100 * TF, T - 997 * TF))
    clock = FakeClock()
    clock.advance(4_000)  # F0 + 40 s
    source = FakeSource(clock, [], honour_limit=True, server_ahead_ms=3 * TF)
    run = invoke(db, capsys, clock=clock, source=source)
    assert run.code == 0
    assert run.pre["wanted_from"] == str(WS) and run.limits[0] == 1000
    assert set(slots(F0 - 996 * TF, F0 + 2 * TF)) <= stored(db) and WS not in stored(db)
    after = run.after
    assert (after["window"], after["missing"], after["clock_behind_stored"]) == ("window_gaps", "1", "true")
    assert (after["history_holes"], after["holes_older_than_window"], after["needs_s16"]) == ("1", "0", "0")
    assert run.line("reconcile-after summary ")["clock_lead_slots_max"] == "3"
    assert run.lines("  still_missing") == [f"  still_missing {SYMBOL} {WS}..{WS + TF} missing"]
    assert (
        "warning: the server's forming slot was 3 slots ahead of the local clock (assumption A1 allows 2);"
        " first-round requests may have missed up to 1 of the oldest wanted slots"
    ) in run.err.splitlines()


def test_a1_within_the_margin_is_silent(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed(db, slots(F0 - 1100 * TF, T - 997 * TF))
    clock = FakeClock()
    clock.advance(4_000)
    source = FakeSource(clock, [], honour_limit=True, server_ahead_ms=2 * TF)
    run = invoke(db, capsys, clock=clock, source=source)
    assert WS in stored(db)
    assert run.after["missing"] == "0" and run.line("reconcile-after summary ")["clock_lead_slots_max"] == "2"
    assert "warning" not in run.err


def test_600_symbols_with_gaps_in_five_use_large_requests_only_for_those_five(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    symbols = [f"S{i:03d}/USDT:USDT" for i in range(600)]
    gapped = set(symbols[::120])
    assert len(gapped) == 5
    seed_many(db, {s: slots(T - 40 * TF, T - 30 * TF) if s in gapped else slots(T - 2 * TF, T) for s in symbols})
    clock = FakeClock()

    def stop_before_round_two(ms: int) -> None:
        if ms > 1_000:  # the wait for the next boundary, not the limiter's spacing
            signal.raise_signal(signal.SIGTERM)

    sleep = AdvancingSleep(clock, on_sleep=stop_before_round_two)
    run = invoke(db, capsys, limit=5, clock=clock, sleep=sleep, symbols=symbols)
    assert run.code == 0
    by_symbol = {symbol: limit for symbol, _, limit in run.source.kline_calls}
    assert len(run.source.kline_calls) == 600
    assert {s for s, limit in by_symbol.items() if limit > 5} == gapped
    assert all(limit == 33 for s, limit in by_symbol.items() if s in gapped)
    assert all(limit == 5 for s, limit in by_symbol.items() if s not in gapped)
    assert run.line("reconcile summary ")["catch_up"] == "5"
    assert len(run.lines("reconcile symbol=")) == 5
    assert 100 in sleep.calls  # the limiter spaced the calls


# --- exact output formats -------------------------------------------------------------------------------------


def series(**overrides: Any) -> SeriesReconcile:
    values: dict[str, Any] = {
        "window": WindowStatus.WINDOW_GAPS,
        "all_present": False,
        "clock_behind_stored": False,
        "present": 990,
        "slots_classified": 997,
        "missing": 2,
        "due": 1,
        "stale": 3,
        "conflicted": 1,
        "unavailable": 0,
        "absent_runs": (
            GapRun(SlotState.MISSING, 1000 * TF, 1002 * TF),
            GapRun(SlotState.STALE, 1010 * TF, 1013 * TF),
            GapRun(SlotState.DUE, 1013 * TF, 1014 * TF),
        ),
        "oldest_wanted_ms": 1000 * TF,
        "catch_up_limit_at_read": 120,
        "unreachable_tail_slots": 0,
        "history": HistoryStatus.HOLES,
        "history_unaccounted_holes": 7,
        "history_holes_older_than_window": 5,
        "hole_runs": 2,
        "largest_hole_runs": ((500 * TF, 505 * TF), (600 * TF, 602 * TF)),
        "history_conflicted_slots": 1,
        "unknown_before_ms": 400 * TF,
        "needs_s16_slots": 5,
    }
    values.update(overrides)
    return SeriesReconcile(**values)


def clean(**overrides: Any) -> SeriesReconcile:
    values: dict[str, Any] = {
        "window": WindowStatus.WINDOW_NO_GAPS,
        "all_present": True,
        "present": 997,
        "missing": 0,
        "due": 0,
        "stale": 0,
        "conflicted": 0,
        "absent_runs": (),
        "oldest_wanted_ms": None,
        "catch_up_limit_at_read": 100,
        "history": HistoryStatus.CONTIGUOUS_FROM_OLDEST,
        "history_unaccounted_holes": 0,
        "history_holes_older_than_window": 0,
        "hole_runs": 0,
        "largest_hole_runs": (),
        "needs_s16_slots": 0,
    }
    return series(**{**values, **overrides})


def test_pre_check_lines_are_exact(capsys: pytest.CaptureFixture[str]) -> None:
    results = {
        "A": series(),
        "B": clean(),
        "C": series(
            window=WindowStatus.NO_BASELINE,
            present=0,
            slots_classified=0,
            missing=0,
            due=0,
            stale=0,
            conflicted=0,
            absent_runs=(),
            oldest_wanted_ms=None,
            catch_up_limit_at_read=100,
            history=HistoryStatus.UNKNOWN,
            history_unaccounted_holes=0,
            history_holes_older_than_window=0,
            hole_runs=0,
            largest_hole_runs=(),
            history_conflicted_slots=0,
            unknown_before_ms=None,
            needs_s16_slots=0,
        ),
    }
    cli._print_reconcile(results, 100)
    assert capsys.readouterr().out.splitlines() == [
        (
            f"reconcile symbol=A window=window_gaps all_present=false clock_behind_stored=false history=holes missing=2"
            f" due=1 stale=3 conflicted=1 unavailable=0 wanted_from={1000 * TF} unreachable_tail=0 history_holes=7"
            f" holes_older_than_window=5 needs_s16=5 unknown_before={400 * TF} catch_up_limit=120"
        ),
        f"  hole {500 * TF}..{505 * TF} (raw)",
        f"  hole {600 * TF}..{602 * TF} (raw)",
        (
            "reconcile symbol=C window=no_baseline all_present=false clock_behind_stored=false history=unknown missing=0"
            " due=0 stale=0 conflicted=0 unavailable=0 wanted_from=- unreachable_tail=0 history_holes=0"
            " holes_older_than_window=0 needs_s16=0 unknown_before=- catch_up_limit=100"
        ),
        (
            "reconcile summary symbols=3 window_gaps=1 not_all_present=2 due_slots=1 history_holes=7 needs_s16_slots=5"
            " catch_up=1"
        ),
    ]


def test_a_fully_present_series_with_old_holes_still_gets_a_line(capsys: pytest.CaptureFixture[str]) -> None:
    cli._print_reconcile({"B": clean(history=HistoryStatus.HOLES, history_unaccounted_holes=1)}, 100)
    assert capsys.readouterr().out.startswith("reconcile symbol=B window=window_no_gaps all_present=true ")


def test_post_check_lines_are_exact(capsys: pytest.CaptureFixture[str]) -> None:
    before = {"A": series(missing=5, due=1, stale=4), "B": clean()}
    after = {"A": series(), "B": clean()}
    cli._print_reconcile_after(after, before, 3)
    assert capsys.readouterr().out.splitlines() == [
        (
            "reconcile-after symbol=A window=window_gaps all_present=false clock_behind_stored=false history=holes"
            " missing=2 due=1 stale=3 conflicted=1 unavailable=0 absent_before=10 history_holes=7"
            f" holes_older_than_window=5 needs_s16=5 unknown_before={400 * TF}"
        ),
        f"  still_missing A {1000 * TF}..{1002 * TF} missing",
        f"  still_missing A {1010 * TF}..{1013 * TF} stale",
        f"  still_missing A {1013 * TF}..{1014 * TF} due",
        f"  hole {500 * TF}..{505 * TF} (raw)",
        f"  hole {600 * TF}..{602 * TF} (raw)",
        (
            "reconcile-after symbol=B window=window_no_gaps all_present=true clock_behind_stored=false"
            " history=contiguous_from_oldest missing=0 due=0 stale=0 conflicted=0 unavailable=0 absent_before=0"
            f" history_holes=0 holes_older_than_window=0 needs_s16=0 unknown_before={400 * TF}"
        ),
        (
            "reconcile-after summary symbols=2 window_gaps=1 not_all_present=1 due_slots=1 history_holes=7"
            " needs_s16_slots=5 clock_lead_slots_max=3"
        ),
    ]
    cli._print_reconcile_after({"B": clean()}, {"B": clean()}, None)
    assert capsys.readouterr().out.splitlines()[-1].endswith(" clock_lead_slots_max=-")


def test_the_pre_start_read_prints_an_unread_history_as_a_dash(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed(db, [*slots(T - 70 * TF, T - 65 * TF), *slots(T - 62 * TF, T - 60 * TF)])  # a window gap and an old hole
    run = invoke(db, capsys, limit=10)
    pre = run.pre
    assert pre["history"] == "unknown"
    assert (pre["history_holes"], pre["holes_older_than_window"], pre["needs_s16"]) == ("-", "-", "-")
    summary = run.line("reconcile summary ")
    assert (summary["history_holes"], summary["needs_s16_slots"]) == ("-", "-")
    assert run.lines("  hole ") == []  # the largest holes are reported by the post-check


class RoundTwoGate(FakeSource):
    """Sets ``reached`` when round two's klines request starts (the first request is round one's)."""

    def __init__(self, *args: Any, on_round_two: Callable[["RoundTwoGate"], None] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reached = threading.Event()
        self.requests = 0
        self.on_round_two = on_round_two

    async def fetch_klines(self, *args: Any, **kwargs: Any) -> Any:
        self.requests += 1
        if self.requests == 2:
            if self.on_round_two is not None:
                self.on_round_two(self)
            self.reached.set()
        return await super().fetch_klines(*args, **kwargs)


def hold_the_post_check_until(monkeypatch: pytest.MonkeyPatch, released: threading.Event) -> list[bool]:
    """The post-check's hole statement (only the post-check reads history) waits for ``released``, at most 5 s.

    Returns one entry per wait: ``True`` when ``released`` ended it, ``False`` when it timed out."""
    waits: list[bool] = []

    def before(sql: str, params: Any) -> None:
        if sql == reconcile_reader.HOLES_SQL:
            waits.append(released.wait(5))

    def opener(path: Path) -> StatementProxy:
        return StatementProxy(open_reader(path), before)

    monkeypatch.setattr("quasar_market_data.collector.reconcile_reader.open_reader", opener)
    return waits


def test_round_two_does_not_wait_for_the_post_check(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db, slots(T - 70 * TF, T - 60 * TF))
    clock = FakeClock()
    source = RoundTwoGate(clock, [], honour_limit=True)
    waits = hold_the_post_check_until(monkeypatch, source.reached)  # it can only finish once round two has started
    started = time.monotonic()
    run = invoke(db, capsys, limit=10, clock=clock, source=source, sleep=stop_at_sleep(clock, 2))
    assert waits == [True]  # round two started while the post-check was still reading (a blocking one never lets it)
    assert time.monotonic() - started < 4
    assert run.code == 0 and len(run.lines("round target=")) == 2
    assert run.after["missing"] == "0"  # and it still reports what round one left


def test_a_stop_during_the_post_check_exits_promptly_with_the_claim_withheld(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db, slots(T - 70 * TF, T - 60 * TF))
    monkeypatch.setattr(cli, "POST_CHECK_GRACE_MS", 50)
    interrupted = threading.Event()
    real_interrupt = reconcile_reader.ReadGuard.interrupt

    def interrupt(self: reconcile_reader.ReadGuard) -> None:
        real_interrupt(self)
        interrupted.set()

    monkeypatch.setattr(reconcile_reader.ReadGuard, "interrupt", interrupt)
    hold_the_post_check_until(monkeypatch, interrupted)  # still reading when the loop has stopped
    started = time.monotonic()
    run = invoke(db, capsys, limit=10)
    assert time.monotonic() - started < 3
    assert run.code == 0 and interrupted.is_set()
    assert run.lines("reconcile-after") == []
    assert any(line.startswith("reconcile-after status=unverified") for line in run.err.splitlines())


def test_the_post_check_prints_hole_lines_and_the_round_one_lead(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db, [*slots(T - 3000 * TF, T - 2500 * TF), *slots(T - 2400 * TF, T - 60 * TF)])  # one old hole

    def lead_more(source: RoundTwoGate) -> None:
        source.server_ahead_ms = 5 * TF  # a larger lead in round two must not reach the round-one post-check

    clock = FakeClock()
    source = RoundTwoGate(clock, [], honour_limit=True, server_ahead_ms=2 * TF, on_round_two=lead_more)
    hold_the_post_check_until(monkeypatch, source.reached)
    run = invoke(db, capsys, clock=clock, source=source, sleep=stop_at_sleep(clock, 2))
    assert run.code == 0
    assert f"  hole {T - 2499 * TF}..{T - 2400 * TF} (raw)" in run.out.splitlines()
    assert run.line("reconcile-after summary ")["clock_lead_slots_max"] == "2"
