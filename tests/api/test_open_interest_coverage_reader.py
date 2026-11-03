"""One request's reads for ``GET /v1/open-interest/coverage`` (contract api-v1 6.6): times only, one snapshot."""

import re
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from quasar_market_data.api.candle_reader import UnknownSeries
from quasar_market_data.api.open_interest_coverage_reader import (
    OpenInterestCoverageReport,
    read_open_interest_coverage_report,
)
from quasar_market_data.api.params import OpenInterestCoverageRequest
from quasar_market_data.domain.open_interest_coverage import (
    OpenInterestRule,
    OpenInterestRun,
    OpenInterestSlotState,
)
from quasar_market_data.domain.open_interest_request import RETRIEVAL_HORIZON_MS
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS
from quasar_market_data.storage.connection import open_reader, open_writer
from tests.api.probe import InterleavingConnection
from tests.api.seed import (
    SYMBOL,
    VENUE,
    insert_open_interest,
    insert_open_interest_conflict,
    make_database,
)

P5 = Timeframe("5m")
TF = 300_000
E = 500_000
L = 60_000
H = RETRIEVAL_HORIZON_MS
RULE = OpenInterestRule(cadence=1, expected_after_ms=E, lag_threshold_ms=L, period=P5)
S = 1792672500000  # the first slot of the contract example (6.5.4)
NOW = S + 100 * TF
ST = OpenInterestSlotState


def slot(n: int) -> int:
    return S + n * TF


def request(start: int, end: int, symbol: str = SYMBOL) -> OpenInterestCoverageRequest:
    return OpenInterestCoverageRequest(VENUE, symbol, P5, start, end)


def read(path: Path, req: OpenInterestCoverageRequest, as_of: int = NOW) -> OpenInterestCoverageReport:
    conn = open_reader(path)
    try:
        return read_open_interest_coverage_report(conn, req, as_of_ms=as_of, rule=RULE)
    finally:
        conn.close()


def test_open_interest_statements_select_only_times(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(3)], open_interest_conflicts=[slot(1)])
    reader = open_reader(path)
    log: list[str] = []
    reader.set_trace_callback(log.append)
    try:
        read_open_interest_coverage_report(reader, request(slot(0), slot(5)), as_of_ms=NOW, rule=RULE)
    finally:
        reader.set_trace_callback(None)
        reader.close()

    reads = [text for text in log if "open_interest" in text]
    assert len(reads) >= 3  # the bounds and the two time reads
    for text in reads:
        assert "open_interest_value" not in text
        assert text.lstrip().upper().startswith("SELECT"), text
        assert re.match(r"SELECT (DISTINCT )?time_ms FROM |SELECT \(SELECT MIN\(time_ms\)", text.lstrip()), text


def test_the_number_of_statements_does_not_depend_on_the_rows(tmp_path: Path) -> None:
    small = make_database(tmp_path / "s.db", open_interest=[slot(0), slot(1)])
    large = make_database(tmp_path / "l.db", open_interest=[slot(i) for i in range(2000)])
    counts = []
    for path in (small, large):
        reader = open_reader(path)
        probe = InterleavingConnection(reader, lambda n, sql: None)
        read_open_interest_coverage_report(
            cast(sqlite3.Connection, probe), request(slot(0), slot(2000)), as_of_ms=NOW, rule=RULE
        )
        counts.append(len(probe.statements))
        reader.close()

    assert counts[0] == counts[1]


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
        report = read_open_interest_coverage_report(cast(sqlite3.Connection, probe), window, as_of_ms=NOW, rule=RULE)

        assert report == read(pristine, window)  # none of the interleaved commits is visible
        assert len(injected) >= 3
        assert reader.in_transaction is False
        after = read_open_interest_coverage_report(reader, window, as_of_ms=NOW, rule=RULE)
        assert after != report
        assert after.runs.coverage.conflicted > report.runs.coverage.conflicted
    finally:
        reader.close()
        writer.close()


def test_a_series_with_only_conflict_records_is_unknown(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest_conflicts=[slot(0), slot(1)])

    with pytest.raises(UnknownSeries):
        read(path, request(slot(0), slot(3)))


