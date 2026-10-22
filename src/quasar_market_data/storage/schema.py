"""Schema identity shared by the Collector (which migrates) and the API (which only verifies).

This module must not import ``storage.migrate``: the import-linter contract ``only the collector
migrates`` forbids the API from reaching the runner, directly or through other modules.
"""

import sqlite3
from typing import Final

from quasar_market_data.storage.connection import StorageError

APPLICATION_ID: Final = 0x514D4431  # "QMD1": refuse to touch a SQLite file that is not ours
SCHEMA_VERSION: Final = 1  # the highest migration shipped in ``storage.migrations``


class SchemaMismatchError(StorageError):
    """The database is not at the schema version this code was built for."""


def verify_schema(conn: sqlite3.Connection) -> None:
    """Raise ``SchemaMismatchError`` unless the database is ours and at exactly ``SCHEMA_VERSION``.

    Read-only: it never migrates. An unmigrated, foreign, older or newer database is a mismatch, so the
    API reports not ready instead of reading data it cannot interpret.
    """
    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    if application_id != APPLICATION_ID:
        raise SchemaMismatchError(f"application_id {application_id} is not this project's ({APPLICATION_ID})")
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if has_table is None:
        raise SchemaMismatchError("schema_migrations table is missing")
    applied = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    if applied != list(range(1, SCHEMA_VERSION + 1)):
        raise SchemaMismatchError(f"applied schema versions {applied} do not match the expected 1..{SCHEMA_VERSION}")
