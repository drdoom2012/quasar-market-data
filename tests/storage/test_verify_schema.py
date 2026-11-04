import hashlib
import sqlite3
from pathlib import Path

import pytest

from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import load_migrations, migrate
from quasar_market_data.storage.schema import SCHEMA_VERSION, EmptyDatabaseError, SchemaMismatchError, verify_schema

AT = 1_792_677_000_000
INSERT_CANDLE = "INSERT INTO candles VALUES ('binance-usdm', 'BTC/USDT:USDT', '5m', 1792676700000, '1', '2', '1', '2', '3', NULL, 1)"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "market.db"


def migrated(path: Path) -> sqlite3.Connection:
    writer = open_writer(path)
    migrate(writer, applied_at_ms=AT)
    return writer


def test_collector_migrates_and_api_verifies(db: Path) -> None:
    writer = migrated(db)
    reader = open_reader(db)
    try:
        verify_schema(reader)
    finally:
        reader.close()
        writer.close()


def test_reader_sees_a_candle_written_after_it_connected(db: Path) -> None:
    writer = migrated(db)
    reader = open_reader(db)
    try:
        assert reader.execute("SELECT count(*) FROM candles").fetchone() == (0,)
        writer.execute(INSERT_CANDLE)
        assert reader.execute("SELECT open_time_ms FROM candles").fetchall() == [(1792676700000,)]
        with pytest.raises(sqlite3.OperationalError):
            reader.execute(INSERT_CANDLE.replace("1792676700000", "1792677000000"))
    finally:
        reader.close()
        writer.close()


def test_unmigrated_database_is_a_mismatch(db: Path) -> None:
    open_writer(db).close()
    reader = open_reader(db)
    try:
        with pytest.raises(SchemaMismatchError):
            verify_schema(reader)
    finally:
        reader.close()


def test_a_file_with_no_objects_and_no_application_id_is_reported_as_empty(db: Path) -> None:
    db.touch()  # a zero-byte file, as the Collector creates it before initialising
    reader = open_reader(db)
    try:
        with pytest.raises(EmptyDatabaseError):
            verify_schema(reader)
    finally:
        reader.close()


def test_foreign_application_id_is_a_mismatch(db: Path) -> None:
    writer = migrated(db)
    writer.execute("PRAGMA application_id = 12345")
    reader = open_reader(db)
    try:
        with pytest.raises(SchemaMismatchError, match="application_id"):
            verify_schema(reader)
    finally:
        reader.close()
        writer.close()


def test_older_database_is_a_mismatch(db: Path) -> None:
    writer = migrated(db)
    writer.execute("DELETE FROM schema_migrations")
    reader = open_reader(db)
    try:
        with pytest.raises(SchemaMismatchError, match="version"):
            verify_schema(reader)
    finally:
        reader.close()
        writer.close()


def test_newer_database_is_a_mismatch(db: Path) -> None:
    writer = migrated(db)
    writer.execute("INSERT INTO schema_migrations VALUES (?, 'extra', 'x', 1)", (SCHEMA_VERSION + 1,))
    reader = open_reader(db)
    try:
        with pytest.raises(SchemaMismatchError, match="version"):
            verify_schema(reader)
    finally:
        reader.close()
        writer.close()


def test_verify_schema_never_writes(db: Path) -> None:
    open_writer(db).close()  # an empty, WAL-mode database: not migrated
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    reader = open_reader(db)
    try:
        with pytest.raises(SchemaMismatchError):
            verify_schema(reader)
    finally:
        reader.close()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_current_code_refuses_a_v1_database(db: Path) -> None:
    writer = open_writer(db)
    migrate(writer, applied_at_ms=AT, migrations=load_migrations()[:1])
    reader = open_reader(db)
    try:
        with pytest.raises(SchemaMismatchError):
            verify_schema(reader)
    finally:
        reader.close()
        writer.close()


def test_v3_code_refuses_a_v2_database(db: Path) -> None:
    writer = open_writer(db)
    migrate(writer, applied_at_ms=AT, migrations=load_migrations()[:2])
    reader = open_reader(db)
    try:
        with pytest.raises(SchemaMismatchError):
            verify_schema(reader)
    finally:
        reader.close()
        writer.close()
