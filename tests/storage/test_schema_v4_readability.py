"""Schema version 4, row readability (ADR-0005 P-2): the cut-over, registration and confirmation, all insert-only."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import MigrationError, load_migrations, migrate
from quasar_market_data.storage.schema import SCHEMA_VERSION

AT = 1_792_677_000_000
VENUE = "binance-usdm"
SYMBOL = "BTC/USDT:USDT"

# (table, dataset name in write_transactions, insert statement taking the stamp last)
CANDLE = (VENUE, SYMBOL, "5m", 1_792_643_400_000, "1", "2", "1", "2", "3", None)
OI = (VENUE, SYMBOL, "5m", 1_792_643_700_000, "1", "2")
FR = (VENUE, SYMBOL, 1_792_656_000_000, "Regular", "0.0001", "82709.1")
DATA_TABLES = [
    pytest.param(
        "candles",
        "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        CANDLE,
        id="candles",
    ),
    pytest.param(
        "candles",
        "INSERT INTO candle_conflicts (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
        " turnover, first_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        CANDLE,
        id="candle_conflicts",
    ),
    pytest.param(
        "open_interest",
        "INSERT INTO open_interest VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        OI,
        id="open_interest",
    ),
    pytest.param(
        "open_interest",
        "INSERT INTO open_interest_conflicts (venue, symbol, period, time_ms, open_interest, open_interest_value,"
        " first_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        OI,
        id="open_interest_conflicts",
    ),
    pytest.param(
        "funding_rates",
        "INSERT INTO funding_rates VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        FR,
        id="funding_rates",
    ),
    pytest.param(
        "funding_rates",
        "INSERT INTO funding_rate_conflicts (venue, symbol, funding_time_ms, rate_type, funding_rate, mark_price,"
        " first_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        FR,
        id="funding_rate_conflicts",
    ),
]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_writer(tmp_path / "m.db")
    migrate(connection, applied_at_ms=AT)
    yield connection
    connection.close()


def cutover(conn: sqlite3.Connection) -> int:
    value: int = conn.execute("SELECT cutover_ms FROM readability_cutover WHERE id = 1").fetchone()[0]
    return value


def register(conn: sqlite3.Connection, table: str, stamp: int) -> None:
    conn.execute("INSERT INTO write_transactions VALUES (?, ?)", (table, stamp))


def test_the_schema_version_is_4() -> None:
    assert SCHEMA_VERSION == 4
    assert [m.name for m in load_migrations()][-1] == "readability"


def test_a_fresh_database_has_its_cut_over_just_after_the_migration_instant(conn: sqlite3.Connection) -> None:
    assert cutover(conn) == AT + 1


@pytest.mark.parametrize(("table", "insert", "row"), DATA_TABLES)
def test_an_unregistered_row_is_refused(
    conn: sqlite3.Connection, table: str, insert: str, row: tuple[object, ...]
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="registered write transaction"):
        conn.execute(insert, (*row, AT + 10))


@pytest.mark.parametrize(("table", "insert", "row"), DATA_TABLES)
def test_a_registered_row_is_stored_and_an_identical_one_under_a_new_stamp_is_a_no_op(
    conn: sqlite3.Connection, table: str, insert: str, row: tuple[object, ...]
) -> None:
    register(conn, table, AT + 10)
    assert conn.execute(insert, (*row, AT + 10)).rowcount == 1
    register(conn, table, AT + 11)
    assert conn.execute(insert, (*row, AT + 11)).rowcount == 0


@pytest.mark.parametrize(("table", "insert", "row"), DATA_TABLES)
def test_an_identical_duplicate_under_an_unregistered_stamp_is_still_refused(
    conn: sqlite3.Connection, table: str, insert: str, row: tuple[object, ...]
) -> None:
    register(conn, table, AT + 10)
    conn.execute(insert, (*row, AT + 10))
    with pytest.raises(sqlite3.IntegrityError, match="registered write transaction"):
        conn.execute(insert, (*row, AT + 12))


def test_a_registration_of_another_dataset_does_not_count(conn: sqlite3.Connection) -> None:
    register(conn, "open_interest", AT + 10)
    with pytest.raises(sqlite3.IntegrityError, match="registered write transaction"):
        conn.execute("INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*CANDLE, AT + 10))


@pytest.mark.parametrize("stamp", [AT, AT + 1])
def test_a_registration_at_or_before_the_cut_over_is_refused(conn: sqlite3.Connection, stamp: int) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="cut-over"):
        register(conn, "candles", stamp)


def test_a_stamp_registers_once_per_table(conn: sqlite3.Connection) -> None:
    register(conn, "candles", AT + 10)
    register(conn, "open_interest", AT + 10)
    with pytest.raises(sqlite3.IntegrityError):
        register(conn, "candles", AT + 10)


def test_an_unknown_table_name_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        register(conn, "candle_conflicts", AT + 10)


def test_a_confirmation_needs_a_registered_transaction(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="no registered write transaction"):
        conn.execute("INSERT INTO write_confirmations VALUES ('candles', ?, ?)", (AT + 10, AT + 11))
    register(conn, "candles", AT + 10)
    conn.execute("INSERT INTO write_confirmations VALUES ('candles', ?, ?)", (AT + 10, AT + 11))
    assert (
        conn.execute(
            "INSERT INTO write_confirmations VALUES ('candles', ?, ?) ON CONFLICT DO NOTHING", (AT + 10, AT + 12)
        ).rowcount
        == 0
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE readability_cutover SET cutover_ms = cutover_ms + 1",
        "DELETE FROM readability_cutover",
        "UPDATE write_transactions SET stamp_ms = stamp_ms + 1",
        "DELETE FROM write_transactions",
        "UPDATE write_confirmations SET confirmed_at_ms = confirmed_at_ms + 1",
        "DELETE FROM write_confirmations",
    ],
)
def test_the_readability_tables_are_immutable(conn: sqlite3.Connection, statement: str) -> None:
    register(conn, "candles", AT + 10)
    conn.execute("INSERT INTO write_confirmations VALUES ('candles', ?, ?)", (AT + 10, AT + 11))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(statement)


def test_a_second_cut_over_row_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO readability_cutover VALUES (1, ?)", (AT + 5,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO readability_cutover VALUES (2, ?)", (AT + 5,))


def seed_v3(path: Path, *, stamps: dict[str, int]) -> sqlite3.Connection:
    """A database at schema 3 with one row per data table, stamped as given (old-style: nothing registered)."""
    connection = open_writer(path)
    migrate(connection, applied_at_ms=AT, migrations=load_migrations()[:3])
    connection.execute("INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*CANDLE, stamps["candles"]))
    connection.execute(
        "INSERT INTO candle_conflicts (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
        " turnover, first_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*CANDLE[:4], "9", "9", "9", "9", "9", None, stamps["candle_conflicts"]),
    )
    connection.execute("INSERT INTO open_interest VALUES (?, ?, ?, ?, ?, ?, ?)", (*OI, stamps["open_interest"]))
    connection.execute(
        "INSERT INTO open_interest_conflicts (venue, symbol, period, time_ms, open_interest, open_interest_value,"
        " first_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (*OI[:4], "9", "9", stamps["open_interest_conflicts"]),
    )
    connection.execute("INSERT INTO funding_rates VALUES (?, ?, ?, ?, ?, ?, ?)", (*FR, stamps["funding_rates"]))
    connection.execute(
        "INSERT INTO funding_rate_conflicts (venue, symbol, funding_time_ms, rate_type, funding_rate, mark_price,"
        " first_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (*FR[:4], "9", None, stamps["funding_rate_conflicts"]),
    )
    return connection


NAMES = [
    "candles",
    "candle_conflicts",
    "open_interest",
    "open_interest_conflicts",
    "funding_rates",
    "funding_rate_conflicts",
]


@pytest.mark.parametrize("newest", NAMES)
def test_the_cut_over_lies_above_every_stored_stamp_even_with_a_clock_behind_the_data(
    tmp_path: Path, newest: str
) -> None:
    stamps = {name: AT - 1_000 for name in NAMES} | {newest: AT + 50_000}  # the host clock stepped back
    connection = seed_v3(tmp_path / "m.db", stamps=stamps)
    migrate(connection, applied_at_ms=AT)
    assert cutover(connection) == AT + 50_001


def test_old_rows_are_kept_and_the_cut_over_is_the_clock_when_it_is_ahead(tmp_path: Path) -> None:
    connection = seed_v3(tmp_path / "m.db", stamps={name: AT - 1_000 for name in NAMES})
    migrate(connection, applied_at_ms=AT)
    assert cutover(connection) == AT + 1
    assert connection.execute("SELECT count(*) FROM candles").fetchone()[0] == 1


def test_a_clock_callable_is_read_after_the_write_lock_is_held(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    connection = open_writer(path)
    migrate(connection, applied_at_ms=AT, migrations=load_migrations()[:3])
    probe = open_writer(path, busy_timeout_ms=0)
    seen: list[bool] = []

    def clock() -> int:
        try:  # the migration holds the write lock, so another writer cannot take it now
            probe.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            seen.append(True)
        else:
            probe.execute("ROLLBACK")
            seen.append(False)
        return AT + 7

    migrate(connection, applied_at_ms=clock)
    assert seen == [True]
    assert cutover(connection) == AT + 8
    assert connection.execute("SELECT applied_at_ms FROM schema_migrations WHERE version = 4").fetchone()[0] == AT + 7


def test_migrating_again_changes_nothing(conn: sqlite3.Connection) -> None:
    before = cutover(conn)
    assert migrate(conn, applied_at_ms=AT + 99) == 4
    assert cutover(conn) == before


def test_a_failed_migration_4_leaves_version_3_and_runs_again(tmp_path: Path) -> None:
    connection = seed_v3(tmp_path / "m.db", stamps={name: AT - 1_000 for name in NAMES})

    def failing() -> int:
        raise RuntimeError("clock failed")

    with pytest.raises((MigrationError, RuntimeError)):
        migrate(connection, applied_at_ms=failing)
    assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 3
    assert (
        connection.execute("SELECT count(*) FROM sqlite_master WHERE name = 'readability_cutover'").fetchone()[0] == 0
    )
    assert migrate(connection, applied_at_ms=AT) == 4
    assert cutover(connection) == AT + 1
