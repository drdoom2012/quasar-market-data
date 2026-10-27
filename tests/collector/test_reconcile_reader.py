import asyncio
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector import reconcile_reader
from quasar_market_data.collector.candle_writer import CandleWriter
from quasar_market_data.collector.reconcile_reader import (
    ReconcileError,
    ReconcileInputs,
    expected_target,
    reconcile,
    reconcile_blocking,
)
from quasar_market_data.domain.reconcile import HistoryFacts, HistoryStatus, SeriesFacts, WindowStatus, reconcile_series
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.validation import require_valid
from quasar_market_data.storage.connection import StorageError, open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.statement_proxy import StatementProxy
from tests.domain.test_candle import make_candle, make_key

AT = 1_792_677_000_000
TF = Timeframe("5m")
TFMS = TF.milliseconds
BASE = 1_700_000_100_000
VENUE = "binance-usdm"
A = "BTC/USDT:USDT"
B = "ETH/USDT:USDT"
DELAY = 10_000


def slot(n: int) -> int:
    return BASE + n * TFMS


def as_of(n: int) -> int:
    """A moment inside slot ``n`` with the delay elapsed: the expected target is slot ``n - 1``."""
    return slot(n) + 20_000


def make_inputs(*symbols: str, max_limit: int = 1000, delay: int = DELAY) -> ReconcileInputs:
    return ReconcileInputs(
        symbols=symbols or (A,),
        venue=VENUE,
        timeframe=TF,
        publication_delay_ms=delay,
        default_limit=100,
        max_limit=max_limit,
    )


def put(
    db: Path, slots: list[int], *, symbol: str = A, venue: str = VENUE, timeframe: Timeframe = TF, **fields: str
) -> None:
    conn = open_writer(db)
    try:
        migrate(conn, applied_at_ms=AT)
        candles = [
            require_valid(
                make_candle(
                    key=make_key(
                        venue=venue, symbol=symbol, timeframe=timeframe, open_time_ms=BASE + n * timeframe.milliseconds
                    ),
                    **fields,
                )
            )
            for n in slots
        ]
        if candles:
            CandleWriter(conn).write(candles, ingested_at_ms=AT)
    finally:
        conn.close()


def mark_unavailable(db: Path, slots: list[int], *, symbol: str = A) -> None:
    conn = open_writer(db)
    try:
        for n in slots:
            conn.execute(
                "INSERT INTO unavailable_candles VALUES (?, ?, ?, ?, 'EXCHANGE_NO_DATA', ?)",
                (VENUE, symbol, TF.label, slot(n), AT),
            )
    finally:
        conn.close()


class Captured:
    def __init__(self) -> None:
        self.facts: list[SeriesFacts | None] = []
        self.history: list[HistoryFacts | None] = []


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "market.db"


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Captured:
    seen = Captured()
    real = reconcile_series

    def spy(facts: SeriesFacts | None, history: HistoryFacts | None, **kwargs: Any) -> Any:
        seen.facts.append(facts)
        seen.history.append(history)
        return real(facts, history, **kwargs)

    monkeypatch.setattr("quasar_market_data.collector.reconcile_reader.reconcile_series", spy)
    return seen


def run(db: Path, inputs: ReconcileInputs, now_slot: int) -> dict[str, Any]:
    now = as_of(now_slot)
    return reconcile_blocking(db, inputs, as_of_ms=now, target_open_ms=expected_target(inputs, now))


def history_of(captured: Captured) -> HistoryFacts:
    history = captured.history[-1]
    assert history is not None
    return history


def test_expected_target() -> None:
    assert expected_target(make_inputs(), as_of(8)) == slot(7)
    assert expected_target(make_inputs(), MIN_BEFORE_FIRST) is None


MIN_BEFORE_FIRST = 946_684_800_000


def test_window_sets_are_read(db: Path, captured: Captured) -> None:
    put(db, [0, 1, 2, 3, 4])
    put(db, [2], volume="7")  # a differing re-delivery of slot 2 is a recorded conflict
    mark_unavailable(db, [6])
    result = run(db, make_inputs(), 8)
    facts = captured.facts[-1]
    assert facts is not None
    assert facts.candles == {slot(n) for n in range(5)}
    assert facts.conflicts == {slot(2)}
    assert facts.unavailable == {slot(6)}
    assert result[A].window is WindowStatus.WINDOW_GAPS
    assert result[A].conflicted == 1 and result[A].unavailable == 1


