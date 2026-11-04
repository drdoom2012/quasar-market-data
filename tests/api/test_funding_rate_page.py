"""One request's reads of the funding rate store (contract api-v1 6.7): one snapshot, stored text, whole-window coverage."""

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from quasar_market_data.api.candle_reader import UnknownSeries
from quasar_market_data.api.funding_rate_page import FundingRatePage, read_funding_rate_page
from quasar_market_data.api.params import FundingRatesRequest
from quasar_market_data.collector.funding_rate_writer import FundingRateWriter, SpanMerge
from quasar_market_data.domain.funding_rate import FundingRate, FundingRateKey, RateType
from quasar_market_data.domain.funding_rate_coverage import WindowState
from quasar_market_data.domain.funding_rate_span import Interval
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.schema import SchemaMismatchError
from tests.api.probe import InterleavingConnection
from tests.api.seed import (
    SYMBOL,
    VENUE,
    funding_values,
    insert_candle,
    insert_funding_conflict,
    insert_funding_rate,
    make_database,
)

H = 3_600_000
EIGHT = 8 * H
T0 = 1_792_656_000_017  # a settlement 17 ms after a whole hour
NOW = T0 + 40 * EIGHT


def ev(n: int) -> int:
    return T0 + n * EIGHT


def request(start: int, end: int | None = None, limit: int = 1000, symbol: str = SYMBOL) -> FundingRatesRequest:
    return FundingRatesRequest(VENUE, symbol, start, end, limit)


def read(path: Path, req: FundingRatesRequest, end: int | None = None, as_of: int = NOW) -> FundingRatePage:
    conn = open_reader(path)
    try:
        effective = req.end if end is None else end
        assert effective is not None
        return read_funding_rate_page(conn, req, end=effective, as_of_ms=as_of)
    finally:
        conn.close()


def times(page: FundingRatePage) -> list[tuple[int, str]]:
    return [(row.funding_time_ms, row.rate_type) for row in page.rows]


def coverage_of(page: FundingRatePage) -> tuple[object, ...]:
    return (
        page.span,
        page.verified_at_ms,
        page.window_state,
        page.events_in_window,
        page.conflicted_events,
        page.complete,
    )


def test_values_are_returned_byte_for_byte_as_stored_with_a_null_mark_price(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[ev(0), ev(1)], funding_span=(ev(0), ev(5)))
    conn = open_writer(path)
    insert_funding_rate(conn, ev(2), funding_rate="-0.00005123", mark_price=None)
    conn.close()

    page = read(path, request(ev(0), ev(5)))

    assert [row.funding_time_ms for row in page.rows] == [ev(0), ev(1), ev(2)]
    assert (page.rows[0].funding_rate, page.rows[0].mark_price) == funding_values(ev(0))
    assert (page.rows[2].funding_rate, page.rows[2].mark_price) == ("-0.00005123", None)
    assert all(row.conflicted is False for row in page.rows)
    assert (page.venue, page.symbol, page.start, page.end) == (VENUE, SYMBOL, ev(0), ev(5))


def test_a_regular_row_comes_before_the_special_row_of_the_same_millisecond(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db", funding=[(ev(1), "Special"), (ev(1), "Regular"), ev(0)], funding_span=(ev(0), ev(2))
    )

    assert times(read(path, request(ev(0), ev(2)))) == [(ev(0), "Regular"), (ev(1), "Regular"), (ev(1), "Special")]


def test_the_window_is_closed_at_both_ends(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[ev(i) for i in range(5)], funding_span=(ev(0), ev(9)))

    page = read(path, request(ev(1), ev(3)))

    assert [t for t, _ in times(page)] == [ev(1), ev(2), ev(3)]  # both bounds included
    assert read(path, request(ev(1), ev(1))).events_in_window == 1  # a one-millisecond window holding a row
    assert read(path, request(ev(1) + 1, ev(2) - 1)).events_in_window == 0


def test_series_bounds_are_rows_of_the_whole_series_not_the_window(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[ev(i) for i in range(5)], funding_span=(ev(0), ev(9)))

    page = read(path, request(ev(2), ev(2)))

    assert (page.oldest_ms, page.newest_ms) == (ev(0), ev(4))


