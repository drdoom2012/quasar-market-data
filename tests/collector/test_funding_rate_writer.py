import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from quasar_market_data.collector.funding_rate_writer import (
    MAX_ROWS_PER_TRANSACTION,
    FundingRateWriteBusy,
    FundingRateWriteError,
    FundingRateWriteOutcomeUnknown,
    FundingRateWriter,
)
from quasar_market_data.domain.candle import Candle, CandleKey
from quasar_market_data.domain.funding_rate import FundingRate, FundingRateKey, RateType
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import load_migrations, migrate
from quasar_market_data.storage.schema import SchemaMismatchError
from tests.collector.faulty_connection import Fault, FaultyConnection, open_faulty_writer
from tests.typing_helpers import untyped

AT = 1_793_840_000_000
T = 1_793_836_800_003
HOUR = 3_600_000
INSERT_ROW = "INSERT INTO funding_rates ("


def fr(
    index: int = 0,
    rate: str = "0.0001",
    mark: str | None = "67123.45",
    rate_type: RateType = RateType.REGULAR,
    time_ms: int | None = None,
) -> FundingRate:
    when = T + index * HOUR if time_ms is None else time_ms
    return FundingRate(FundingRateKey("binance-usdm", "BTC/USDT:USDT", when, rate_type), rate, mark)


def stored(db: Path) -> list[tuple[object, ...]]:
    conn = open_reader(db)
    try:
        return conn.execute(
            "SELECT funding_time_ms, rate_type, funding_rate, mark_price, ingested_at_ms FROM funding_rates"
            " ORDER BY funding_time_ms, rate_type"
        ).fetchall()
    finally:
        conn.close()


