"""ADR-0005 P-3: the writers stamp under the lock, restamp when too old, confirm only within the bound."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from quasar_market_data.collector.candle_writer import CandleWriteError, CandleWriteNotStored, CandleWriter
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.faulty_connection import Fault, open_faulty_writer
from tests.collector.test_candle_writer import AT, valid

S = 1_000_000_000  # one second in ns


class Mono:
    """A monotonic clock that advances by ``step_ns`` on every read."""

    def __init__(self, step_ns: int = 0) -> None:
        self.now = 0
        self.step_ns = step_ns

    def __call__(self) -> int:
        self.now += self.step_ns
        return self.now


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_writer(tmp_path / "m.db")
    migrate(c, applied_at_ms=AT - 10)
    yield c
    c.close()


def stamps(conn: sqlite3.Connection, table: str) -> list[int]:
    return [r[0] for r in conn.execute(f"SELECT stamp_ms FROM {table} WHERE table_name = 'candles' ORDER BY 1")]


def test_a_stamp_is_the_wall_clock_then_unique_and_increasing(conn: sqlite3.Connection) -> None:
    writer = CandleWriter(conn, wall_ms=lambda: AT)
    assert writer.write([valid(0)]).stamp_ms == AT
    assert writer.write([valid(1)]).stamp_ms == AT + 1  # the same millisecond: last + 1
    assert stamps(conn, "write_transactions") == stamps(conn, "write_confirmations") == [AT, AT + 1]


def test_a_wall_clock_behind_the_cut_over_stamps_just_above_it(conn: sqlite3.Connection) -> None:
    assert CandleWriter(conn, wall_ms=lambda: AT - 500).write([valid(0)]).stamp_ms == AT - 8  # cut-over AT - 9, plus 1


def test_two_writer_connections_never_share_a_stamp(conn: sqlite3.Connection, tmp_path: Path) -> None:
    other = open_writer(tmp_path / "m.db")
    try:
        first = CandleWriter(conn, wall_ms=lambda: AT).write([valid(0)])
        second = CandleWriter(other, wall_ms=lambda: AT).write([valid(1)])
    finally:
        other.close()
    assert (first.stamp_ms, second.stamp_ms) == (AT, AT + 1)


def test_nothing_new_rolls_back_and_registers_nothing(conn: sqlite3.Connection) -> None:
    writer = CandleWriter(conn, wall_ms=lambda: AT)
    writer.write([valid(0)])
    report = writer.write([valid(0)])
    assert (report.duplicates, report.stamp_ms) == (1, None)
    assert stamps(conn, "write_transactions") == [AT]


def test_too_old_before_commit_is_restamped_and_never_committed_then_not_stored(conn: sqlite3.Connection) -> None:
    writer = CandleWriter(conn, wall_ms=lambda: AT, stamp_monotonic_ns=Mono(step_ns=2 * S))
    with pytest.raises(CandleWriteNotStored):
        writer.write([valid(0)])
    assert conn.execute("SELECT count(*) FROM candles").fetchone() == (0,)
    assert stamps(conn, "write_transactions") == []


def test_a_commit_slower_than_the_bound_is_stored_but_not_confirmed(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    reads = iter([0, 1 * S, 3 * S])  # register, before COMMIT (within B - M), after COMMIT (beyond B)
    writer = CandleWriter(conn, wall_ms=lambda: AT, stamp_monotonic_ns=lambda: next(reads))
    assert writer.write([valid(0)]).inserted == 1
    assert stamps(conn, "write_transactions") == [AT] and stamps(conn, "write_confirmations") == []
    assert "readability bound exceeded" in caplog.text


def test_a_failed_confirmation_is_no_write_error_and_is_carried_into_the_next_transaction(tmp_path: Path) -> None:
    conn = open_faulty_writer(tmp_path / "f.db", AT - 10)
    try:
        writer = CandleWriter(conn, wall_ms=lambda: AT)
        conn.faults.append(Fault("INSERT INTO write_confirmations", sqlite3.OperationalError("locked")))
        assert writer.write([valid(0)]).inserted == 1  # the data is committed; no exception
        assert stamps(conn, "write_confirmations") == []
        writer.write([valid(1)])
        assert stamps(conn, "write_confirmations") == [AT, AT + 1]
    finally:
        conn.close()


def test_an_outcome_unknown_commit_is_never_confirmed(tmp_path: Path) -> None:
    conn = open_faulty_writer(tmp_path / "f.db", AT - 10)
    try:
        writer = CandleWriter(conn, wall_ms=lambda: AT)
        conn.faults.append(Fault("COMMIT", sqlite3.OperationalError("disk I/O error"), after_execution=True))
        with pytest.raises(CandleWriteError):
            writer.write([valid(0)])
        assert stamps(conn, "write_transactions") == [AT] and stamps(conn, "write_confirmations") == []
    finally:
        conn.close()
