"""A connection proxy that records, times and can intercept every statement a reader executes."""

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from time import perf_counter
from typing import Any


@dataclass
class Statement:
    sql: str
    params: Any
    seconds: float = 0.0
    rows: int = 0


class CursorProxy:
    def __init__(self, cursor: sqlite3.Cursor, record: Statement) -> None:
        self._cursor = cursor
        self._record = record

    def fetchone(self) -> Any:
        start = perf_counter()
        row = self._cursor.fetchone()
        self._record.seconds += perf_counter() - start
        self._record.rows += 0 if row is None else 1
        return row

    def __iter__(self) -> Iterator[Any]:
        return iter(self.fetchall())

    def fetchall(self) -> list[Any]:
        start = perf_counter()
        rows = self._cursor.fetchall()
        self._record.seconds += perf_counter() - start
        self._record.rows += len(rows)
        return rows


class StatementProxy:
    def __init__(self, conn: sqlite3.Connection, before: Callable[[str, Any], None] | None = None) -> None:
        self._conn = conn
        self._before = before
        self.statements: list[Statement] = []
        self.closed = False

    def execute(self, sql: str, params: Any = ()) -> CursorProxy:
        if self._before is not None:
            self._before(sql, params)
        record = Statement(sql, params)
        self.statements.append(record)
        start = perf_counter()
        cursor = self._conn.execute(sql, params)
        record.seconds += perf_counter() - start
        return CursorProxy(cursor, record)

    def interrupt(self) -> None:
        self._conn.interrupt()

    def close(self) -> None:
        self.closed = True
        self._conn.close()