def conflict_rows(db: Path) -> list[tuple[object, ...]]:
    conn = open_reader(db)
    try:
        return conn.execute(
            "SELECT funding_time_ms, rate_type, funding_rate, mark_price, first_seen_at_ms"
            " FROM funding_rate_conflicts ORDER BY conflict_id"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "market.db"


@pytest.fixture
def writer(db: Path) -> Iterator[FundingRateWriter]:
    conn = open_writer(db)
    migrate(conn, applied_at_ms=AT)
    w = FundingRateWriter(conn)
    yield w
    if not w.unusable:
        w.close()


def faulty(db: Path) -> tuple[FaultyConnection, FundingRateWriter]:
    conn = open_faulty_writer(db, AT)
    w = FundingRateWriter(conn)
    conn.statements.clear()
    return conn, w


def test_rows_are_inserted_once(writer: FundingRateWriter, db: Path) -> None:
    rows = [fr(0), fr(1), fr(2)]
    report = writer.write_transaction(rows, ingested_at_ms=AT)
    assert (report.inserted, report.duplicates, report.conflicts) == (3, 0, ())
    again = writer.write_transaction(rows, ingested_at_ms=AT + 1)
    assert (again.inserted, again.duplicates, again.conflicts) == (0, 3, ())
    assert [row[4] for row in stored(db)] == [AT, AT, AT]


def test_negative_rate_and_null_mark_price_round_trip(writer: FundingRateWriter, db: Path) -> None:
    writer.write_transaction([fr(0, "-0.0001", None), fr(1, "0", "0")], ingested_at_ms=AT)
    assert stored(db) == [(T, "Regular", "-0.0001", None, AT), (T + HOUR, "Regular", "0", "0", AT)]


def test_differing_rate_is_recorded_not_written_over(writer: FundingRateWriter, db: Path) -> None:
    writer.write_transaction([fr(0, "0.0001")], ingested_at_ms=AT)
    report = writer.write_transaction([fr(0, "0.0002")], ingested_at_ms=AT + 5)
    assert report.inserted == 0 and report.duplicates == 0
    (conflict,) = report.conflicts
    assert conflict.incoming == fr(0, "0.0002")
    assert conflict.stored == fr(0, "0.0001")
    assert conflict.newly_recorded is True
    assert stored(db) == [(T, "Regular", "0.0001", "67123.45", AT)]
    assert conflict_rows(db) == [(T, "Regular", "0.0002", "67123.45", AT + 5)]


def test_differing_mark_price_is_a_conflict(writer: FundingRateWriter, db: Path) -> None:
    writer.write_transaction([fr(0, mark="67123.45")], ingested_at_ms=AT)
    report = writer.write_transaction([fr(0, mark="67123.46")], ingested_at_ms=AT + 5)
    assert [c.newly_recorded for c in report.conflicts] == [True]
    assert conflict_rows(db) == [(T, "Regular", "0.0001", "67123.46", AT + 5)]


@pytest.mark.parametrize(("first", "second"), [(None, "67123.45"), ("67123.45", None)])
def test_null_versus_price_is_a_conflict_in_both_directions(
    writer: FundingRateWriter, db: Path, first: str | None, second: str | None
) -> None:
    writer.write_transaction([fr(0, mark=first)], ingested_at_ms=AT)
    report = writer.write_transaction([fr(0, mark=second)], ingested_at_ms=AT + 5)
    (conflict,) = report.conflicts
    assert conflict.stored.mark_price == first and conflict.incoming.mark_price == second
    assert stored(db) == [(T, "Regular", "0.0001", first, AT)]
    assert conflict_rows(db) == [(T, "Regular", "0.0001", second, AT + 5)]


def test_same_differing_value_twice_is_one_conflict_row(writer: FundingRateWriter, db: Path) -> None:
    writer.write_transaction([fr(0, "0.0001")], ingested_at_ms=AT)
    writer.write_transaction([fr(0, "0.0002")], ingested_at_ms=AT + 5)
    report = writer.write_transaction([fr(0, "0.0002")], ingested_at_ms=AT + 9)
    assert [c.newly_recorded for c in report.conflicts] == [False]
    assert conflict_rows(db) == [(T, "Regular", "0.0002", "67123.45", AT + 5)]


def test_same_differing_null_price_twice_is_one_conflict_row(writer: FundingRateWriter, db: Path) -> None:
    writer.write_transaction([fr(0, mark="1")], ingested_at_ms=AT)
    writer.write_transaction([fr(0, mark=None)], ingested_at_ms=AT + 5)
    report = writer.write_transaction([fr(0, mark=None)], ingested_at_ms=AT + 9)
    assert [c.newly_recorded for c in report.conflicts] == [False]
    assert len(conflict_rows(db)) == 1


@pytest.mark.parametrize("order", [(RateType.REGULAR, RateType.SPECIAL), (RateType.SPECIAL, RateType.REGULAR)])
def test_regular_and_special_of_one_time_never_conflict(
    writer: FundingRateWriter, db: Path, order: tuple[RateType, RateType]
) -> None:
    rows = [fr(0, "0.0001", rate_type=order[0]), fr(0, "0.0009", "1", rate_type=order[1])]
    report = writer.write_transaction(rows, ingested_at_ms=AT)
    assert (report.inserted, report.conflicts) == (2, ())
    assert len(stored(db)) == 2 and conflict_rows(db) == []
    again = writer.write_transaction(rows, ingested_at_ms=AT + 1)
    assert (again.inserted, again.duplicates, again.conflicts) == (0, 2, ())


def test_inserted_keys_name_exactly_the_new_rows(writer: FundingRateWriter) -> None:
    writer.write_transaction([fr(0, "0.0001"), fr(1, "0.0001")], ingested_at_ms=AT)
    report = writer.write_transaction([fr(0, "0.0001"), fr(1, "0.0002"), fr(2, "0.0001")], ingested_at_ms=AT + 1)
    assert (report.inserted, report.duplicates, len(report.conflicts)) == (1, 1, 1)
    assert report.inserted_keys == (fr(2).key,)
    assert (report.span_before, report.span_after) == (None, None)


def test_more_than_100_rows_is_refused_before_any_sql(db: Path) -> None:
    conn, w = faulty(db)
    rows = [fr(i) for i in range(MAX_ROWS_PER_TRANSACTION + 1)]
    with pytest.raises(ValueError, match="100"):
        w.write_transaction(rows, ingested_at_ms=AT)
    assert conn.statements == []
    assert w.write_transaction(rows[:MAX_ROWS_PER_TRANSACTION], ingested_at_ms=AT).inserted == 100


def test_rows_must_be_funding_rates(writer: FundingRateWriter, db: Path) -> None:
    candle = Candle(CandleKey("binance-usdm", "BTC/USDT:USDT", Timeframe("5m"), T - 3), "1", "1", "1", "1", "1", None)
    for bad in (candle, object(), 0.0001):
        with pytest.raises(TypeError):
            writer.write_transaction([fr(0), untyped(bad)], ingested_at_ms=AT)
    assert stored(db) == []


def test_empty_rows_run_no_transaction(db: Path) -> None:
    conn, w = faulty(db)
    report = w.write_transaction([], ingested_at_ms=AT)
    assert (report.inserted, report.duplicates, report.conflicts) == (0, 0, ())
    assert conn.statements == []


def test_failure_mid_transaction_rolls_back(db: Path) -> None:
    conn, w = faulty(db)
    conn.faults.append(Fault(INSERT_ROW, sqlite3.OperationalError("injected"), nth=2))
    rows = [fr(0), fr(1), fr(2)]
    with pytest.raises(FundingRateWriteError, match="nothing was written") as error:
        w.write_transaction(rows, ingested_at_ms=AT)
    assert type(error.value) is FundingRateWriteError
    assert stored(db) == []
    assert w.write_transaction(rows, ingested_at_ms=AT).inserted == 3


def test_commit_that_took_effect_is_outcome_unknown(db: Path) -> None:
    conn, w = faulty(db)
    conn.faults.append(Fault("COMMIT", sqlite3.OperationalError("disk I/O error"), after_execution=True))
    rows = [fr(0), fr(1)]
    with pytest.raises(FundingRateWriteOutcomeUnknown):
        w.write_transaction(rows, ingested_at_ms=AT)
    retry = w.write_transaction(rows, ingested_at_ms=AT + 1)
    assert (retry.inserted, retry.duplicates, retry.conflicts) == (0, 2, ())


def test_busy_is_classified(db: Path) -> None:
    conn = open_writer(db, busy_timeout_ms=50)
    migrate(conn, applied_at_ms=AT)
    w = FundingRateWriter(conn)
    holder = open_writer(db)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(FundingRateWriteBusy):
            w.write_transaction([fr(0)], ingested_at_ms=AT)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert stored(db) == []
    assert conflict_rows(db) == []
    assert w.write_transaction([fr(0)], ingested_at_ms=AT).inserted == 1
    w.close()


def test_failed_rollback_closes_the_connection(db: Path) -> None:
    conn, w = faulty(db)
    conn.faults.append(Fault(INSERT_ROW, sqlite3.OperationalError("injected")))
    conn.faults.append(Fault("ROLLBACK", sqlite3.OperationalError("rollback failed")))
    with pytest.raises(FundingRateWriteError):
        w.write_transaction([fr(0)], ingested_at_ms=AT)
    assert w.unusable
    with pytest.raises(FundingRateWriteError, match="unusable"):
        w.write_transaction([fr(0)], ingested_at_ms=AT)
    other = open_writer(db, busy_timeout_ms=0)
    try:
        other.execute("BEGIN IMMEDIATE")  # the write lock was released by closing the connection
        other.execute("ROLLBACK")
    finally:
        other.close()


def test_constructor_refuses_v2_database_and_bad_connections(tmp_path: Path) -> None:
    v2 = open_writer(tmp_path / "v2.db")
    migrate(v2, applied_at_ms=AT, migrations=load_migrations()[:2])
    with pytest.raises(SchemaMismatchError):
        FundingRateWriter(v2)
    v2.close()

    path = tmp_path / "v3.db"
    good = open_writer(path)
    migrate(good, applied_at_ms=AT)
    good.close()

    reader = open_reader(path)
    with pytest.raises(FundingRateWriteError, match="read-only"):
        FundingRateWriter(reader)
    reader.close()

    no_triggers = open_writer(path)
    no_triggers.execute("PRAGMA recursive_triggers=OFF")
    with pytest.raises(FundingRateWriteError, match="recursive_triggers"):
        FundingRateWriter(no_triggers)
    no_triggers.close()

    implicit = sqlite3.connect(path)
    with pytest.raises(FundingRateWriteError, match="isolation_level"):
        FundingRateWriter(implicit)
    implicit.close()


def test_durations_are_measured(db: Path) -> None:
    conn = open_writer(db)
    migrate(conn, applied_at_ms=AT)
    ticks = iter([100, 130, 380])
    w = FundingRateWriter(conn, monotonic_ns=lambda: next(ticks))
    report = w.write_transaction([fr(0)], ingested_at_ms=AT)
    assert (report.lock_wait_ns, report.duration_ns) == (30, 250)
    w.close()


def test_closed_writer_refuses(writer: FundingRateWriter) -> None:
    writer.close()
    with pytest.raises(FundingRateWriteError, match="closed"):
        writer.write_transaction([fr(0)], ingested_at_ms=AT)


def busy_error(code: int) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize("code", [5, 261, 517, 773], ids=["busy", "busy_recovery", "busy_snapshot", "busy_timeout"])
def test_extended_busy_codes_are_busy(db: Path, code: int) -> None:
    conn, w = faulty(db)
    conn.faults.append(Fault("BEGIN IMMEDIATE", busy_error(code)))
    with pytest.raises(FundingRateWriteBusy):
        w.write_transaction([fr(0)], ingested_at_ms=AT)


def test_a_non_busy_code_is_not_busy(db: Path) -> None:
    conn, w = faulty(db)
    conn.faults.append(Fault("BEGIN IMMEDIATE", busy_error(1555)))
    with pytest.raises(FundingRateWriteError) as error:
        w.write_transaction([fr(0)], ingested_at_ms=AT)
    assert type(error.value) is FundingRateWriteError


def test_busy_commit_that_rolled_back_is_busy(db: Path) -> None:
    conn, w = faulty(db)
    conn.faults.append(Fault("COMMIT", busy_error(5)))
    with pytest.raises(FundingRateWriteBusy):
        w.write_transaction([fr(0)], ingested_at_ms=AT)
    assert stored(db) == []


def test_conflict_stored_by_an_unknown_commit_is_reported_as_not_new_on_retry(db: Path) -> None:
    conn, w = faulty(db)
    w.write_transaction([fr(0, "0.0001")], ingested_at_ms=AT)
    conn.faults.append(Fault("COMMIT", sqlite3.OperationalError("disk I/O error"), after_execution=True))
    with pytest.raises(FundingRateWriteOutcomeUnknown):
        w.write_transaction([fr(0, "0.0002")], ingested_at_ms=AT + 5)
    retry = w.write_transaction([fr(0, "0.0002")], ingested_at_ms=AT + 9)
    assert [c.newly_recorded for c in retry.conflicts] == [False]
    assert conflict_rows(db) == [(T, "Regular", "0.0002", "67123.45", AT + 5)]
