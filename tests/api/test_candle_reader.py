import random
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from quasar_market_data.api.candle_reader import CandlesPage, UnknownSeries, read_candles_page
from quasar_market_data.api.params import MAX_WINDOW_SLOTS, CandlesRequest
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from quasar_market_data.storage.schema import SchemaMismatchError
from tests.api.probe import InterleavingConnection
from tests.api.seed import (
    AT,
    SYMBOL,
    VENUE,
    candle_values,
    insert_candle,
    insert_conflict,
    insert_unavailable,
    make_database,
)

TF = 300_000
S = 1792643400000


def slot(n: int) -> int:
    return S + n * TF


def request(start: int, end: int | None = None, limit: int = 1000, symbol: str = SYMBOL) -> CandlesRequest:
    return CandlesRequest(VENUE, symbol, Timeframe("5m"), start, end, limit)


def read(path: Path, req: CandlesRequest) -> CandlesPage:
    conn = open_reader(path)
    try:
        return read_candles_page(conn, req)
    finally:
        conn.close()


def opens(page: CandlesPage) -> list[int]:
    return [c.open_time_ms for c in page.candles]


def test_values_are_returned_byte_for_byte_as_stored(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(1)])

    page = read(path, request(slot(0)))

    for stored in page.candles:
        o, h, lo, c, v, t = candle_values(stored.open_time_ms)
        assert (stored.open, stored.high, stored.low, stored.close, stored.volume, stored.turnover) == (
            o,
            h,
            lo,
            c,
            v,
            t,
        )
    assert page.candles[0].close.isdigit()  # an integer value stays without ".0"
    assert page.candles[1].turnover is None  # an odd slot has no turnover, and it stays None


def test_unknown_series_raises(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0)])
    with pytest.raises(UnknownSeries):
        read(path, request(slot(0), symbol="ETH/USDT:USDT"))

    writer = open_writer(path)
    insert_unavailable(writer, slot(0), symbol="ETH/USDT:USDT")
    insert_conflict(writer, slot(1), 0, symbol="SOL/USDT:USDT")
    writer.close()
    for symbol in ("ETH/USDT:USDT", "SOL/USDT:USDT"):
        with pytest.raises(UnknownSeries):
            read(path, request(slot(0), symbol=symbol))


def test_existing_series_with_an_empty_window_is_a_page(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(10)])

    empty = read(path, request(slot(2), slot(5)))
    assert empty.candles == ()
    assert empty.next is None
    assert empty.coverage.missing == 3
    assert empty.coverage.gap_free is False

    reaching = read(path, request(slot(2), slot(12)))
    assert opens(reaching) == [slot(10)]
    assert reaching.next is None


def test_series_bounds_describe_the_whole_series_not_the_window(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(5), slot(9)])

    page = read(path, request(slot(4), slot(7)))

    assert (page.oldest_open_time_ms, page.newest_open_time_ms) == (slot(0), slot(9))


def test_other_series_never_leak_in(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db", candles=[slot(0), slot(2), slot(4)], conflicts=[slot(2)], unavailable=[slot(3)]
    )
    before = read(path, request(slot(0), slot(6)))

    writer = open_writer(path)
    for other in (
        {"symbol": "ETH/USDT:USDT"},
        {"venue": "binance-spot"},
        {"timeframe": "15m"},
    ):
        for n in range(8):
            insert_candle(writer, slot(n), **other)
            insert_conflict(writer, slot(n), 1, **other)
        for n in (1, 5):  # slots where the subject series has no unavailable record, so a leak would change a count
            insert_unavailable(writer, slot(n), **other)
    writer.close()

    assert read(path, request(slot(0), slot(6))) == before


def test_next_is_the_first_stored_candle_not_returned(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(1), slot(5), slot(6)])

    page = read(path, request(slot(0), slot(10), limit=2))

    assert opens(page) == [slot(0), slot(1)]
    assert page.next == slot(5)
    assert page.coverage.slots == 5  # the gap before `next` belongs to this page
    assert page.coverage.missing == 3


def test_an_exactly_full_last_page_has_next_none(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(1), slot(2)])

    page = read(path, request(slot(0), slot(3), limit=3))

    assert len(page.candles) == 3
    assert page.next is None


def test_next_is_none_when_the_remainder_of_the_window_is_empty(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(1), slot(20)])

    page = read(path, request(slot(0), slot(10), limit=2))

    assert len(page.candles) == 2
    assert page.next is None  # the candle at slot 20 is outside [start, end)


def test_paging_loop_terminates_and_sees_every_candle_once(tmp_path: Path) -> None:
    rng = random.Random(0)
    stored = sorted({slot(rng.randint(0, 300)) for _ in range(120)})
    path = make_database(tmp_path / "m.db", candles=stored)
    first = read(path, request(slot(0), limit=7))
    end = first.end

    seen: list[int] = []
    cursor = slot(0)
    previous_next = -1
    for _ in range(len(stored) + 1):
        page = read(path, request(cursor, end, limit=7))
        seen.extend(opens(page))
        if page.next is None:
            break
        assert page.next > previous_next
        assert page.next > page.candles[-1].open_time_ms
        previous_next = page.next
        cursor = page.next
    else:
        pytest.fail("the paging loop did not terminate")

    assert seen == stored