def test_a_window_of_2500_events_read_as_three_pages_page_one_describes_the_whole_window(tmp_path: Path) -> None:
    n = 2500
    path = make_database(
        tmp_path / "m.db",
        funding=[ev(i) for i in range(n)],
        funding_conflicts=[ev(7), ev(2400)],
        funding_span=(ev(0), ev(n)),
    )
    start, end = ev(0), ev(n - 1)
    pages, cursor = [], start
    while True:
        page = read(path, request(cursor, end, limit=1000))
        pages.append(page)
        if page.next is None:
            break
        cursor = page.next

    assert len(pages) == 3
    first = pages[0]
    assert first.events_in_window == n == sum(len(page.rows) for page in pages)  # the first page speaks for the window
    assert (first.conflicted_events, first.window_state, first.complete) == (2, WindowState.VERIFIED, False)
    # a continuation with start = next asks about the shorter window [next, end]: its coverage describes that window
    assert [page.events_in_window for page in pages] == [2500, 1500, 500]
    assert [page.conflicted_events for page in pages] == [2, 1, 1]
    assert {(page.span, page.verified_at_ms, page.window_state) for page in pages} == {
        (first.span, first.verified_at_ms, first.window_state)
    }
    seen = [row.funding_time_ms for page in pages for row in page.rows]
    assert seen == [ev(i) for i in range(n)]  # none repeated, none skipped
    assert [row.conflicted for page in pages for row in page.rows].count(True) == 2


def test_the_limit_changes_the_rows_and_next_but_not_the_coverage_of_the_same_window(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db",
        funding=[ev(i) for i in range(30)],
        funding_conflicts=[ev(7)],
        funding_span=(ev(0), ev(40)),
    )

    small = read(path, request(ev(0), ev(29), limit=3))
    large = read(path, request(ev(0), ev(29), limit=1000))

    assert (small.rows, small.next) != (large.rows, large.next)
    assert coverage_of(small) == coverage_of(large)
    assert small.events_in_window == 30 and small.conflicted_events == 1


def test_a_page_never_splits_the_rows_of_one_millisecond(tmp_path: Path) -> None:
    # rows 0..3 are single, row 4 has a Regular and a Special at the same millisecond
    funding: list[int | tuple[int, str]] = [ev(0), ev(1), ev(2), ev(3), (ev(4), "Regular"), (ev(4), "Special"), ev(5)]
    path = make_database(tmp_path / "m.db", funding=funding, funding_span=(ev(0), ev(9)))

    first = read(path, request(ev(0), ev(9), limit=5))  # the 5th and 6th row share a millisecond

    assert [t for t, _ in times(first)] == [ev(0), ev(1), ev(2), ev(3)]  # the pair is not split: 4 rows, not 5
    assert first.next == ev(4)
    second = read(path, request(first.next, ev(9), limit=5))
    assert times(second) == [(ev(4), "Regular"), (ev(4), "Special"), (ev(5), "Regular")]
    assert second.next is None


def test_limit_two_with_a_pair_on_every_boundary_still_advances_and_sees_every_row_once(tmp_path: Path) -> None:
    funding: list[int | tuple[int, str]] = []
    for i in range(6):
        funding += [(ev(i), "Regular"), (ev(i), "Special")]
    path = make_database(tmp_path / "m.db", funding=funding, funding_span=(ev(0), ev(9)))
    seen, cursor, loops = [], ev(0), 0
    while True:
        page = read(path, request(cursor, ev(9), limit=2))
        assert len(page.rows) in (1, 2)
        seen += times(page)
        loops += 1
        assert loops < 20
        if page.next is None:
            break
        assert page.next > page.rows[-1].funding_time_ms and page.next <= ev(9)
        cursor = page.next

    assert seen == sorted((ev(i), t) for i in range(6) for t in ("Regular", "Special"))


def test_limit_two_with_an_odd_alignment_drops_the_row_before_the_pair(tmp_path: Path) -> None:
    funding: list[int | tuple[int, str]] = [ev(0), (ev(1), "Regular"), (ev(1), "Special")]
    path = make_database(tmp_path / "m.db", funding=funding, funding_span=(ev(0), ev(9)))

    page = read(path, request(ev(0), ev(9), limit=2))

    assert times(page) == [(ev(0), "Regular")] and page.next == ev(1)


