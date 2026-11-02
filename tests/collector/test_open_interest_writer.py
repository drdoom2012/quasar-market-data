import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from quasar_market_data.collector.open_interest_writer import (
    MAX_ROWS_PER_TRANSACTION,
    OpenInterestWriteBusy,
    OpenInterestWriteError,
    OpenInterestWriteOutcomeUnknown,
    OpenInterestWriter,
)
from quasar_market_data.domain.candle import Candle, CandleKey
from quasar_market_data.domain.open_interest import OpenInterest, OpenInterestKey
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import load_migrations, migrate
from quasar_market_data.storage.schema import SchemaMismatchError
from tests.collector.faulty_connection import Fault, FaultyConnection, open_faulty_writer
from tests.typing_helpers import untyped

AT = 1_793_621_000_000
T = 1_793_620_800_000
P5 = Timeframe("5m")
INSERT_ROW = "INSERT INTO open_interest ("


def oi(index: int = 0, value: str = "1", value_usd: str = "2") -> OpenInterest:
    return OpenInterest(OpenInterestKey("binance-usdm", "BTC/USDT:USDT", P5, T + index * 300_000), value, value_usd)


def stored(db: Path) -> list[tuple[object, ...]]:
    conn = open_reader(db)
    try:
        return conn.execute(
            "SELECT time_ms, open_interest, open_interest_value, ingested_at_ms FROM open_interest ORDER BY time_ms"
        ).fetchall()
    finally:
        conn.close()


