import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from quasar_market_data.api.candle_reader import UnknownSeries
from quasar_market_data.api.coverage_reader import CoverageReport, read_coverage_report
from quasar_market_data.api.database import read_coverage
from quasar_market_data.api.errors import ApiError
from quasar_market_data.api.params import MAX_WINDOW_SLOTS, CoverageRequest
from quasar_market_data.domain.timed_coverage import ExpectationRule, GapRun, SlotState
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from tests.api.probe import InterleavingConnection
from tests.api.seed import (
    AT,
    SYMBOL,
    VENUE,
    insert_candle,
    insert_conflict,
    insert_unavailable,
    make_database,
)

TF = 300_000
S = 1792643400000
RULE = ExpectationRule(15_000, 60_000)


def slot(n: int) -> int:
    return S + n * TF


def request(start: int, end: int, symbol: str = SYMBOL) -> CoverageRequest:
    return CoverageRequest(VENUE, symbol, Timeframe("5m"), start, end)


def read(path: Path, req: CoverageRequest, as_of_ms: int, rule: ExpectationRule = RULE) -> CoverageReport:
    conn = open_reader(path)
    try:
        return read_coverage_report(conn, req, as_of_ms=as_of_ms, rule=rule)
    finally:
        conn.close()


def test_each_state_is_counted_from_stored_facts(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db",
        candles=[slot(0), slot(1), slot(4)],
        unavailable=[slot(2), slot(7)],
        conflicts=[slot(3), slot(6)],
    )
    # slot(5) expected_at = slot(5) + TF + 15_000; one ms past its stale threshold (+60_000) makes it stale
    as_of = slot(5) + TF + 15_000 + 60_000 + 1

    report = read(path, request(slot(0), slot(10)), as_of)

    c = report.coverage
    assert (report.oldest_open_time_ms, report.newest_open_time_ms) == (slot(0), slot(4))
    assert (report.start, report.end) == (slot(0), slot(10))
    assert (report.venue, report.symbol, report.timeframe) == (VENUE, SYMBOL, "5m")
    assert (c.slots, c.present, c.conflicted, c.unavailable, c.missing) == (10, 3, 2, 2, 0)
    assert (c.stale, c.not_yet_expected, c.due) == (1, 2, 0)
    assert c.runs == (
        GapRun(SlotState.UNAVAILABLE, slot(2), slot(3)),
        GapRun(SlotState.CONFLICTED, slot(3), slot(4)),
        GapRun(SlotState.STALE, slot(5), slot(6)),
        GapRun(SlotState.CONFLICTED, slot(6), slot(7)),
        GapRun(SlotState.UNAVAILABLE, slot(7), slot(8)),
        GapRun(SlotState.NOT_YET_EXPECTED, slot(8), slot(10)),
    )


def test_slots_before_the_oldest_candle_are_missing(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(3), slot(4)])

    c = read(path, request(slot(0), slot(5)), slot(100)).coverage

    assert (c.missing, c.present, c.slots) == (3, 2, 5)
    assert c.runs == (GapRun(SlotState.MISSING, slot(0), slot(3)),)


def test_a_window_after_the_newest_candle_is_all_stale_for_a_dead_series(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0)])

    c = read(path, request(slot(5), slot(8)), slot(1000)).coverage

    assert (c.stale, c.slots, c.present, c.missing) == (3, 3, 0, 0)
    assert c.runs == (GapRun(SlotState.STALE, slot(5), slot(8)),)


def test_unknown_series_raises_unknown_series(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0)])
    writer = open_writer(path)
    insert_unavailable(writer, slot(0), symbol="ETH/USDT:USDT")
    insert_conflict(writer, slot(1), 0, symbol="SOL/USDT:USDT")
    writer.close()

    for symbol in ("ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"):
        with pytest.raises(UnknownSeries):
            read(path, request(slot(0), slot(3), symbol), slot(100))


def test_other_series_do_not_leak_in(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db", candles=[slot(0), slot(2), slot(4)], conflicts=[slot(2)], unavailable=[slot(3)]
    )
    before = read(path, request(slot(0), slot(6)), slot(100))

    writer = open_writer(path)
    for other in ({"symbol": "ETH/USDT:USDT"}, {"venue": "binance-spot"}, {"timeframe": "15m"}):
        for n in range(8):
            insert_candle(writer, slot(n), **other)
            insert_conflict(writer, slot(n), 1, **other)
        for n in (1, 5):
            insert_unavailable(writer, slot(n), **other)
    writer.close()

    assert read(path, request(slot(0), slot(6)), slot(100)) == before


def test_all_statements_run_in_one_read_transaction(tmp_path: Path) -> None:
    evens = [slot(i) for i in range(0, 21, 2)]
    window = request(slot(0), slot(21))
    pristine = make_database(tmp_path / "pristine.db", candles=evens)
    subject = make_database(tmp_path / "subject.db", candles=evens)
    writer = open_writer(subject, busy_timeout_ms=1000)
    injected: list[str] = []

    def hook(_n: int, sql: str) -> None:
        if "open_time_ms >=" not in sql:  # only the reads of the window's stored facts
            return
        k = len(injected) + 1
        insert_candle(writer, slot(2 * k - 1))
        insert_conflict(writer, slot(2 * k), k)
        injected.append(sql)

    reader = open_reader(subject)
    probe = InterleavingConnection(reader, hook)
    try:
        report = read_coverage_report(cast(sqlite3.Connection, probe), window, as_of_ms=slot(100), rule=RULE)

        assert report == read(pristine, window, slot(100))
        assert len(injected) >= 3
        for table in ("FROM candles", "FROM candle_conflicts", "FROM unavailable_candles"):
            assert any(table in sql for sql in injected), table
        assert reader.in_transaction is False
        after = read_coverage_report(reader, window, as_of_ms=slot(100), rule=RULE)
        assert after != report
    finally:
        reader.close()
        writer.close()