def test_a_full_last_page_has_no_next_and_next_never_passes_the_end(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[ev(i) for i in range(4)], funding_span=(ev(0), ev(9)))

    assert read(path, request(ev(0), ev(3), limit=4)).next is None  # exactly full, nothing after it
    partial = read(path, request(ev(0), ev(3), limit=3))
    assert partial.next == ev(3)
    assert read(path, request(ev(0), ev(2), limit=3)).next is None  # the row at ev(3) is outside the window


def test_a_conflicted_event_outside_the_page_counts_in_the_window_and_flags_no_row(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db",
        funding=[ev(i) for i in range(6)],
        funding_conflicts=[ev(5)],
        funding_span=(ev(0), ev(9)),
    )

    page = read(path, request(ev(0), ev(9), limit=3))  # the conflicted row is on the next page

    assert [row.conflicted for row in page.rows] == [False, False, False]
    assert (page.conflicted_events, page.complete, page.window_state) == (1, False, WindowState.VERIFIED)


def test_a_conflict_outside_the_window_is_ignored_and_one_without_a_row_counts(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db",
        funding=[ev(i) for i in range(4)],
        funding_conflicts=[ev(9), ev(2) + 5],  # outside the window; and one with no stored row
        funding_span=(ev(0), ev(20)),
    )

    page = read(path, request(ev(0), ev(3)))

    assert page.conflicted_events == 1 and page.complete is False
    assert all(row.conflicted is False for row in page.rows)  # a conflict record without a row flags nothing
    assert read(path, request(ev(0), ev(1))).complete is True  # the conflict is outside this window


def test_two_conflict_records_of_one_event_count_once_and_only_its_own_rate_type_is_flagged(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db",
        funding=[(ev(1), "Regular"), (ev(1), "Special")],
        funding_span=(ev(0), ev(2)),
    )
    conn = open_writer(path)
    insert_funding_conflict(conn, ev(1), 0, "Special")
    insert_funding_conflict(conn, ev(1), 1, "Special")
    conn.close()

    page = read(path, request(ev(0), ev(2)))

    assert page.conflicted_events == 1
    assert [(row.rate_type, row.conflicted) for row in page.rows] == [("Regular", False), ("Special", True)]


def test_window_state_edges_through_the_reader(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[ev(1)], funding_span=(ev(0), ev(4)))

    def state(start: int, end: int) -> WindowState:
        return read(path, request(start, end)).window_state

    assert state(ev(0), ev(4)) is WindowState.VERIFIED
    assert state(ev(0) - 1, ev(4)) is WindowState.PARTLY_VERIFIED
    assert state(ev(0), ev(4) + 1) is WindowState.PARTLY_VERIFIED
    assert state(ev(0) - 5, ev(0) - 1) is WindowState.UNVERIFIED
    assert state(ev(4) + 1, ev(4) + 5) is WindowState.UNVERIFIED


def test_without_a_span_the_window_is_unverified_and_the_span_fields_are_none(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[ev(1)])  # rows without a span exist

    page = read(path, request(ev(0), ev(2)))

    assert (page.span, page.verified_at_ms, page.window_state, page.complete) == (
        None,
        None,
        WindowState.UNVERIFIED,
        False,
    )
    assert page.events_in_window == 1


def test_the_span_and_its_verification_time_are_returned(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db", funding=[ev(1)], funding_span=(ev(0), ev(4)), funding_span_verified_at_ms=NOW - 5
    )

    page = read(path, request(ev(0), ev(4)))

    assert (page.span, page.verified_at_ms) == (Interval(ev(0), ev(4)), NOW - 5)


def test_a_span_without_rows_is_a_series_with_null_bounds(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding_span=(ev(0), ev(4)))

    page = read(path, request(ev(0), ev(4)))

    assert (page.rows, page.next, page.oldest_ms, page.newest_ms) == ((), None, None, None)
    assert (page.window_state, page.events_in_window, page.complete) == (WindowState.VERIFIED, 0, True)