def conflict_rows(db: Path) -> list[tuple[object, ...]]:
    conn = open_reader(db)
    try:
        return conn.execute(
            "SELECT time_ms, open_interest, open_interest_value, first_seen_at_ms FROM open_interest_conflicts ORDER BY conflict_id"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "market.db"


@pytest.fixture
def writer(db: Path) -> Iterator[OpenInterestWriter]:
    conn = open_writer(db)
    migrate(conn, applied_at_ms=AT)
    w = OpenInterestWriter(conn)
    yield w
    if not w.unusable:
        w.close()


def faulty(db: Path) -> tuple[FaultyConnection, OpenInterestWriter]:
    conn = open_faulty_writer(db, AT)
    w = OpenInterestWriter(conn)
    conn.statements.clear()
    return conn, w


def test_rows_are_inserted_once(writer: OpenInterestWriter, db: Path) -> None:
    rows = [oi(0), oi(1), oi(2)]
    report = writer.write_transaction(rows, ingested_at_ms=AT)
    assert (report.inserted, report.duplicates, report.conflicts) == (3, 0, ())
    again = writer.write_transaction(rows, ingested_at_ms=AT + 1)
    assert (again.inserted, again.duplicates, again.conflicts) == (0, 3, ())
    assert [row[3] for row in stored(db)] == [AT, AT, AT]


def test_differing_row_is_recorded_not_written_over(writer: OpenInterestWriter, db: Path) -> None:
    writer.write_transaction([oi(0, "1", "2")], ingested_at_ms=AT)
    report = writer.write_transaction([oi(0, "1", "3")], ingested_at_ms=AT + 5)
    assert report.inserted == 0 and report.duplicates == 0
    (conflict,) = report.conflicts
    assert conflict.incoming == oi(0, "1", "3")
    assert conflict.stored == oi(0, "1", "2")
    assert conflict.newly_recorded is True
    assert stored(db) == [(T, "1", "2", AT)]
    assert conflict_rows(db) == [(T, "1", "3", AT + 5)]


def test_same_differing_value_twice_is_one_conflict_row(writer: OpenInterestWriter, db: Path) -> None:
    writer.write_transaction([oi(0, "1", "2")], ingested_at_ms=AT)
    writer.write_transaction([oi(0, "1", "3")], ingested_at_ms=AT + 5)
    report = writer.write_transaction([oi(0, "1", "3")], ingested_at_ms=AT + 9)
    assert [c.newly_recorded for c in report.conflicts] == [False]
    assert conflict_rows(db) == [(T, "1", "3", AT + 5)]


def test_more_than_100_rows_is_refused_before_any_sql(db: Path) -> None:
    conn, w = faulty(db)
    rows = [oi(i) for i in range(MAX_ROWS_PER_TRANSACTION + 1)]
    with pytest.raises(ValueError, match="100"):
        w.write_transaction(rows, ingested_at_ms=AT)
    assert conn.statements == []
    assert w.write_transaction(rows[:MAX_ROWS_PER_TRANSACTION], ingested_at_ms=AT).inserted == 100


def test_rows_must_be_open_interest(writer: OpenInterestWriter, db: Path) -> None:
    candle = Candle(CandleKey("binance-usdm", "BTC/USDT:USDT", P5, T), "1", "1", "1", "1", "1", None)
    for bad in (candle, object()):
        with pytest.raises(TypeError):
            writer.write_transaction([oi(0), untyped(bad)], ingested_at_ms=AT)
    assert stored(db) == []


def test_empty_rows_run_no_transaction(db: Path) -> None:
    conn, w = faulty(db)
    report = w.write_transaction([], ingested_at_ms=AT)
    assert (report.inserted, report.duplicates, report.conflicts) == (0, 0, ())
    assert conn.statements == []


def test_failure_mid_transaction_rolls_back(db: Path) -> None:
    conn, w = faulty(db)
    conn.faults.append(Fault(INSERT_ROW, sqlite3.OperationalError("injected"), nth=2))
    rows = [oi(0), oi(1), oi(2)]
    with pytest.raises(OpenInterestWriteError, match="nothing was written") as error:
        w.write_transaction(rows, ingested_at_ms=AT)
    assert type(error.value) is OpenInterestWriteError
    assert stored(db) == []
    assert w.write_transaction(rows, ingested_at_ms=AT).inserted == 3


def test_commit_that_took_effect_is_outcome_unknown(db: Path) -> None:
    conn, w = faulty(db)
    conn.faults.append(Fault("COMMIT", sqlite3.OperationalError("disk I/O error"), after_execution=True))
    rows = [oi(0), oi(1)]
    with pytest.raises(OpenInterestWriteOutcomeUnknown):
        w.write_transaction(rows, ingested_at_ms=AT)
    retry = w.write_transaction(rows, ingested_at_ms=AT + 1)
    assert (retry.inserted, retry.duplicates, retry.conflicts) == (0, 2, ())


def test_busy_is_classified(db: Path) -> None:
    conn = open_writer(db, busy_timeout_ms=50)
    migrate(conn, applied_at_ms=AT)
    w = OpenInterestWriter(conn)
    holder = open_writer(db)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(OpenInterestWriteBusy):
            w.write_transaction([oi(0)], ingested_at_ms=AT)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert stored(db) == []
    assert w.write_transaction([oi(0)], ingested_at_ms=AT).inserted == 1
    w.close()


def test_failed_rollback_closes_the_connection(db: Path) -> None:
    conn, w = faulty(db)
    conn.faults.append(Fault(INSERT_ROW, sqlite3.OperationalError("injected")))
    conn.faults.append(Fault("ROLLBACK", sqlite3.OperationalError("rollback failed")))
    with pytest.raises(OpenInterestWriteError):
        w.write_transaction([oi(0)], ingested_at_ms=AT)
    assert w.unusable
    with pytest.raises(OpenInterestWriteError, match="unusable"):
        w.write_transaction([oi(0)], ingested_at_ms=AT)
    other = open_writer(db, busy_timeout_ms=0)
    try:
        other.execute("BEGIN IMMEDIATE")  # the write lock was released by closing the connection
        other.execute("ROLLBACK")
    finally:
        other.close()


def test_constructor_refuses_v1_database_and_bad_connections(tmp_path: Path) -> None:
    v1 = open_writer(tmp_path / "v1.db")
    migrate(v1, applied_at_ms=AT, migrations=load_migrations()[:1])
    with pytest.raises(SchemaMismatchError):
        OpenInterestWriter(v1)
    v1.close()

    path = tmp_path / "v2.db"
    good = open_writer(path)
    migrate(good, applied_at_ms=AT)
    good.close()

    reader = open_reader(path)
    with pytest.raises(OpenInterestWriteError, match="read-only"):
        OpenInterestWriter(reader)
    reader.close()

    no_triggers = open_writer(path)
    no_triggers.execute("PRAGMA recursive_triggers=OFF")
    with pytest.raises(OpenInterestWriteError, match="recursive_triggers"):
        OpenInterestWriter(no_triggers)
    no_triggers.close()

    implicit = sqlite3.connect(path)
    with pytest.raises(OpenInterestWriteError, match="isolation_level"):
        OpenInterestWriter(implicit)
    implicit.close()


def test_durations_are_measured(db: Path) -> None:
    conn = open_writer(db)
    migrate(conn, applied_at_ms=AT)
    ticks = iter([100, 130, 380])
    w = OpenInterestWriter(conn, monotonic_ns=lambda: next(ticks))
    report = w.write_transaction([oi(0)], ingested_at_ms=AT)
    assert (report.lock_wait_ns, report.duration_ns) == (30, 250)
    w.close()


def test_closed_writer_refuses(writer: OpenInterestWriter) -> None:
    writer.close()
    with pytest.raises(OpenInterestWriteError, match="closed"):
        writer.write_transaction([oi(0)], ingested_at_ms=AT)