def test_span_ends_at_next_when_there_is_a_next(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(1), slot(4)])

    page = read(path, request(slot(0), slot(10), limit=2))

    assert page.next == slot(4)
    assert (page.span_start, page.span_end) == (slot(0), slot(4))
    assert page.coverage.slots == (page.next - page.start) // TF


def test_default_end_stops_at_the_newest_candle(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(5)])

    page = read(path, request(slot(0)))

    assert page.end == slot(6)
    assert (page.span_start, page.span_end) == (slot(0), slot(6))


def test_start_equal_to_the_newest_candle_returns_it(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(5)])

    page = read(path, request(slot(5)))

    assert opens(page) == [slot(5)]
    assert page.end == slot(6)


def test_default_end_is_capped_at_10000_slots(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(20_000)])

    page = read(path, request(slot(0)))

    assert page.end == slot(MAX_WINDOW_SLOTS)
    assert opens(page) == [slot(0)]
    assert page.next is None
    assert page.coverage.slots == MAX_WINDOW_SLOTS


def test_start_after_the_newest_candle_is_an_empty_one_slot_window(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0)])

    page = read(path, request(slot(10)))

    assert page.end == slot(11)
    assert page.candles == ()
    assert page.next is None
    assert page.coverage.beyond_stored == 1
    assert page.coverage.slots == 1
    assert page.coverage.gap_free is False


def test_explicit_end_is_echoed_unchanged(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(5)])

    assert read(path, request(slot(0), slot(50))).end == slot(50)


def test_a_gap_is_missing_and_slots_before_the_oldest_candle_are_missing(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(2), slot(5)])

    coverage = read(path, request(slot(0), slot(6))).coverage

    assert (coverage.present, coverage.missing, coverage.slots) == (2, 4, 6)


def test_a_conflicted_slot_returns_the_first_stored_value_flagged_and_counted_once(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(1)], conflicts=[slot(0)])
    writer = open_writer(path)
    insert_conflict(writer, slot(0), 1)  # a second observation of the same slot
    writer.close()

    page = read(path, request(slot(0), slot(2)))

    assert [c.conflicted for c in page.candles] == [True, False]
    assert page.candles[0].close == candle_values(slot(0))[3]
    assert page.coverage.conflicted == 1
    assert page.coverage.present == 1


def test_candle_plus_unavailable_record_is_conflicted_and_flagged(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(1)], unavailable=[slot(0)])

    page = read(path, request(slot(0), slot(2)))

    assert [c.conflicted for c in page.candles] == [True, False]
    assert (page.coverage.conflicted, page.coverage.unavailable, page.coverage.present) == (1, 0, 1)


def test_unavailable_only_slot_is_counted_and_has_no_candle(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(2)], unavailable=[slot(1)])

    page = read(path, request(slot(0), slot(3)))

    assert opens(page) == [slot(0), slot(2)]
    assert (page.coverage.unavailable, page.coverage.present, page.coverage.missing) == (1, 2, 0)


def test_counts_sum_to_slots_over_random_windows(tmp_path: Path) -> None:
    rng = random.Random(0)
    candles = {slot(0)} | {slot(rng.randint(0, 60)) for _ in range(30)}
    conflicts = {slot(rng.randint(0, 60)) for _ in range(10)}
    unavailable = {slot(rng.randint(0, 60)) for _ in range(10)}
    path = make_database(tmp_path / "m.db", candles=candles, conflicts=conflicts, unavailable=unavailable)

    for _ in range(200):
        start = slot(rng.randint(-5, 65))
        end = slot(rng.randint(-4, 70)) if rng.random() < 0.5 else None
        if end is not None and end <= start:
            end = start + TF
        page = read(path, request(start, end, limit=rng.randint(1, 10)))

        c = page.coverage
        assert c.present + c.conflicted + c.unavailable + c.missing + c.beyond_stored == c.slots
        assert c.slots == (page.span_end - page.span_start) // TF
        assert c.conflicted == len(c.conflicted_open_times)
        assert {x.open_time_ms for x in page.candles if x.conflicted} == c.conflicted_open_times & set(opens(page))


def test_the_interleaving_probe_can_observe_a_missing_snapshot(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0)])
    writer = open_writer(path, busy_timeout_ms=1000)
    assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    reader = open_reader(path)
    counter = iter(range(1, 100))

    def hook(n: int, sql: str) -> None:
        if n >= 2:
            insert_candle(writer, slot(next(counter)))

    def count(probe: InterleavingConnection) -> int:
        return int(probe.execute("SELECT COUNT(*) FROM candles").fetchone()[0])

    try:
        # (i) no transaction: each statement sees its own snapshot, so the second count differs
        probe = InterleavingConnection(reader, hook)
        first, second = count(probe), count(probe)
        assert second == first + 1

        # (ii) with a transaction the first read freezes the view; a commit before the second read is invisible
        probe = InterleavingConnection(reader, hook)
        probe.execute("BEGIN")
        frozen_first, frozen_second = count(probe), count(probe)
        assert frozen_second == frozen_first
        probe.execute("ROLLBACK")
        assert count(probe) == frozen_first + 2  # both interleaved commits are visible to a new snapshot
    finally:
        reader.close()
        writer.close()


