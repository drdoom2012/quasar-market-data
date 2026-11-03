"""One request's reads of the open interest store (contract api-v1 6.5): one snapshot, stored text, the seven states."""

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from quasar_market_data.api.candle_reader import UnknownSeries
from quasar_market_data.api.open_interest_page import OpenInterestPage, read_open_interest_page
from quasar_market_data.api.params import MAX_WINDOW_SLOTS, OpenInterestRequest
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.schema import SchemaMismatchError
from tests.api.probe import InterleavingConnection
from tests.api.seed import (
    SYMBOL,
    VENUE,
    insert_candle,
    insert_open_interest,
    insert_open_interest_conflict,
    make_database,
    open_interest_values,
)

P5 = Timeframe("5m")
TF = 300_000
E = 500_000
L = 60_000
RULE = OpenInterestRule(cadence=1, expected_after_ms=E, lag_threshold_ms=L, period=P5)
S = 1792672500000  # the first slot of the contract example (6.5.4)
NOW = S + 100 * TF  # a clock well after the small test data and inside the 29-day horizon


def slot(n: int) -> int:
    return S + n * TF


def request(
    start: int, end: int | None = None, limit: int = 1000, symbol: str = SYMBOL, venue: str = VENUE
) -> OpenInterestRequest:
    return OpenInterestRequest(venue, symbol, P5, start, end, limit)


def read(path: Path, req: OpenInterestRequest, as_of: int = NOW, rule: OpenInterestRule = RULE) -> OpenInterestPage:
    conn = open_reader(path)
    try:
        return read_open_interest_page(conn, req, as_of_ms=as_of, rule=rule)
    finally:
        conn.close()


def times(page: OpenInterestPage) -> list[int]:
    return [row.time_ms for row in page.rows]


def test_values_are_returned_byte_for_byte_as_stored(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(1)])

    page = read(path, request(slot(0)))

    for row in page.rows:
        assert (row.open_interest, row.open_interest_value) == open_interest_values(row.time_ms)
        assert row.conflicted is False
    assert (page.venue, page.symbol, page.period) == (VENUE, SYMBOL, "5m")


def test_rows_are_ascending_and_selected_by_time_ms_with_an_exclusive_end(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(i) for i in (4, 0, 2, 1, 3)])

    page = read(path, request(slot(1), slot(4)))

    assert times(page) == [slot(1), slot(2), slot(3)]
    assert (page.start, page.end, page.span_start, page.span_end, page.next) == (
        slot(1),
        slot(4),
        slot(1),
        slot(4),
        None,
    )
    assert (page.oldest_time_ms, page.newest_time_ms) == (slot(0), slot(4))


def test_the_defaulted_end_is_newest_plus_one_period_capped_at_ten_thousand_slots(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(5), slot(20_000)])

    assert read(path, request(slot(0))).end == slot(0) + MAX_WINDOW_SLOTS * TF
    assert read(path, request(slot(15_000), limit=5)).end == slot(20_000) + TF
    assert read(path, request(slot(10_000), limit=5)).end == slot(20_000)  # the cap: start + 10 000 slots
    near = make_database(tmp_path / "n.db", open_interest=[slot(0), slot(5)])
    assert read(near, request(slot(0))).end == slot(5) + TF


def test_a_start_after_the_newest_row_is_an_empty_one_slot_window(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0)])

    page = read(path, request(slot(5)))

    assert (page.rows, page.next, page.end) == ((), None, slot(5) + TF)
    assert page.coverage.slots == 1 and not page.coverage.gap_free


def test_an_exactly_full_last_page_has_next_none_and_paging_terminates(tmp_path: Path) -> None:
    every = [slot(i) for i in range(0, 50, 2)]
    path = make_database(tmp_path / "m.db", open_interest=every)
    seen: list[int] = []
    cursor, end = slot(0), slot(60)

    for _ in range(100):
        page = read(path, request(cursor, end, limit=5))
        seen.extend(times(page))
        assert page.span_end == (page.next if page.next is not None else end)
        if page.next is None:
            break
        assert page.next > page.rows[-1].time_ms
        cursor = page.next
    else:
        pytest.fail("the paging loop did not terminate")

    assert seen == every
    exact = read(path, request(slot(0), slot(60), limit=len(every)))
    assert exact.next is None and times(exact) == every


def test_an_unknown_series_raises(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0)], candles=[slot(0)])

    with pytest.raises(UnknownSeries):
        read(path, request(slot(0), symbol="ETH/USDT:USDT"))


def test_a_series_that_only_has_candles_is_unknown(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(1)])

    with pytest.raises(UnknownSeries):
        read(path, request(slot(0)))


def test_a_conflicted_slot_returns_the_first_stored_values_flagged_and_counted_once(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(1), slot(2)])
    conn = open_writer(path)
    insert_open_interest_conflict(conn, slot(1), 1)
    insert_open_interest_conflict(conn, slot(1), 2)  # a second differing observation of the same slot
    insert_open_interest_conflict(conn, slot(1), 1, symbol="ETH/USDT:USDT")  # another series: no leak
    conn.close()

    page = read(path, request(slot(0), slot(3)))

    assert [row.conflicted for row in page.rows] == [False, True, False]
    assert page.rows[1].open_interest == open_interest_values(slot(1))[0]  # the first stored, not the conflict's
    assert page.coverage.conflicted == 1 and page.coverage.present == 2
    assert page.coverage.conflicted_times == frozenset({slot(1)})
    assert not page.coverage.gap_free


