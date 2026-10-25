"""A fresh read-only connection per request, and the mapping of storage failures to the contract's errors.

Readiness is decided by the open and the reads themselves: there is no filesystem check before the open (no
check-then-open race). A failed open is classified afterwards with one ``os.stat``. Only environment failures
become ``503``; programming errors propagate and end as ``500`` (contract section 2: "500 is reserved for bugs").
"""

import logging
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

from quasar_market_data.api.candle_reader import CandlesPage, UnknownSeries, read_candles_page
from quasar_market_data.api.errors import ApiError, NotReadyReason, not_ready, unknown_series
from quasar_market_data.api.params import CandlesRequest
from quasar_market_data.storage.connection import StorageError, open_reader
from quasar_market_data.storage.schema import SchemaMismatchError

logger = logging.getLogger(__name__)

type ConnectionFactory = Callable[[Path], sqlite3.Connection]

_SQLITE_NOTADB: Final = 26
_SQLITE_READONLY: Final = 8
# Primary result codes that describe the environment, not the code: PERM, BUSY, LOCKED, NOMEM, INTERRUPT, IOERR,
# CORRUPT, FULL, CANTOPEN, PROTOCOL. SQLITE_ERROR (a bad statement), a plain SQLITE_READONLY (a write attempt),
# MISUSE, RANGE, CONSTRAINT and the rest are bugs once the schema check has passed. The extended READONLY_*
# codes (recovery, cannot lock, directory, ...) are environmental: the database cannot be opened read-only.
_ENVIRONMENT_CODES: Final = frozenset({3, 5, 6, 7, 9, 10, 11, 13, 14, 15})


def read_candles(
    db_path: Path, request: CandlesRequest, *, open_connection: ConnectionFactory = open_reader
) -> CandlesPage:
    try:
        conn = open_connection(db_path)
    except StorageError as error:
        raise _not_ready("database_unreadable", error) from None
    except (sqlite3.Error, OSError) as error:
        raise _not_ready(_classify_open_failure(db_path), error) from None

    try:
        return read_candles_page(conn, request)
    except UnknownSeries:
        raise unknown_series(request.venue, request.symbol, request.timeframe.label) from None
    except SchemaMismatchError as error:
        raise _not_ready("schema_mismatch", error) from None
    except StorageError as error:
        raise _not_ready("database_unreadable", error) from None
    except sqlite3.DatabaseError as error:
        if getattr(error, "sqlite_errorcode", None) == _SQLITE_NOTADB:
            raise _not_ready("schema_mismatch", error) from None
        if _is_environment_failure(error):
            raise _not_ready("database_unreadable", error) from None
        raise
    except OSError as error:
        raise _not_ready("database_unreadable", error) from None
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            logger.warning("closing the read connection failed", exc_info=True)


def _is_environment_failure(error: sqlite3.DatabaseError) -> bool:
    code: int | None = getattr(error, "sqlite_errorcode", None)
    if code is None:  # built in Python, not by SQLite: keep the class-based rule
        return isinstance(error, sqlite3.OperationalError) or type(error) is sqlite3.DatabaseError
    primary = code & 0xFF
    if primary == _SQLITE_READONLY:
        return code != _SQLITE_READONLY
    return primary in _ENVIRONMENT_CODES


def _classify_open_failure(db_path: Path) -> Literal["database_missing", "database_unreadable"]:
    try:
        os.stat(db_path)
    except (FileNotFoundError, NotADirectoryError):
        return "database_missing"
    except OSError:
        return "database_unreadable"  # PermissionError, ELOOP, EIO, ...: the path cannot even be inspected
    return "database_unreadable"  # it exists but could not be opened (or is not a regular file)


def _not_ready(reason: NotReadyReason, cause: BaseException) -> ApiError:
    logger.warning("database not ready (%s)", reason, exc_info=cause)
    return not_ready(reason)