def test_all_statements_run_in_one_read_transaction(tmp_path: Path) -> None:
    evens = [slot(i) for i in range(0, 21, 2)]
    window = request(slot(0), slot(21))
    pristine = make_database(tmp_path / "pristine.db", candles=evens)
    injections = range(1, 7)
    expected_final = make_database(
        tmp_path / "final.db",
        candles=evens + [slot(2 * k - 1) for k in injections],
        conflicts=[slot(2 * k) for k in injections],
    )
    subject = make_database(tmp_path / "subject.db", candles=evens)

    writer = open_writer(subject, busy_timeout_ms=1000)
    injected: list[tuple[int, str]] = []

    def hook(n: int, sql: str) -> None:
        if n < 2:
            return
        k = n - 1
        insert_candle(writer, slot(2 * k - 1))
        insert_conflict(writer, slot(2 * k), k)
        injected.append((n, sql))

    reader = open_reader(subject)
    probe = InterleavingConnection(reader, hook)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        page = read_candles_page(cast(sqlite3.Connection, probe), window)

        assert page == read(pristine, window)
        assert len(injected) >= 5
        for table in ("FROM candles", "FROM candle_conflicts", "FROM unavailable_candles"):
            assert any(table in sql and "open_time_ms >=" in sql for _n, sql in injected), table
        assert reader.in_transaction is False

        after = read_candles_page(reader, window)
        assert after == read(expected_final, window)
        assert after != page
        assert after.coverage.missing < page.coverage.missing  # injected candles filled gaps
        assert after.coverage.conflicted > page.coverage.conflicted
    finally:
        reader.close()
        writer.close()


def test_the_transaction_ends_on_every_path(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0)])
    conn = open_reader(path)
    try:
        read_candles_page(conn, request(slot(0)))
        assert conn.in_transaction is False
        with pytest.raises(UnknownSeries):
            read_candles_page(conn, request(slot(0), symbol="X/Y"))
        assert conn.in_transaction is False
    finally:
        conn.close()

    foreign = tmp_path / "foreign.db"
    raw = sqlite3.connect(foreign)
    raw.execute("PRAGMA application_id = 1")
    raw.execute("CREATE TABLE t (x)")
    raw.commit()
    raw.close()
    conn = open_reader(foreign)
    try:
        with pytest.raises(SchemaMismatchError):
            read_candles_page(conn, request(slot(0)))
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_schema_problems_raise_schema_mismatch_error(tmp_path: Path) -> None:
    unmigrated = tmp_path / "empty.db"
    sqlite3.connect(unmigrated).close()
    foreign = tmp_path / "foreign.db"
    raw = sqlite3.connect(foreign)
    raw.execute("PRAGMA application_id = 1")
    raw.execute("CREATE TABLE t (x)")
    raw.commit()
    raw.close()
    newer = make_database(tmp_path / "newer.db", candles=[slot(0)])
    raw = sqlite3.connect(newer)
    raw.execute("INSERT INTO schema_migrations VALUES (2, 'x', 'x', 0)")
    raw.commit()
    raw.close()

    for path in (unmigrated, foreign, newer):
        with pytest.raises(SchemaMismatchError):
            read(path, request(slot(0)))


@pytest.mark.parametrize("stored", [10, 1000])
def test_the_cost_is_bounded(tmp_path: Path, stored: int) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(i) for i in range(stored)])
    conn = open_reader(path)
    probe = InterleavingConnection(conn, lambda n, sql: None)
    try:
        read_candles_page(cast(sqlite3.Connection, probe), request(slot(0), limit=5))
    finally:
        conn.close()

    statements = probe.statements
    assert len(statements) <= 9
    touching = [
        s for s in statements if any(t in s.sql for t in ("candles", "candle_conflicts", "unavailable_candles"))
    ]
    assert touching
    assert all(
        "?" in s.sql for s in touching if "FROM candles" in s.sql or "FROM candle_" in s.sql or "unavailable" in s.sql
    )
    candle_read = next(s for s in statements if "FROM candles" in s.sql and "ORDER BY" in s.sql)
    assert "LIMIT ?" in candle_read.sql
    assert candle_read.parameters[-1] == 6
    assert len(statements) == 9  # identical for 10 and 1000 stored candles


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
        return lambda conn: read_candles_page(conn, request(slot(rows - 10), slot(rows)))

    small_steps, big_steps = _steps(small, newest_ten(20)), _steps(big, newest_ten(5_000))
    assert big_steps < 2 * small_steps + 50, (small_steps, big_steps)
