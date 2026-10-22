"""A ``sqlite3.Connection`` that raises on chosen statements, to inject COMMIT, ROLLBACK and interrupt faults."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quasar_market_data.storage.migrate import migrate


@dataclass
class Fault:
    """Raise ``exception`` when the ``nth`` statement starting with ``prefix`` is executed."""

    prefix: str
    exception: BaseException
    nth: int = 1
    seen: int = 0

    def hit(self, sql: str) -> bool:
        if not sql.startswith(self.prefix):
            return False
        self.seen += 1
        return self.seen == self.nth


class FaultyConnection(sqlite3.Connection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.faults: list[Fault] = []
        self.statements: list[str] = []

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        self.statements.append(sql)
        for fault in self.faults:
            if fault.hit(sql):
                raise fault.exception
        return super().execute(sql, *args)


def open_faulty_writer(path: Path, applied_at_ms: int) -> FaultyConnection:
    """Like ``open_writer`` (WAL, FULL, recursive triggers) on a migrated database; add faults afterwards."""
    conn = sqlite3.connect(path, factory=FaultyConnection, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA recursive_triggers=ON")
    migrate(conn, applied_at_ms=applied_at_ms)
    conn.statements.clear()
    return conn
