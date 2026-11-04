import hashlib
import sqlite3
from pathlib import Path

import pytest

from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import (
    Migration,
    MigrationError,
    applied_version,
    load_migrations,
    migrate,
    refuse_foreign_file,
)
from quasar_market_data.storage.schema import APPLICATION_ID, SCHEMA_VERSION
from tests.storage.conftest import schema_sql
from tests.typing_helpers import untyped

AT = 1_792_677_000_000
M1 = load_migrations()[0]


def migration(version: int, name: str, sql: str) -> Migration:
    return Migration.from_bytes(version, name, sql.encode())


# A first migration must create the bookkeeping table, as the shipped one does.
BOOKKEEPING = (
    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
    "checksum_sha256 TEXT NOT NULL, applied_at_ms INTEGER NOT NULL) STRICT;\n"
)
M2 = migration(2, "extra", "CREATE TABLE extra (a INTEGER) STRICT;")


def inventory(conn: sqlite3.Connection) -> set[tuple[str, str, str | None]]:
    return set(conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


def versions(conn: sqlite3.Connection) -> list[int]:
    return [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_writer(tmp_path / "m.db")


def test_fresh_database_is_migrated_to_the_latest_version(conn: sqlite3.Connection) -> None:
    assert migrate(conn, applied_at_ms=AT) == 3
    assert conn.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
    assert conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall() == [
        (1, "schema_v1", M1.checksum, AT),
        (2, "open_interest", load_migrations()[1].checksum, AT),
        (3, "funding_rate", load_migrations()[2].checksum, AT),
    ]
    tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"candles", "candle_conflicts", "unavailable_candles", "schema_migrations"} <= tables
    assert {"open_interest", "open_interest_conflicts"} <= tables
    assert {"funding_rates", "funding_rate_conflicts", "funding_rate_verified"} <= tables


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    migrate(conn, applied_at_ms=AT)
    before = inventory(conn)
    assert migrate(conn, applied_at_ms=AT + 1) == 3
    assert conn.execute("SELECT version, applied_at_ms FROM schema_migrations ORDER BY version").fetchall() == [
        (1, AT),
        (2, AT),
        (3, AT),
    ]
    assert inventory(conn) == before


def test_pending_migration_is_applied_on_top(conn: sqlite3.Connection) -> None:
    migrate(conn, applied_at_ms=AT, migrations=(M1,))
    assert migrate(conn, applied_at_ms=AT + 5, migrations=(M1, M2)) == 2
    assert conn.execute("SELECT version, applied_at_ms FROM schema_migrations ORDER BY version").fetchall() == [
        (1, AT),
        (2, AT + 5),
    ]
    assert conn.execute("SELECT count(*) FROM extra").fetchone() == (0,)


def test_failing_migration_is_atomic(conn: sqlite3.Connection) -> None:
    broken = migration(2, "broken", "CREATE TABLE half (a INTEGER) STRICT;\nCREATE TABLE oops (;\n")
    migrate(conn, applied_at_ms=AT, migrations=(M1,))
    before = inventory(conn)
    with pytest.raises(MigrationError):
        migrate(conn, applied_at_ms=AT, migrations=(M1, broken))
    assert inventory(conn) == before
    assert versions(conn) == [1]
    assert not conn.in_transaction


def test_failing_first_migration_leaves_a_fresh_database(conn: sqlite3.Connection) -> None:
    broken = migration(1, "broken", "CREATE TABLE half (a INTEGER) STRICT;\nCREATE TABLE oops (;\n")
    with pytest.raises(MigrationError):
        migrate(conn, applied_at_ms=AT, migrations=(broken,))
    assert conn.execute("PRAGMA application_id").fetchone() == (0,)
    assert inventory(conn) == set()


def test_checksum_mismatch_is_refused(conn: sqlite3.Connection) -> None:
    migrate(conn, applied_at_ms=AT)
    conn.execute("UPDATE schema_migrations SET checksum_sha256 = 'tampered'")
    with pytest.raises(MigrationError, match="checksum"):
        migrate(conn, applied_at_ms=AT)
    assert conn.execute("SELECT checksum_sha256 FROM schema_migrations").fetchall() == [("tampered",)] * SCHEMA_VERSION


def test_database_newer_than_the_code_is_refused(conn: sqlite3.Connection) -> None:
    migrate(conn, applied_at_ms=AT, migrations=(M1, M2))
    with pytest.raises(MigrationError, match="newer"):
        migrate(conn, applied_at_ms=AT, migrations=(M1,))
    assert versions(conn) == [1, 2]


def test_gap_in_applied_versions_is_refused(conn: sqlite3.Connection) -> None:
    m3 = migration(3, "third", "CREATE TABLE third (a INTEGER) STRICT;")
    migrate(conn, applied_at_ms=AT, migrations=(M1,))
    conn.execute("INSERT INTO schema_migrations VALUES (3, 'third', ?, ?)", (m3.checksum, AT))
    with pytest.raises(MigrationError, match="gap"):
        migrate(conn, applied_at_ms=AT, migrations=(M1, M2, m3))
    assert versions(conn) == [1, 3]


def test_foreign_application_id_is_refused_untouched(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA application_id = 12345")
    with pytest.raises(MigrationError, match="not a quasar-market-data database"):
        migrate(conn, applied_at_ms=AT)
    assert conn.execute("PRAGMA application_id").fetchone() == (12345,)
    assert inventory(conn) == set()


def test_database_with_unknown_tables_is_refused_untouched(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE t (a INTEGER)")
    before = inventory(conn)
    with pytest.raises(MigrationError, match="not a quasar-market-data database"):
        migrate(conn, applied_at_ms=AT)
    assert inventory(conn) == before
    assert conn.execute("PRAGMA application_id").fetchone() == (0,)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def foreign_file(path: Path, journal_mode: str, kind: str) -> None:
    """A SQLite file that is not ours, written by a plain connection (not ``open_writer``)."""
    plain = sqlite3.connect(path)
    plain.execute(f"PRAGMA journal_mode={journal_mode}")
    if kind == "foreign_application_id":
        plain.execute("PRAGMA application_id = 12345")
    else:  # unknown_tables
        plain.execute("CREATE TABLE t (a INTEGER)")
        plain.execute("INSERT INTO t VALUES (1)")
    plain.commit()
    plain.close()


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
@pytest.mark.parametrize("kind", ["foreign_application_id", "unknown_tables"])
def test_refuse_foreign_file_leaves_the_file_byte_for_byte_unchanged(
    tmp_path: Path, journal_mode: str, kind: str
) -> None:
    path = tmp_path / "foreign.db"
    foreign_file(path, journal_mode, kind)
    before = sha(path)
    with pytest.raises(MigrationError, match="not a quasar-market-data database"):
        refuse_foreign_file(path)
    assert sha(path) == before
    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == journal_mode
    finally:
        check.close()


def test_refuse_foreign_file_accepts_missing_empty_fresh_and_ours(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    refuse_foreign_file(missing)
    assert not missing.exists()  # the check creates nothing
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    refuse_foreign_file(empty)
    ours = tmp_path / "ours.db"
    conn = open_writer(ours)
    try:
        migrate(conn, applied_at_ms=AT)
    finally:
        conn.close()
    refuse_foreign_file(ours)


def test_refuse_foreign_file_lets_a_non_sqlite_file_fail_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "junk.db"
    path.write_bytes(b"not a database" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        refuse_foreign_file(path)
    assert path.read_bytes() == b"not a database" * 100


def test_own_application_id_without_a_migration_table_is_refused(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    with pytest.raises(MigrationError):
        migrate(conn, applied_at_ms=AT)


def test_non_autocommit_connection_is_refused(tmp_path: Path) -> None:
    plain = sqlite3.connect(tmp_path / "plain.db")
    try:
        with pytest.raises(MigrationError, match="isolation_level"):
            migrate(plain, applied_at_ms=AT)
    finally:
        plain.close()


def test_applied_at_ms_is_validated(conn: sqlite3.Connection) -> None:
    for bad in (True, 1.5):
        with pytest.raises(TypeError):
            migrate(conn, applied_at_ms=untyped(bad))
    with pytest.raises(ValueError):
        migrate(conn, applied_at_ms=0)
    assert inventory(conn) == set()


def test_trigger_bodies_are_not_split(conn: sqlite3.Connection) -> None:
    sql = BOOKKEEPING + (
        "CREATE TABLE a (x INTEGER) STRICT;\n"
        "-- a comment between statements\n"
        "CREATE TRIGGER a_no_delete BEFORE DELETE ON a\n"
        "  BEGIN SELECT RAISE(ABORT, 'no'); SELECT 1; END;\n"
    )
    migrate(conn, applied_at_ms=AT, migrations=(migration(1, "a", sql),))
    assert ("trigger", "a_no_delete") in {(t, n) for t, n, _ in inventory(conn)}


def test_trailing_comment_is_ignored(conn: sqlite3.Connection) -> None:
    sql = BOOKKEEPING + "CREATE TABLE a (x INTEGER) STRICT;\n-- trailing comment\n"
    assert migrate(conn, applied_at_ms=AT, migrations=(migration(1, "a", sql),)) == 1


def test_unterminated_statement_is_refused(conn: sqlite3.Connection) -> None:
    sql = "CREATE TABLE a (x INTEGER) STRICT;\nCREATE TABLE b (x INTEGER) STRICT"
    with pytest.raises(MigrationError):
        migrate(conn, applied_at_ms=AT, migrations=(migration(1, "a", sql),))
    assert inventory(conn) == set()


def test_injected_migrations_must_run_from_one_without_gaps(conn: sqlite3.Connection) -> None:
    with pytest.raises(MigrationError):
        migrate(conn, applied_at_ms=AT, migrations=(M2,))
    assert inventory(conn) == set()


def test_shipped_migrations_are_consistent() -> None:
    shipped = load_migrations()
    assert [m.version for m in shipped] == list(range(1, len(shipped) + 1))
    assert SCHEMA_VERSION == len(shipped)


@pytest.mark.parametrize(
    "files",
    [
        ["1_x.sql"],
        ["001_X.sql"],
        ["001_a.sql", "001_b.sql"],
        ["002_a.sql"],
        ["001_a.sql", "003_c.sql"],
    ],
    ids=["short_number", "uppercase", "duplicate_version", "missing_first", "gap"],
)
def test_load_migrations_rejects_bad_names_and_gaps(tmp_path: Path, files: list[str]) -> None:
    for name in files:
        (tmp_path / name).write_text("SELECT 1;\n")
    with pytest.raises(MigrationError):
        load_migrations(tmp_path)


def test_load_migrations_ignores_non_sql_files_and_orders_by_version(tmp_path: Path) -> None:
    (tmp_path / "002_b.sql").write_text("SELECT 2;\n")
    (tmp_path / "001_a.sql").write_text("SELECT 1;\n")
    (tmp_path / "__init__.py").write_text("")
    assert [(m.version, m.name) for m in load_migrations(tmp_path)] == [(1, "a"), (2, "b")]


def test_checksum_is_sha256_of_the_file_bytes() -> None:
    raw = b"SELECT 1;\n"
    migration_ = Migration.from_bytes(1, "a", raw)
    assert migration_.checksum == hashlib.sha256(raw).hexdigest()
    assert migration_.sql == "SELECT 1;\n"


def test_migrated_database_matches_the_shipped_schema_fixture(
    conn: sqlite3.Connection, schema_v3_conn: sqlite3.Connection
) -> None:
    migrate(conn, applied_at_ms=AT)
    assert schema_sql(3)  # the fixture applied the same SQL directly
    assert inventory(conn) == inventory(schema_v3_conn)


V1_TABLES = {
    "candles": "venue, symbol, timeframe, open_time_ms",
    "candle_conflicts": "conflict_id",
    "unavailable_candles": "venue, symbol, timeframe, open_time_ms",
}


def table_digest(conn: sqlite3.Connection, table: str, orders: dict[str, str] = V1_TABLES) -> str:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY {orders[table]}").fetchall()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def test_upgrade_from_v1_keeps_every_v1_row(conn: sqlite3.Connection) -> None:
    migrate(conn, applied_at_ms=AT, migrations=(M1,))
    for offset in range(3):
        conn.execute(
            "INSERT INTO candles VALUES ('binance-usdm', 'BTC/USDT:USDT', '5m', ?, '1', '2', '1', '2', '3', NULL, 1)",
            (1_792_676_700_000 + offset * 300_000,),
        )
    conn.execute(
        "INSERT INTO candle_conflicts (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
        " turnover, first_seen_at_ms) VALUES ('binance-usdm', 'BTC/USDT:USDT', '5m', 1792676700000,"
        " '1', '2', '1', '9', '3', NULL, 2)"
    )
    conn.execute(
        "INSERT INTO unavailable_candles VALUES ('binance-usdm', 'BTC/USDT:USDT', '5m', 1792677600000,"
        " 'EXCHANGE_NO_DATA', 3)"
    )
    before = {table: table_digest(conn, table) for table in V1_TABLES}
    assert conn.execute("SELECT count(*) FROM candles").fetchone() == (3,)

    assert migrate(conn, applied_at_ms=AT + 1) == 3
    assert versions(conn) == [1, 2, 3]
    assert {table: table_digest(conn, table) for table in V1_TABLES} == before

    after = inventory(conn)
    assert migrate(conn, applied_at_ms=AT + 2) == 3
    assert inventory(conn) == after
    assert versions(conn) == [1, 2, 3]


V1_AND_V2_TABLES = {
    **V1_TABLES,
    "open_interest": "venue, symbol, period, time_ms",
    "open_interest_conflicts": "conflict_id",
}


def test_upgrade_from_v2_keeps_every_v1_and_v2_row(conn: sqlite3.Connection) -> None:
    migrations = load_migrations()
    migrate(conn, applied_at_ms=AT, migrations=migrations[:2])
    for offset in range(3):
        conn.execute(
            "INSERT INTO candles VALUES ('binance-usdm', 'BTC/USDT:USDT', '5m', ?, '1', '2', '1', '2', '3', NULL, 1)",
            (1_792_676_700_000 + offset * 300_000,),
        )
        conn.execute(
            "INSERT INTO open_interest VALUES ('binance-usdm', 'BTC/USDT:USDT', '5m', ?, '5', '6', 1)",
            (1_792_676_700_000 + offset * 300_000,),
        )
    conn.execute(
        "INSERT INTO candle_conflicts (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
        " turnover, first_seen_at_ms) VALUES ('binance-usdm', 'BTC/USDT:USDT', '5m', 1792676700000,"
        " '1', '2', '1', '9', '3', NULL, 2)"
    )
    conn.execute(
        "INSERT INTO unavailable_candles VALUES ('binance-usdm', 'BTC/USDT:USDT', '5m', 1792677600000,"
        " 'EXCHANGE_NO_DATA', 3)"
    )
    conn.execute(
        "INSERT INTO open_interest_conflicts (venue, symbol, period, time_ms, open_interest, open_interest_value,"
        " first_seen_at_ms) VALUES ('binance-usdm', 'BTC/USDT:USDT', '5m', 1792676700000, '7', '8', 2)"
    )
    before = {table: table_digest(conn, table, V1_AND_V2_TABLES) for table in V1_AND_V2_TABLES}
    assert conn.execute("SELECT count(*) FROM open_interest").fetchone() == (3,)

    assert migrate(conn, applied_at_ms=AT + 1) == 3
    assert versions(conn) == [1, 2, 3]
    assert {table: table_digest(conn, table, V1_AND_V2_TABLES) for table in V1_AND_V2_TABLES} == before
    assert conn.execute("SELECT count(*) FROM funding_rates").fetchone() == (0,)
    assert migrate(conn, applied_at_ms=AT + 2) == 3


def test_applied_version(conn: sqlite3.Connection, tmp_path: Path) -> None:
    assert applied_version(conn) == 0
    migrate(conn, applied_at_ms=AT, migrations=(M1,))
    assert applied_version(conn) == 1
    migrate(conn, applied_at_ms=AT, migrations=load_migrations()[:2])
    assert applied_version(conn) == 2
    migrate(conn, applied_at_ms=AT)
    assert applied_version(conn) == 3

    foreign = open_writer(tmp_path / "foreign.db")
    foreign.execute("CREATE TABLE other (a INTEGER)")
    with pytest.raises(MigrationError):
        applied_version(foreign)
    foreign.close()

    ours_without_table = open_writer(tmp_path / "ours.db")
    ours_without_table.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    with pytest.raises(MigrationError):
        applied_version(ours_without_table)
    ours_without_table.close()
