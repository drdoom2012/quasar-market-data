import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from quasar_market_data.collector.candle_writer import CandleConflict, CandleWriteError, CandleWriter, WriteReport
from quasar_market_data.domain.candle import Candle
from quasar_market_data.domain.validation import ValidatedCandle, require_valid
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from quasar_market_data.storage.schema import SchemaMismatchError
from tests.collector.faulty_connection import Fault, open_faulty_writer
from tests.domain.test_candle import OPEN_MS, make_candle, make_key
from tests.typing_helpers import untyped

AT = 1_792_677_000_000
SELECT_ROWS = "SELECT open_time_ms, open, high, low, close, volume, turnover, ingested_at_ms FROM candles ORDER BY 1"


def valid(n: int = 0, **fields: str | None) -> ValidatedCandle:
    """A valid 5m candle ``n`` intervals after ``OPEN_MS``; ``fields`` override the default values."""
    return require_valid(make_candle(key=make_key(open_time_ms=OPEN_MS + n * 300_000), **untyped(fields)))


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "market.db"


@pytest.fixture
def writer_conn(db: Path) -> Iterator[sqlite3.Connection]:
    conn = open_writer(db)
    migrate(conn, applied_at_ms=AT)
    yield conn
    conn.close()


@pytest.fixture
def writer(writer_conn: sqlite3.Connection) -> CandleWriter:
    return CandleWriter(writer_conn)