def test_oldest_and_newest_are_series_wide_while_the_sets_are_window_wide(db: Path, captured: Captured) -> None:
    put(db, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20])
    # max_limit 8: the window starts at slot 17 - 5 = 12 for as_of slot 17
    run(db, make_inputs(max_limit=8), 17)
    facts = captured.facts[-1]
    assert facts is not None
    assert (facts.oldest_open_ms, facts.newest_open_ms) == (slot(0), slot(20))
    assert facts.candles == {slot(n) for n in range(12, 17)}


def test_history_holes_are_counted_exactly(db: Path, captured: Captured) -> None:
    put(db, [*range(10), *range(20, 30), 40])
    result = run(db, make_inputs(), 45)
    history = history_of(captured)
    assert history.raw_hole_slots == 20
    assert history.hole_runs == 2
    assert history.largest_runs == ((slot(10), slot(20)), (slot(30), slot(40)))
    assert result[A].history is HistoryStatus.HOLES
    assert result[A].history_unaccounted_holes == 20


def test_largest_runs_are_ordered_by_size_then_start_and_capped_at_five(db: Path, captured: Captured) -> None:
    slots = [0, 2, 4, 6, 8, 10, 12, 16]  # six holes of one slot, then one of three slots
    put(db, slots)
    run(db, make_inputs(), 20)
    history = history_of(captured)
    assert history.hole_runs == 7
    assert history.largest_runs == (
        (slot(13), slot(16)),
        (slot(1), slot(2)),
        (slot(3), slot(4)),
        (slot(5), slot(6)),
        (slot(7), slot(8)),
    )


def test_history_part_older_than_the_window_start_is_clipped(db: Path, captured: Captured) -> None:
    put(db, [*range(10), *range(20, 30), 40])
    mark_unavailable(db, [12])
    result = run(db, make_inputs(max_limit=33), 45)  # the window starts at slot 45 - 30 = 15
    history = history_of(captured)
    assert history.raw_hole_slots == 20
    assert history.raw_hole_slots_older_than_window == 5  # slots 10..14
    assert history.unavailable_in_holes_older_than_window == 1
    assert result[A].history_holes_older_than_window == 4


def test_unavailable_rows_inside_holes_are_counted_and_rows_outside_are_not(db: Path, captured: Captured) -> None:
    put(db, [*range(5, 15), *range(25, 35), 45])
    mark_unavailable(db, [20, 2, 7])  # in a hole; before the oldest candle; on a stored candle
    result = run(db, make_inputs(), 50)
    history = history_of(captured)
    assert history.unavailable_in_holes == 1
    assert history.raw_hole_slots == 20
    assert result[A].history_unaccounted_holes == 19


def test_history_conflicted_slots_count_distinct_open_times(db: Path, captured: Captured) -> None:
    put(db, [0, 1, 2])
    put(db, [1], volume="7")
    put(db, [1], volume="8")
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM candle_conflicts").fetchone()[0] == 2
    conn.close()
    result = run(db, make_inputs(), 5)
    assert history_of(captured).conflicted_slots == 1
    assert result[A].history_conflicted_slots == 1


def test_contiguous_series_has_zero_holes(db: Path, captured: Captured) -> None:
    put(db, list(range(10)))
    result = run(db, make_inputs(), 12)
    history = history_of(captured)
    assert (history.raw_hole_slots, history.hole_runs, history.largest_runs) == (0, 0, ())
    assert result[A].history is HistoryStatus.CONTIGUOUS_FROM_OLDEST


def test_a_single_candle_series_has_zero_holes(db: Path, captured: Captured) -> None:
    put(db, [3])
    run(db, make_inputs(), 8)
    history = history_of(captured)
    assert (history.raw_hole_slots, history.hole_runs, history.unavailable_in_holes) == (0, 0, 0)


