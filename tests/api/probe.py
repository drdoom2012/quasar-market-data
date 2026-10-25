"""A connection wrapper that runs a hook between two ``execute`` calls on the caller's thread.

No trace callback, no thread, no event: the hook runs between two completed statements, so it can write
through another connection without re-entering the connection under test.
"""

import sqlite3
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple


class Statement(NamedTuple):
    sql: str
    n: int  # 1-based count of statements other than BEGIN and ROLLBACK; 0 for those two
    parameters: Sequence[Any]


class InterleavingConnection:
    def __init__(self, conn: sqlite3.Connection, before_statement: Callable[[int, str], None]) -> None:
        self._conn = conn
        self._before_statement = before_statement
        self._count = 0
        self.statements: list[Statement] = []

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        keyword = sql.lstrip().split(None, 1)[0].upper()
        n = 0
        if keyword not in ("BEGIN", "ROLLBACK"):
            self._count += 1
            n = self._count
        self.statements.append(Statement(sql, n, tuple(parameters)))
        if n:
            self._before_statement(n, sql)
        return self._conn.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)