def stored_rows(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return conn.execute(SELECT_ROWS).fetchall()


def test_new_candles_are_inserted_with_exact_text(writer: CandleWriter, writer_conn: sqlite3.Connection) -> None:
    report = writer.write([valid(0), valid(1, turnover=None)], ingested_at_ms=AT)
    assert report == WriteReport(inserted=2, duplicates=0, conflicts=())
    assert stored_rows(writer_conn) == [
        (OPEN_MS, "100.5", "101", "99", "100", "12.5", "1250", AT),
        (OPEN_MS + 300_000, "100.5", "101", "99", "100", "12.5", None, AT),
    ]


def test_rewriting_the_same_batch_is_a_no_op(writer: CandleWriter, writer_conn: sqlite3.Connection) -> None:
    batch = [valid(0), valid(1)]
    writer.write(batch, ingested_at_ms=AT)
    before = stored_rows(writer_conn)
    assert writer.write(batch, ingested_at_ms=AT + 1) == WriteReport(inserted=0, duplicates=2, conflicts=())
    assert stored_rows(writer_conn) == before


def test_restart_does_not_duplicate(db: Path) -> None:
    batch = [valid(0), valid(1)]
    first = open_writer(db)
    migrate(first, applied_at_ms=AT)
    CandleWriter(first).write(batch, ingested_at_ms=AT)
    first.close()

    second = open_writer(db)
    try:
        assert CandleWriter(second).write(batch, ingested_at_ms=AT + 1) == WriteReport(0, 2, ())
        assert second.execute("SELECT count(*) FROM candles").fetchone() == (2,)
    finally:
        second.close()


DIFFERENCES = {
    "open": ({}, {"open": "100.2"}),
    "high": ({}, {"high": "102"}),
    "low": ({}, {"low": "98"}),
    "close": ({}, {"close": "100.1"}),
    "volume": ({}, {"volume": "13"}),
    "turnover_changed": ({}, {"turnover": "1251"}),
    "turnover_to_none": ({}, {"turnover": None}),
    "turnover_from_none": ({"turnover": None}, {"turnover": "1250"}),
}


@pytest.mark.parametrize(("stored_fields", "incoming_fields"), DIFFERENCES.values(), ids=DIFFERENCES.keys())
def test_differing_duplicate_is_reported_and_never_overwritten(
    writer: CandleWriter,
    writer_conn: sqlite3.Connection,
    stored_fields: dict[str, str | None],
    incoming_fields: dict[str, str | None],
) -> None:
    stored, incoming = valid(0, **stored_fields), valid(0, **incoming_fields)
    writer.write([stored], ingested_at_ms=AT)
    before = stored_rows(writer_conn)

    report = writer.write([incoming], ingested_at_ms=AT + 1)

    assert report == WriteReport(
        inserted=0, duplicates=0, conflicts=(CandleConflict(incoming=incoming, stored=stored),)
    )
    assert stored_rows(writer_conn) == before


def test_conflict_does_not_stop_the_rest_of_the_batch(writer: CandleWriter, writer_conn: sqlite3.Connection) -> None:
    writer.write([valid(1)], ingested_at_ms=AT)
    conflicting = valid(1, close="100.1")
    report = writer.write([valid(0), conflicting, valid(2)], ingested_at_ms=AT)
    assert (report.inserted, report.duplicates, len(report.conflicts)) == (2, 0, 1)
    assert [row[0] for row in stored_rows(writer_conn)] == [OPEN_MS, OPEN_MS + 300_000, OPEN_MS + 600_000]


def test_duplicate_inside_one_batch(writer: CandleWriter) -> None:
    candle, changed = valid(0), valid(0, close="100.1")
    assert writer.write([candle, candle], ingested_at_ms=AT) == WriteReport(1, 1, ())
    report = writer.write([valid(1), valid(1, close="100.1")], ingested_at_ms=AT)
    assert report.inserted == 1
    assert report.conflicts == (CandleConflict(incoming=valid(1, close="100.1"), stored=valid(1)),)
    assert changed != candle


def test_conflicts_record_nothing_yet(writer: CandleWriter, writer_conn: sqlite3.Connection) -> None:
    # Recording a conflict in candle_conflicts is S8; S7 only reports it.
    writer.write([valid(0)], ingested_at_ms=AT)
    writer.write([valid(0, close="100.1")], ingested_at_ms=AT)
    assert writer_conn.execute("SELECT count(*) FROM candle_conflicts").fetchone() == (0,)


def test_writer_does_not_check_eligibility(writer: CandleWriter, writer_conn: sqlite3.Connection) -> None:
    # Documents a NON-guarantee: ValidatedCandle proves validity, not that the candle is closed. A candle in
    # the far future is persisted. Selecting only closed candles is S9's job (see the S7 plan, section
    # "What the S7 writer does not guarantee").
    far_future = require_valid(make_candle(key=make_key(open_time_ms=4_000_000_200_000 - 4_000_000_200_000 % 300_000)))
    assert writer.write([far_future], ingested_at_ms=AT) == WriteReport(1, 0, ())
    assert writer_conn.execute("SELECT count(*) FROM candles").fetchone() == (1,)


def test_empty_batch_is_a_no_op(writer: CandleWriter, writer_conn: sqlite3.Connection) -> None:
    assert writer.write([], ingested_at_ms=AT) == WriteReport(0, 0, ())
    assert not writer_conn.in_transaction


def _reader(db: Path) -> sqlite3.Connection:
    return open_reader(db)


def _recursive_triggers_off(db: Path) -> sqlite3.Connection:
    conn = open_writer(db)
    conn.execute("PRAGMA recursive_triggers=OFF")
    return conn


def _not_autocommit(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(db)


def _already_in_transaction(db: Path) -> sqlite3.Connection:
    conn = open_writer(db)
    conn.execute("BEGIN")
    return conn


MISCONFIGURED: dict[str, Callable[[Path], sqlite3.Connection]] = {
    "reader": _reader,
    "recursive_triggers_off": _recursive_triggers_off,
    "not_autocommit": _not_autocommit,
    "already_in_transaction": _already_in_transaction,
}


@pytest.mark.parametrize("make_connection", MISCONFIGURED.values(), ids=MISCONFIGURED.keys())
def test_writer_refuses_a_misconfigured_connection(
    db: Path, writer_conn: sqlite3.Connection, make_connection: Callable[[Path], sqlite3.Connection]
) -> None:
    conn = make_connection(db)  # the database is already migrated by writer_conn
    try:
        with pytest.raises(CandleWriteError):
            CandleWriter(conn)
        if make_connection is _already_in_transaction:
            assert conn.in_transaction  # refused without touching the caller's transaction
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def test_writer_refuses_an_unmigrated_database(db: Path) -> None:
    conn = open_writer(db)
    try:
        with pytest.raises(SchemaMismatchError):
            CandleWriter(conn)
    finally:
        conn.close()


def test_argument_validation(writer: CandleWriter, writer_conn: sqlite3.Connection) -> None:
    for bad in (True, 1.5):
        with pytest.raises(TypeError):
            writer.write([valid(0)], ingested_at_ms=untyped(bad))
    with pytest.raises(ValueError):
        writer.write([valid(0)], ingested_at_ms=0)
    with pytest.raises(TypeError):
        writer.write([valid(0), untyped("x")], ingested_at_ms=AT)
    assert stored_rows(writer_conn) == []


def test_write_after_close_raises(db: Path) -> None:
    conn = open_writer(db)
    migrate(conn, applied_at_ms=AT)
    writer = CandleWriter(conn)
    writer.close()
    with pytest.raises(CandleWriteError):
        writer.write([valid(0)], ingested_at_ms=AT)


def _typing_contract(writer: CandleWriter, plain: Candle) -> None:
    """Never called. mypy must reject a plain ``Candle``; an unused ignore fails ``make typecheck``."""
    writer.write([plain], ingested_at_ms=AT)  # type: ignore[list-item]


# --- Task 2: failure semantics -------------------------------------------------------------------------------


def test_candle_cast_to_validated_but_invalid_is_rejected_whole_batch(
    writer: CandleWriter, writer_conn: sqlite3.Connection
) -> None:
    bad = cast(ValidatedCandle, make_candle(key=make_key(open_time_ms=OPEN_MS + 300_000), high="98", low="99"))
    with pytest.raises(CandleWriteError, match=r"candles\[1\]"):
        writer.write([valid(0), bad], ingested_at_ms=AT)
    assert stored_rows(writer_conn) == []
    assert not writer_conn.in_transaction


def test_failure_midway_rolls_back_the_whole_batch(writer: CandleWriter, writer_conn: sqlite3.Connection) -> None:
    writer_conn.execute(
        "CREATE TEMP TRIGGER boom BEFORE INSERT ON main.candles "
        f"WHEN NEW.open_time_ms = {OPEN_MS + 300_000} BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    batch = [valid(0), valid(1), valid(2)]
    with pytest.raises(CandleWriteError) as caught:
        writer.write(batch, ingested_at_ms=AT)
    assert isinstance(caught.value.__cause__, sqlite3.Error)
    assert stored_rows(writer_conn) == []
    assert not writer_conn.in_transaction

    writer_conn.execute("DROP TRIGGER boom")
    assert writer.write(batch, ingested_at_ms=AT) == WriteReport(3, 0, ())


def test_commit_failure_rolls_back_and_the_batch_can_be_retried(db: Path) -> None:
    conn = open_faulty_writer(db, AT)
    try:
        writer = CandleWriter(conn)
        conn.faults.append(Fault("COMMIT", sqlite3.OperationalError("injected COMMIT failure")))
        batch = [valid(0), valid(1)]
        with pytest.raises(CandleWriteError) as caught:
            writer.write(batch, ingested_at_ms=AT)
        assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        assert stored_rows(conn) == []
        assert not conn.in_transaction

        assert writer.write(batch, ingested_at_ms=AT) == WriteReport(2, 0, ())
    finally:
        conn.close()


def test_failed_rollback_makes_the_writer_unusable(db: Path) -> None:
    conn = open_faulty_writer(db, AT)
    try:
        writer = CandleWriter(conn)
        conn.faults.append(Fault("COMMIT", sqlite3.OperationalError("injected COMMIT failure")))
        conn.faults.append(Fault("ROLLBACK", sqlite3.OperationalError("injected ROLLBACK failure")))
        with pytest.raises(CandleWriteError):
            writer.write([valid(0)], ingested_at_ms=AT)

        statements_before = len(conn.statements)
        with pytest.raises(CandleWriteError, match="unusable"):
            writer.write([valid(1)], ingested_at_ms=AT)
        assert len(conn.statements) == statements_before  # the database was not touched again

        other = sqlite3.connect(db)
        try:
            assert other.execute("SELECT count(*) FROM candles").fetchone() == (0,)
        finally:
            other.close()
        writer.close()  # closing still works
    finally:
        conn.close()


def test_interrupt_midway_rolls_back_and_propagates_unchanged(db: Path) -> None:
    conn = open_faulty_writer(db, AT)
    try:
        writer = CandleWriter(conn)
        interrupt = KeyboardInterrupt()
        conn.faults.append(Fault("INSERT INTO candles", interrupt, nth=2))
        with pytest.raises(KeyboardInterrupt) as caught:
            writer.write([valid(0), valid(1), valid(2)], ingested_at_ms=AT)
        assert caught.value is interrupt
        assert stored_rows(conn) == []
        assert not conn.in_transaction

        assert writer.write([valid(0)], ingested_at_ms=AT) == WriteReport(1, 0, ())
    finally:
        conn.close()


def test_busy_database_raises_and_writes_nothing(db: Path, writer_conn: sqlite3.Connection) -> None:
    busy = open_writer(db, busy_timeout_ms=0)
    blocker = sqlite3.connect(db, isolation_level=None)
    try:
        writer = CandleWriter(busy)
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(CandleWriteError) as caught:
            writer.write([valid(0), valid(1)], ingested_at_ms=AT)
        assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        assert not busy.in_transaction
        assert stored_rows(writer_conn) == []

        blocker.execute("ROLLBACK")
        assert writer.write([valid(0), valid(1)], ingested_at_ms=AT) == WriteReport(2, 0, ())
    finally:
        blocker.close()
        busy.close()


def test_writer_is_usable_after_a_failed_batch(writer: CandleWriter, writer_conn: sqlite3.Connection) -> None:
    bad = cast(ValidatedCandle, make_candle(high="98", low="99"))
    with pytest.raises(CandleWriteError):
        writer.write([bad], ingested_at_ms=AT)
    assert writer.write([valid(0)], ingested_at_ms=AT) == WriteReport(1, 0, ())