def test_rows_without_a_span_exist_but_neither_nor_only_conflicts_is_unknown(tmp_path: Path) -> None:
    with_rows = make_database(tmp_path / "a.db", funding=[ev(0)])
    assert read(with_rows, request(ev(0), ev(1))).oldest_ms == ev(0)

    empty = make_database(tmp_path / "b.db", candles=[1_792_643_400_000])
    with pytest.raises(UnknownSeries):
        read(empty, request(ev(0), ev(1)))

    only_conflicts = make_database(tmp_path / "c.db", funding_conflicts=[ev(0)])
    with pytest.raises(UnknownSeries):
        read(only_conflicts, request(ev(0), ev(1)))


def test_another_symbol_or_venue_never_leaks(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[ev(0)], funding_span=(ev(0), ev(1)))
    conn = open_writer(path)
    insert_funding_rate(conn, ev(0), symbol="ETH/USDT:USDT")
    insert_funding_rate(conn, ev(1), venue="other-venue")
    conn.close()

    page = read(path, request(ev(0), ev(5)))

    assert times(page) == [(ev(0), "Regular")]
    with pytest.raises(UnknownSeries):
        read(path, request(ev(0), ev(5), symbol="SOL/USDT:USDT"))


def test_schema_mismatch_and_an_empty_database_raise_the_existing_errors(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    sqlite3.connect(path).close()
    conn = open_reader(path)
    try:
        with pytest.raises(Exception) as caught:  # EmptyDatabaseError, a SchemaMismatchError subclass
            read_funding_rate_page(conn, request(ev(0), ev(1)), end=ev(1), as_of_ms=NOW)
        assert isinstance(caught.value, SchemaMismatchError)
    finally:
        conn.close()


def test_a_limit_below_two_is_a_programming_error(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[ev(0)], funding_span=(ev(0), ev(1)))
    conn = open_reader(path)
    try:
        with pytest.raises(ValueError):
            read_funding_rate_page(conn, request(ev(0), ev(1), limit=1), end=ev(1), as_of_ms=NOW)
    finally:
        conn.close()


def test_the_interleaving_probe_sees_one_snapshot(tmp_path: Path) -> None:
    base = [ev(i) for i in range(0, 20, 2)]
    window = request(ev(0), ev(19))
    pristine = make_database(tmp_path / "pristine.db", funding=base, funding_span=(ev(0), ev(10)))
    subject = make_database(tmp_path / "subject.db", funding=base, funding_span=(ev(0), ev(10)))
    writer = open_writer(subject, busy_timeout_ms=1000)
    injected: list[tuple[int, str]] = []

    def hook(n: int, sql: str) -> None:
        if n < 2:
            return
        k = n - 1
        insert_funding_rate(writer, ev(2 * k - 1))
        insert_funding_conflict(writer, ev(2 * k), k)
        writer.execute("UPDATE funding_rate_verified SET through_ms = ? WHERE venue = ?", (ev(19), VENUE))
        injected.append((n, sql))

    reader = open_reader(subject)
    probe = InterleavingConnection(reader, hook)
    try:
        page = read_funding_rate_page(cast(sqlite3.Connection, probe), window, end=ev(19), as_of_ms=NOW)

        assert page == read(pristine, window)  # none of the interleaved commits is visible: no mixed state
        assert len(injected) >= 3 and page.window_state is WindowState.PARTLY_VERIFIED
        for table in ("FROM funding_rates ", "FROM funding_rate_conflicts"):
            assert any(table in sql for _n, sql in injected), table
        assert reader.in_transaction is False

        after = read_funding_rate_page(reader, window, end=ev(19), as_of_ms=NOW)
        assert after != page
        assert (after.span, after.window_state) == (
            Interval(ev(0), ev(19)),
            WindowState.VERIFIED,
        )  # the commits happened
        assert after.conflicted_events > page.conflicted_events
        assert after.events_in_window > page.events_in_window
    finally:
        reader.close()
        writer.close()


def test_the_state_changed_between_two_pages_shows_in_the_second_page_only(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[ev(i) for i in range(4)], funding_span=(ev(0), ev(9)))
    window = request(ev(0), ev(9), limit=2)
    first = read(path, window)
    assert (first.complete, first.conflicted_events) == (True, 0) and first.next == ev(2)

    writer = open_writer(path)
    insert_funding_conflict(writer, ev(3), 0)  # a conflict appears between page 1 and page 2
    writer.close()
    second = read(path, request(first.next, ev(9), limit=2))

    assert (second.complete, second.conflicted_events) == (False, 1)  # page 2 reflects the new state
    assert first.complete is True  # page 1 is not rewritten: no field claims consistency across pages


def test_the_statement_count_does_not_depend_on_the_rows(tmp_path: Path) -> None:
    small = make_database(tmp_path / "s.db", funding=[ev(0), ev(1)], funding_span=(ev(0), ev(5)))
    large = make_database(
        tmp_path / "l.db",
        funding=[ev(i) for i in range(400)],
        funding_conflicts=[ev(i) for i in range(0, 400, 3)],
        funding_span=(ev(0), ev(500)),
    )

    def count(path: Path, req: FundingRatesRequest) -> int:
        conn = open_reader(path)
        probe = InterleavingConnection(conn, lambda n, sql: None)
        try:
            read_funding_rate_page(cast(sqlite3.Connection, probe), req, end=req.end or 0, as_of_ms=NOW)
            return sum(1 for s in probe.statements if s.n)
        finally:
            conn.close()

    assert count(small, request(ev(0), ev(5))) == count(large, request(ev(0), ev(500), limit=50))


def test_every_statement_uses_a_primary_key_or_index_search(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db",
        funding=[ev(i) for i in range(20)],
        funding_conflicts=[ev(3)],
        funding_span=(ev(0), ev(30)),
    )
    conn = open_reader(path)
    probe = InterleavingConnection(conn, lambda n, sql: None)
    try:
        read_funding_rate_page(
            cast(sqlite3.Connection, probe), request(ev(0), ev(19), limit=5), end=ev(19), as_of_ms=NOW
        )
        data = [s for s in probe.statements if s.n and "funding_rate" in s.sql]
        assert len(data) >= 6
        for statement in data:
            plan = " ".join(
                row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + statement.sql, tuple(statement.parameters))
            )
            # the outer COUNT(*) reads the materialised subquery, whose own rows come from an index SEARCH
            assert "SCAN" not in plan.replace("SCAN CONSTANT ROW", "").replace("SCAN (subquery", ""), (
                statement.sql,
                plan,
            )
            assert "SEARCH" in plan or "CONSTANT" in plan, (statement.sql, plan)
    finally:
        conn.close()


