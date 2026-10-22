import sqlite3
from pathlib import Path

import pytest

from quasar_market_data.storage.connection import (
    DEFAULT_BUSY_TIMEOUT_MS,
    StorageError,
    UnsupportedSqliteError,
    ensure_sqlite_version,
    open_reader,
    open_writer,
)
from tests.typing_helpers import untyped


def pragma(conn: sqlite3.Connection, name: str) -> object:
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_ensure_sqlite_version() -> None:
    with pytest.raises(UnsupportedSqliteError):
        ensure_sqlite_version((3, 36, 9))
    ensure_sqlite_version((3, 37, 0))
    ensure_sqlite_version((3, 53, 1))
    ensure_sqlite_version()  # the running version


def test_writer_connection_settings(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    conn = open_writer(path)
    try:
        assert pragma(conn, "journal_mode") == "wal"
        assert pragma(conn, "synchronous") == 2
        assert pragma(conn, "recursive_triggers") == 1
        assert pragma(conn, "busy_timeout") == DEFAULT_BUSY_TIMEOUT_MS == 5000
        assert conn.isolation_level is None
    finally:
        conn.close()
    assert path.exists()

    conn = open_writer(path, busy_timeout_ms=1234)
    try:
        assert pragma(conn, "busy_timeout") == 1234
    finally:
        conn.close()


def test_writer_refuses_a_database_without_wal() -> None:
    with pytest.raises(StorageError):
        open_writer(Path(":memory:"))


def test_writer_busy_timeout_must_be_a_non_negative_int(tmp_path: Path) -> None:
    for bad in (True, 1.5):
        with pytest.raises(TypeError):
            open_writer(tmp_path / "m.db", busy_timeout_ms=untyped(bad))
    with pytest.raises(ValueError):
        open_writer(tmp_path / "m.db", busy_timeout_ms=-1)
    assert not (tmp_path / "m.db").exists()


def test_reader_is_read_only_and_sees_committed_writes(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    writer = open_writer(path)
    writer.execute("CREATE TABLE t (a INTEGER)")
    writer.execute("INSERT INTO t VALUES (1)")
    reader = open_reader(path)
    try:
        assert reader.execute("SELECT a FROM t").fetchall() == [(1,)]
        assert pragma(reader, "query_only") == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("CREATE TABLE u (a INTEGER)")
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO t VALUES (2)")
    finally:
        reader.close()
        writer.close()


def test_reader_does_not_create_a_missing_database(tmp_path: Path) -> None:
    with pytest.raises(sqlite3.OperationalError):
        open_reader(tmp_path / "missing.db")
    assert list(tmp_path.iterdir()) == []


def test_unsupported_sqlite_blocks_both_factories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    existing = tmp_path / "existing.db"
    open_writer(existing).close()
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 0))
    with pytest.raises(UnsupportedSqliteError):
        open_writer(tmp_path / "new.db")
    with pytest.raises(UnsupportedSqliteError):
        open_reader(existing)
    assert not (tmp_path / "new.db").exists()