def test_other_symbols_venues_and_timeframes_are_not_mixed_in(db: Path, captured: Captured) -> None:
    put(db, [0, 1, 2, 3])
    put(db, [8, 9], symbol=B)
    put(db, [0, 9], venue="other-venue")
    put(db, [0, 9], timeframe=Timeframe("1m"))
    mark_unavailable(db, [5], symbol=B)
    put(db, [2], volume="7", symbol=B)
    run(db, make_inputs(), 6)
    facts, history = captured.facts[-1], history_of(captured)
    assert facts is not None
    assert (facts.oldest_open_ms, facts.newest_open_ms) == (slot(0), slot(3))
    assert facts.unavailable == frozenset() and facts.conflicts == frozenset()
    assert (history.raw_hole_slots, history.unavailable_in_holes, history.conflicted_slots) == (0, 0, 0)


def test_result_keys_follow_the_configured_symbols(db: Path) -> None:
    put(db, [0, 1, 2], symbol=B)
    put(db, [0, 1, 2])
    result = run(db, make_inputs(B, A), 5)
    assert list(result) == [B, A]


def test_empty_database_gives_no_baseline_and_unknown_history(db: Path) -> None:
    put(db, [])
    result = run(db, make_inputs(), 8)
    assert result[A].window is WindowStatus.NO_BASELINE
    assert result[A].history is HistoryStatus.UNKNOWN
    assert result[A].unknown_before_ms is None


def test_an_empty_window_skips_the_window_reads_but_still_reads_the_history(db: Path, captured: Captured) -> None:
    put(db, [0, 1, 5])
    inputs = make_inputs(max_limit=4, delay=3 * TFMS)  # the window starts after the target
    now = as_of(100)
    result = reconcile_blocking(db, inputs, as_of_ms=now, target_open_ms=expected_target(inputs, now))
    assert result[A].window is WindowStatus.TARGET_OUT_OF_REACH
    facts = captured.facts[-1]
    assert facts is not None and facts.candles == frozenset()
    assert history_of(captured).raw_hole_slots == 3
    assert result[A].history is HistoryStatus.HOLES


