"""Connection factories carrying the settings ADR-0002 P-1 relies on.

Connections use ``isolation_level=None``: transactions are explicit (``BEGIN IMMEDIATE`` ... ``COMMIT``).
The writer sets ``recursive_triggers=ON`` because without it ``INSERT OR REPLACE`` bypasses the
immutability triggers (ADR-0002 E-2); the pragma is per connection and is not persisted.
"""

import sqlite3
from pathlib import Path
from typing import Final

MIN_SQLITE_VERSION: Final = (3, 37, 0)  # STRICT tables
DEFAULT_BUSY_TIMEOUT_MS: Final = 5000


class StorageError(Exception):
    """Base class for storage-layer failures."""


class UnsupportedSqliteError(StorageError):
    """The SQLite library is older than ``MIN_SQLITE_VERSION``."""


def ensure_sqlite_version(version: tuple[int, ...] | None = None) -> None:
    """Raise ``UnsupportedSqliteError`` when ``version`` (default: the running library) is too old."""
    actual = sqlite3.sqlite_version_info if version is None else version
    if actual < MIN_SQLITE_VERSION:
        joined = ".".join(str(part) for part in actual)
        required = ".".join(str(part) for part in MIN_SQLITE_VERSION)
        raise UnsupportedSqliteError(f"SQLite {joined} is older than the required {required}")


def _ensure_busy_timeout(busy_timeout_ms: int) -> int:
    if type(busy_timeout_ms) is not int:
        raise TypeError(f"busy_timeout_ms must be int, got {type(busy_timeout_ms).__name__}")
    if busy_timeout_ms < 0:
        raise ValueError(f"busy_timeout_ms must be >= 0, got {busy_timeout_ms}")
    return busy_timeout_ms


def open_writer(path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open (creating if missing) the database for writing in WAL mode with the ADR-0002 P-1 settings."""
    ensure_sqlite_version()
    timeout = _ensure_busy_timeout(busy_timeout_ms)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode != "wal":
            raise StorageError(f"WAL journal mode is unavailable for {str(path)!r} (got {mode!r})")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA recursive_triggers=ON")
        conn.execute(f"PRAGMA busy_timeout={timeout}")
    except BaseException:
        conn.close()
        raise
    return conn


def open_reader(path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open an existing database read-only. A missing file raises ``sqlite3.OperationalError``; nothing is created."""
    ensure_sqlite_version()
    timeout = _ensure_busy_timeout(busy_timeout_ms)
    conn = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={timeout}")
    except BaseException:
        conn.close()
        raise
    return conn