def test_an_unknown_series_raises(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0)], candles=[slot(0)])

    with pytest.raises(UnknownSeries):
        read(path, request(slot(0), slot(3), symbol="ETH/USDT:USDT"))


def test_oldest_and_newest_ignore_conflicts(tmp_path: Path) -> None:
    path = make_database(
        tmp_path / "m.db",
        open_interest=[slot(2), slot(4)],
        open_interest_conflicts=[slot(0), slot(1), slot(6)],  # before the oldest row and after the newest row
    )

    report = read(path, request(slot(0), slot(7)))

    assert (report.oldest_time_ms, report.newest_time_ms) == (slot(2), slot(4))
    assert [run for run in report.runs.runs if run.state is ST.CONFLICTED] == [
        OpenInterestRun(ST.CONFLICTED, slot(0), slot(2)),
        OpenInterestRun(ST.CONFLICTED, slot(6), slot(7)),
    ]
    assert report.runs.coverage.conflicted == 3


def test_a_stopped_series_before_the_window(tmp_path: Path) -> None:
    newest = NOW - H - 10 * TF  # older than the horizon
    path = make_database(tmp_path / "m.db", open_interest=[newest - TF, newest])
    start = newest + TF
    end = NOW + 3 * TF  # reaches slots the clock has not made due yet

    report = read(path, request(start, end))

    states = [run.state for run in report.runs.runs]
    assert states[0] is ST.NO_LONGER_RECOVERABLE
    assert states[-1] is ST.NOT_YET_EXPECTED
    assert set(states) <= {ST.NO_LONGER_RECOVERABLE, ST.STALE, ST.DUE, ST.NOT_YET_EXPECTED}
    assert ST.STALE in states
    assert ST.MISSING not in states
    assert report.newest_time_ms == newest
    assert report.runs.coverage.present == 0


def test_the_report_carries_the_request_and_the_runs(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(3)], open_interest_conflicts=[slot(1)])

    report = read(path, request(slot(0), slot(5)))

    assert (report.venue, report.symbol, report.period, report.start, report.end) == (
        VENUE,
        SYMBOL,
        "5m",
        slot(0),
        slot(5),
    )
    assert (report.oldest_time_ms, report.newest_time_ms) == (slot(0), slot(3))
    assert report.runs.runs == (
        OpenInterestRun(ST.CONFLICTED, slot(1), slot(2)),
        OpenInterestRun(ST.MISSING, slot(2), slot(3)),
        OpenInterestRun(ST.STALE, slot(4), slot(5)),  # NOW is far past the slot and the lag tolerance
    )
    assert report.runs.coverage.present == 2


def test_an_unaligned_series_bound_is_refused_whatever_the_window(tmp_path: Path) -> None:
    """A hand-edited row that is the series minimum or maximum cannot be judged: fail closed, never echo it."""
    oldest = make_database(tmp_path / "o.db", open_interest=[slot(0) - 7, slot(2)])
    newest = make_database(tmp_path / "n.db", open_interest=[slot(0), slot(9) + 7])

    with pytest.raises(ValueError):
        read(oldest, request(slot(1), slot(4)))  # the minimum lies outside the window
    with pytest.raises(ValueError):
        read(newest, request(slot(0), slot(5)))  # the maximum lies outside the window


def test_an_unaligned_conflict_inside_the_window_is_refused(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", open_interest=[slot(0), slot(3)])
    writer = open_writer(path)
    insert_open_interest_conflict(writer, slot(1) + 7, 1)
    writer.close()

    with pytest.raises(ValueError):
        read(path, request(slot(0), slot(4)))


def test_a_window_may_end_one_period_after_the_last_valid_time(tmp_path: Path) -> None:
    last = MAX_TIMESTAMP_MS  # the last valid time_ms; the exclusive end is one period later
    path = make_database(tmp_path / "m.db", open_interest=[last])

    report = read(path, request(last - TF, last + TF))

    assert report.runs.coverage.slots == 2 and report.runs.coverage.present == 1
    assert report.runs.runs == (OpenInterestRun(ST.NOT_YET_EXPECTED, last - TF, last),)
    assert (report.oldest_time_ms, report.newest_time_ms) == (last, last)