def test_a_write_between_statements_cannot_change_the_report(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(4)])
    writer = open_writer(path, busy_timeout_ms=1000)
    reader = open_reader(path)

    def hook(_n: int, sql: str) -> None:
        if "FROM candle_conflicts" in sql and "open_time_ms >=" in sql:  # after the candle read
            insert_candle(writer, slot(2))
            insert_unavailable(writer, slot(1))
            insert_candle(writer, slot(9))

    probe = InterleavingConnection(reader, hook)
    try:
        report = read_coverage_report(
            cast(sqlite3.Connection, probe), request(slot(0), slot(5)), as_of_ms=slot(100), rule=RULE
        )
        c = report.coverage
        assert report.newest_open_time_ms == slot(4)
        assert (c.present, c.missing, c.unavailable) == (2, 3, 0)
    finally:
        reader.close()
        writer.close()


def _max_window(tmp_path: Path, *, last_candle_in_window: bool) -> CoverageReport:
    req = request(slot(0), slot(MAX_WINDOW_SLOTS))
    candle = slot(MAX_WINDOW_SLOTS - 1) if last_candle_in_window else req.end
    path = make_database(
        tmp_path / "m.db", candles=[candle], conflicts=[slot(i) for i in range(0, MAX_WINDOW_SLOTS, 2)]
    )
    return read(path, req, slot(MAX_WINDOW_SLOTS + 1000))


def test_the_true_maximum_is_10000_runs_of_one_slot(tmp_path: Path) -> None:
    c = _max_window(tmp_path, last_candle_in_window=False).coverage

    assert (c.slots, c.conflicted, c.missing, c.present) == (10_000, 5000, 5000, 0)
    assert c.conflicted + c.missing + c.present + c.unavailable + c.stale + c.due + c.not_yet_expected == c.slots
    assert len(c.runs) == 10_000
    assert all(r.end_ms - r.start_ms == TF for r in c.runs)
    assert sum(r.end_ms - r.start_ms for r in c.runs) == 10_000 * TF
    assert [r.start_ms for r in c.runs] == [slot(i) for i in range(10_000)]
    assert [r.state for r in c.runs] == [SlotState.CONFLICTED, SlotState.MISSING] * 5000


def test_a_present_slot_is_never_listed_in_runs(tmp_path: Path) -> None:
    c = _max_window(tmp_path, last_candle_in_window=True).coverage

    assert c.present == 1
    assert len(c.runs) == 9999
    assert all(r.state is not SlotState.PRESENT for r in c.runs)


def test_database_errors_are_mapped_like_candles(tmp_path: Path) -> None:
    with pytest.raises(ApiError) as missing:
        read_coverage(tmp_path / "none.db", request(slot(0), slot(2)), as_of_ms=slot(5), rule=RULE)
    assert missing.value.details == {"reason": "database_missing"}

    path = make_database(tmp_path / "m.db", candles=[slot(0)])
    foreign = tmp_path / "foreign.db"
    raw = sqlite3.connect(foreign)
    raw.execute("PRAGMA application_id = 1")
    raw.execute("CREATE TABLE t (x)")
    raw.commit()
    raw.close()
    os.replace(foreign, path)
    with pytest.raises(ApiError) as replaced:
        read_coverage(path, request(slot(0), slot(2)), as_of_ms=slot(5), rule=RULE)
    assert replaced.value.details == {"reason": "schema_mismatch"}

    ok = make_database(tmp_path / "ok.db", candles=[slot(0)])
    with pytest.raises(ApiError) as unknown:
        read_coverage(ok, request(slot(0), slot(2), "ETH/USDT:USDT"), as_of_ms=slot(5), rule=RULE)
    assert unknown.value.status == 404


def _seeded(path: Path, rows: int) -> Path:
    conn = open_writer(path)
    try:
        migrate(conn, applied_at_ms=AT)
        conn.execute("BEGIN")
        for n in range(rows):
            insert_candle(conn, slot(n))
        conn.execute("COMMIT")
    finally:
        conn.close()
    return path


def _steps(path: Path, read: Callable[[sqlite3.Connection], object]) -> int:
    conn = open_reader(path)
    count = 0

    def tick() -> int:
        nonlocal count
        count += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        read(conn)
    finally:
        conn.set_progress_handler(None, 1)
        conn.close()
    return count


def test_series_bounds_cost_does_not_grow_with_rows(tmp_path: Path) -> None:
    small, big = _seeded(tmp_path / "small.db", 20), _seeded(tmp_path / "big.db", 5_000)

    def newest_ten(rows: int) -> Callable[[sqlite3.Connection], object]:
        return lambda conn: read_coverage_report(
            conn, request(slot(rows - 10), slot(rows)), as_of_ms=slot(rows) + 20_000, rule=RULE
        )

    small_steps, big_steps = _steps(small, newest_ten(20)), _steps(big, newest_ten(5_000))
    assert big_steps < 2 * small_steps + 50, (small_steps, big_steps)