def test_nothing_expected_reads_nothing(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(db, [0, 1])

    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("open_reader must not be called")

    monkeypatch.setattr("quasar_market_data.collector.reconcile_reader.open_reader", forbidden)
    result = reconcile_blocking(db, make_inputs(), as_of_ms=MIN_BEFORE_FIRST, target_open_ms=None)
    assert result[A].window is WindowStatus.NOTHING_EXPECTED
    assert result[A].history is HistoryStatus.UNKNOWN


def test_reconcile_uses_a_connection_that_cannot_write(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(db, [0, 1, 2])
    real = open_reader
    opened: list[int] = []

    def spy(path: Path) -> sqlite3.Connection:
        conn = real(path)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO unavailable_candles VALUES ('v', 's', '5m', 946684800000, 'EXCHANGE_NO_DATA', 1)")
        opened.append(1)
        return conn

    monkeypatch.setattr("quasar_market_data.collector.reconcile_reader.open_reader", spy)
    run(db, make_inputs(), 5)
    assert opened == [1]


def test_unmigrated_database_and_missing_file_raise_reconcile_error(tmp_path: Path) -> None:
    inputs = make_inputs()
    now = as_of(8)
    unmigrated = tmp_path / "unmigrated.db"
    open_writer(unmigrated).close()
    with pytest.raises(ReconcileError):
        asyncio.run(reconcile(unmigrated, inputs, as_of_ms=now, target_open_ms=slot(7)))
    missing = tmp_path / "missing.db"
    with pytest.raises(ReconcileError) as info:
        asyncio.run(reconcile(missing, inputs, as_of_ms=now, target_open_ms=slot(7)))
    assert isinstance(info.value, StorageError)
    assert info.value.__cause__ is not None
    assert not missing.exists()


def test_a_domain_error_becomes_a_reconcile_error(db: Path) -> None:
    put(db, [0, 1])
    with pytest.raises(ReconcileError) as info:
        asyncio.run(reconcile(db, make_inputs(), as_of_ms=as_of(8), target_open_ms=slot(50)))
    assert isinstance(info.value.__cause__, ValueError)


def test_reconcile_returns_the_blocking_result(db: Path) -> None:
    put(db, [0, 1, 2, 3])
    inputs = make_inputs()
    now = as_of(6)
    assert asyncio.run(reconcile(db, inputs, as_of_ms=now, target_open_ms=slot(5))) == reconcile_blocking(
        db, inputs, as_of_ms=now, target_open_ms=slot(5)
    )


def install_proxy(monkeypatch: pytest.MonkeyPatch, before: Any = None) -> list[StatementProxy]:
    proxies: list[StatementProxy] = []

    def opener(path: Path) -> StatementProxy:
        proxy = StatementProxy(open_reader(path), before)
        proxies.append(proxy)
        return proxy

    monkeypatch.setattr("quasar_market_data.collector.reconcile_reader.open_reader", opener)
    return proxies


def test_one_snapshot_for_all_symbols(db: Path, monkeypatch: pytest.MonkeyPatch, captured: Captured) -> None:
    put(db, [0, 1, 2, 3], symbol=A)
    put(db, [0, 1, 2, 3], symbol=B)
    fired: list[bool] = []

    def before(sql: str, params: Any) -> None:
        if sql == reconcile_reader.BOUNDS_SQL and params["symbol"] == B and not fired:
            fired.append(True)
            put(db, [4, 5], symbol=B)  # committed by a separate writer after the first symbol's reads

    install_proxy(monkeypatch, before)
    run(db, make_inputs(A, B), 8)
    assert fired == [True]
    second = captured.facts[-1]
    assert second is not None
    assert second.newest_open_ms == slot(3)
    assert second.candles == {slot(n) for n in range(4)}


def test_runs_in_a_worker_thread_and_closes_the_connection(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(db, [0, 1, 2])
    threads: list[int] = []
    real = open_reader

    def spy(path: Path) -> StatementProxy:
        threads.append(threading.get_ident())
        proxy = StatementProxy(real(path))
        proxies.append(proxy)
        return proxy

    proxies: list[StatementProxy] = []
    monkeypatch.setattr("quasar_market_data.collector.reconcile_reader.open_reader", spy)
    asyncio.run(reconcile(db, make_inputs(), as_of_ms=as_of(5), target_open_ms=slot(4)))
    assert threads and threads[0] != threading.get_ident()
    assert len(proxies) == 1 and proxies[0].closed
    assert proxies[0].statements[-1].sql == "ROLLBACK"


def test_the_connection_is_closed_and_rolled_back_when_a_read_fails(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(db, [0, 1, 2])

    def before(sql: str, params: Any) -> None:
        if sql == reconcile_reader.HOLES_SQL:
            raise sqlite3.OperationalError("boom")

    proxies = install_proxy(monkeypatch, before)
    with pytest.raises(sqlite3.OperationalError):
        run(db, make_inputs(), 5)
    assert proxies[0].closed
    assert proxies[0].statements[-1].sql == "ROLLBACK"


def test_the_history_statements_use_the_primary_key_and_no_table_scan(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(db, [*range(10), *range(20, 30), 40])
    put(db, [3], volume="7")
    mark_unavailable(db, [15])
    proxies = install_proxy(monkeypatch)
    run(db, make_inputs(), 45)
    raw = open_reader(db)
    seen: set[str] = set()
    for statement in proxies[0].statements:
        if not statement.sql.startswith("SELECT") or statement.sql in seen:
            continue
        seen.add(statement.sql)
        plan = [row[3] for row in raw.execute("EXPLAIN QUERY PLAN " + statement.sql, statement.params).fetchall()]
        if "sqlite_master" in statement.sql:
            continue
        for detail in plan:
            assert not re.search(r"SCAN (candles|unavailable_candles|candle_conflicts)\b", detail), detail
            if re.match(r"(SCAN|SEARCH) (candles|unavailable_candles|candle_conflicts|c|u)\b", detail):
                assert re.search(r"USING (PRIMARY KEY|INDEX|COVERING INDEX)", detail), detail
        if statement.sql == reconcile_reader.HOLES_SQL:
            assert not any("USE TEMP B-TREE" in detail for detail in plan), plan
        if statement.sql in (reconcile_reader.CANDLES_SQL, reconcile_reader.UNAVAILABLE_SQL):
            assert not any("USE TEMP B-TREE" in detail for detail in plan), plan
    raw.close()
    assert {reconcile_reader.HOLES_SQL, reconcile_reader.LARGEST_RUNS_SQL} <= seen
