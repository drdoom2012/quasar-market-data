"""Forward-only, checksummed, atomic migration runner (ADR-0002 P-8). Used by the Collector only.

Rules, all checked before anything is changed: the file is either fresh (``application_id`` 0 and no
objects) or ours; the applied versions are exactly ``1..k`` and match the shipped files by name and
checksum; the database is not newer than the code. Each pending migration runs in its own
``BEGIN IMMEDIATE`` transaction together with its ``schema_migrations`` row, so it applies completely or
not at all. ``executescript`` is not used because it commits implicitly.
"""

import hashlib
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Final

from quasar_market_data.domain.timestamps import ensure_timestamp_ms
from quasar_market_data.storage.connection import StorageError
from quasar_market_data.storage.schema import APPLICATION_ID

_FILE_NAME: Final = re.compile(r"(\d{3})_([a-z0-9_]+)\.sql", re.ASCII)


class MigrationError(StorageError):
    """A migration cannot be applied, or the database is not in a state the runner accepts."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str

    @classmethod
    def from_bytes(cls, version: int, name: str, raw: bytes) -> "Migration":
        return cls(version=version, name=name, sql=raw.decode("utf-8"), checksum=hashlib.sha256(raw).hexdigest())


def _ensure_contiguous(migrations: Sequence[Migration]) -> None:
    if [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
        raise MigrationError(f"migration versions must run 1..n without gaps, got {[m.version for m in migrations]}")


def load_migrations(source: Traversable | None = None) -> tuple[Migration, ...]:
    """Load ``NNN_name.sql`` files (default: the packaged ones), ordered by version."""
    directory = resources.files("quasar_market_data.storage.migrations") if source is None else source
    found: list[Migration] = []
    for entry in directory.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = _FILE_NAME.fullmatch(entry.name)
        if match is None:
            raise MigrationError(f"migration file name {entry.name!r} must match NNN_name.sql (lower case)")
        found.append(Migration.from_bytes(int(match.group(1)), match.group(2), entry.read_bytes()))
    found.sort(key=lambda m: m.version)
    _ensure_contiguous(found)
    return tuple(found)


def _split_statements(sql: str) -> list[str]:
    """Split on statement boundaries; ``complete_statement`` keeps trigger bodies in one piece."""
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        if not buffer and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if buffer.strip():
        raise MigrationError("migration ends with an unterminated statement")
    return statements


def _applied(conn: sqlite3.Connection, shipped: Sequence[Migration]) -> int:
    """Return how many shipped migrations are applied, refusing any inconsistency."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if has_table is None:
        raise MigrationError("database carries the project application_id but has no schema_migrations table")
    rows = conn.execute("SELECT version, name, checksum_sha256 FROM schema_migrations ORDER BY version").fetchall()
    if len(rows) > len(shipped):
        raise MigrationError(f"database is at version {len(rows)}, newer than the code ({len(shipped)})")
    for expected, (version, name, checksum) in enumerate(rows, start=1):
        if version != expected:
            raise MigrationError(f"gap in applied migrations: expected version {expected}, found {version}")
        migration = shipped[expected - 1]
        if (name, checksum) != (migration.name, migration.checksum):
            raise MigrationError(f"checksum or name mismatch for applied migration {version} ({name})")
    return len(rows)


def _apply(conn: sqlite3.Connection, migration: Migration, applied_at_ms: int, *, claim_database: bool) -> None:
    statements = _split_statements(migration.sql)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if claim_database:
            conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum_sha256, applied_at_ms) VALUES (?, ?, ?, ?)",
            (migration.version, migration.name, migration.checksum, applied_at_ms),
        )
        conn.execute("COMMIT")
    except Exception as error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(f"migration {migration.version} ({migration.name}) failed: {error}") from error
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def migrate(conn: sqlite3.Connection, *, applied_at_ms: int, migrations: Sequence[Migration] | None = None) -> int:
    """Bring the database to the latest shipped version and return that version.

    ``conn`` must be an autocommit-style connection (``isolation_level=None``), as ``open_writer`` returns.
    ``applied_at_ms`` is the wall-clock time recorded for newly applied migrations.
    """
    ensure_timestamp_ms(applied_at_ms, "applied_at_ms")
    if conn.isolation_level is not None:
        raise MigrationError("connection must use isolation_level=None (explicit transactions)")
    shipped = load_migrations() if migrations is None else tuple(migrations)
    _ensure_contiguous(shipped)

    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    has_objects = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] > 0
    fresh = application_id == 0 and not has_objects
    if not fresh and application_id != APPLICATION_ID:
        raise MigrationError("not a quasar-market-data database (application_id or contents are foreign)")

    done = 0 if fresh else _applied(conn, shipped)
    for migration in shipped[done:]:
        _apply(conn, migration, applied_at_ms, claim_database=fresh and migration.version == 1)
    return len(shipped)