def test_candles_in_the_same_database_change_nothing(tmp_path: Path) -> None:
    plain = make_database(tmp_path / "a.db", funding=[ev(0), ev(1)], funding_span=(ev(0), ev(3)))
    mixed = make_database(tmp_path / "b.db", funding=[ev(0), ev(1)], funding_span=(ev(0), ev(3)))
    conn = open_writer(mixed)
    insert_candle(conn, 1_792_643_400_000)
    conn.close()

    assert read(plain, request(ev(0), ev(3))) == read(mixed, request(ev(0), ev(3)))


def test_the_real_writer_and_the_reader_agree_on_conflicts_and_the_span(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db")
    conn = open_writer(path)
    writer = FundingRateWriter(conn)

    def row(time_ms: int, rate: str) -> FundingRate:
        return FundingRate(FundingRateKey(VENUE, SYMBOL, time_ms, RateType.REGULAR), rate, "81000.5")

    writer.write_transaction(
        [row(ev(0), "0.0001"), row(ev(1), "-0.0002")],
        ingested_at_ms=NOW,
        merge=SpanMerge(VENUE, SYMBOL, Interval(ev(0), ev(2)), NOW),
    )
    report = writer.write_transaction([row(ev(1), "-0.0003")], ingested_at_ms=NOW + 1)  # a differing re-delivery
    conn.close()
    assert report.conflicts and report.conflicts[0].newly_recorded

    page = read(path, request(ev(0), ev(2)))

    assert [(r.funding_rate, r.conflicted) for r in page.rows] == [("0.0001", False), ("-0.0002", True)]  # first values
    assert (page.window_state, page.conflicted_events, page.complete) == (WindowState.VERIFIED, 1, False)
    assert page.span == Interval(ev(0), ev(2))