def test_a_conflict_without_a_stored_row_is_counted_but_has_nothing_to_flag(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(3)], open_interest_conflicts=[slot(1)])

    page = read(path, request(slot(0), slot(4)))

    assert times(page) == [slot(0), slot(3)] and not any(row.conflicted for row in page.rows)
    assert page.coverage.conflicted == 1


def test_the_contract_example_through_the_reader(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db",
        open_interest=[slot(0), slot(1), slot(3)],
        open_interest_conflicts=[slot(1)],
    )
    as_of = slot(5) + E + 30_000

    page = read(path, request(slot(0), slot(7)), as_of)

    assert as_of == 1_792_674_530_000
    c = page.coverage
    assert (c.slots, c.present, c.conflicted, c.not_yet_expected, c.no_longer_recoverable) == (7, 2, 1, 1, 0)
    assert (c.due, c.stale, c.missing, c.gap_free) == (1, 1, 1, False)
    assert times(page) == [slot(0), slot(1), slot(3)]
    assert [row.conflicted for row in page.rows] == [False, True, False]


def test_candles_in_the_same_database_never_change_a_state(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(3)])
    before = read(path, request(slot(0), slot(4)))
    conn = open_writer(path)
    for n in range(4):
        insert_candle(conn, slot(n))
    conn.close()

    assert read(path, request(slot(0), slot(4))) == before


def test_a_database_with_another_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    open_writer(path).close()

    conn = open_reader(path)
    try:
        with pytest.raises(SchemaMismatchError):
            read_open_interest_page(conn, request(slot(0)), as_of_ms=NOW, rule=RULE)
    finally:
        conn.close()


def test_the_interleaving_probe_sees_one_snapshot(tmp_path: Path) -> None:
    evens = [slot(i) for i in range(0, 21, 2)]
    window = request(slot(0), slot(21))
    pristine = make_database(tmp_path / "pristine.db", open_interest=evens)
    subject = make_database(tmp_path / "subject.db", open_interest=evens)
    writer = open_writer(subject, busy_timeout_ms=1000)
    injected: list[tuple[int, str]] = []

    def hook(n: int, sql: str) -> None:
        if n < 2:
            return
        k = n - 1
        insert_open_interest(writer, slot(2 * k - 1))
        insert_open_interest_conflict(writer, slot(2 * k), k)
        injected.append((n, sql))

    reader = open_reader(subject)
    probe = InterleavingConnection(reader, hook)
    try:
        page = read_open_interest_page(cast(sqlite3.Connection, probe), window, as_of_ms=NOW, rule=RULE)

        assert page == read(pristine, window)  # none of the interleaved commits is visible
        assert len(injected) >= 3
        for table in ("FROM open_interest ", "FROM open_interest_conflicts"):
            assert any(table in sql and "time_ms >=" in sql for _n, sql in injected), table
        assert reader.in_transaction is False

        expected_final = make_database(
            tmp_path / "final.db",
            open_interest=evens + [slot(2 * k - 1) for k in range(1, len(injected) + 1)],
            open_interest_conflicts=[slot(2 * k) for k in range(1, len(injected) + 1)],
        )
        after = read_open_interest_page(reader, window, as_of_ms=NOW, rule=RULE)
        assert after == read(expected_final, window)
        assert after != page
        assert after.coverage.missing < page.coverage.missing  # the injected rows filled gaps
        assert after.coverage.conflicted > page.coverage.conflicted
    finally:
        reader.close()
        writer.close()


def test_the_number_of_statements_does_not_depend_on_the_rows(tmp_path: Path) -> None:
    small = make_database(tmp_path / "s.db", open_interest=[slot(0), slot(1)])
    large = make_database(tmp_path / "l.db", open_interest=[slot(i) for i in range(2000)])
    counts = []
    for path in (small, large):
        reader = open_reader(path)
        probe = InterleavingConnection(reader, lambda n, sql: None)
        read_open_interest_page(cast(sqlite3.Connection, probe), request(slot(0), slot(2000)), as_of_ms=NOW, rule=RULE)
        counts.append(len(probe.statements))
        reader.close()

    assert counts[0] == counts[1]


def test_a_conflict_beyond_the_end_of_a_cut_page_belongs_to_a_later_page(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db", open_interest=[slot(i) for i in range(10)], open_interest_conflicts=[slot(1), slot(8)]
    )

    first = read(path, request(slot(0), slot(10), limit=3))
    last = read(path, request(first.next or 0, slot(10), limit=3))
    tail = read(path, request(slot(6), slot(10), limit=3))

    assert first.next == slot(3) and first.coverage.conflicted == 1  # slot(1) only; slot(8) is on a later page
    assert [row.conflicted for row in first.rows] == [False, True, False]
    assert last.coverage.conflicted == 0
    assert tail.coverage.conflicted == 1 and [row.conflicted for row in tail.rows] == [False, False, True]
