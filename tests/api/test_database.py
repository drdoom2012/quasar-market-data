import errno
import logging
import os
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.api.candle_reader import CandlesPage, read_candles_page
from quasar_market_data.api.database import read_candles
from quasar_market_data.api.errors import ApiError
from quasar_market_data.api.params import CandlesRequest
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import UnsupportedSqliteError, open_reader, open_writer
from tests.api.seed import SYMBOL, VENUE, insert_candle, make_database

TF = 300_000
S = 1792643400000
POISON = "secret /var/db/x SELECT"


def slot(n: int) -> int:
    return S + n * TF


def req(symbol: str = SYMBOL) -> CandlesRequest:
    return CandlesRequest(VENUE, symbol, Timeframe("5m"), slot(0), slot(5), 1000)


def not_ready(path: Path, **kwargs: Any) -> ApiError:
    with pytest.raises(ApiError) as caught:
        read_candles(path, req(), **kwargs)
    assert caught.value.status == 503
    assert caught.value.code == "not_ready"
    return caught.value


def fail_stat_for(monkeypatch: pytest.MonkeyPatch, path: Path, error: BaseException) -> None:
    real = os.stat

    def fake(target: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(target) == path:
            raise error
        return real(target, *args, **kwargs)

    monkeypatch.setattr("quasar_market_data.api.database.os.stat", fake)


class Spy:
    """A real connection whose first ``execute`` can fail and whose ``close`` is observable."""

    def __init__(self, real: sqlite3.Connection, error: BaseException | None = None, close_error: bool = False) -> None:
        self.real = real
        self.error = error
        self.close_error = close_error
        self.closed = False

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return self.real.execute(sql, parameters)

    def close(self) -> None:
        self.closed = True
        self.real.close()
        if self.close_error:
            raise sqlite3.OperationalError(POISON)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


def spy_factory(spies: list[Spy], **kwargs: Any) -> Callable[[Path], sqlite3.Connection]:
    def factory(path: Path) -> sqlite3.Connection:
        spy = Spy(open_reader(path), **kwargs)
        spies.append(spy)
        return spy  # type: ignore[return-value]

    return factory


def foreign_sqlite(path: Path) -> Path:
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA application_id = 1")
    raw.execute("CREATE TABLE t (x)")
    raw.commit()
    raw.close()
    return path


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return make_database(tmp_path / "market.db", candles=[slot(0), slot(2)])


def test_a_missing_file_is_database_missing_and_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "nope.db"

    error = not_ready(path)

    assert error.details == {"reason": "database_missing"}
    assert error.headers == {"Retry-After": "5"}
    assert not path.exists()


def test_a_missing_parent_directory_is_database_missing(tmp_path: Path) -> None:
    assert not_ready(tmp_path / "no" / "such" / "x.db").details == {"reason": "database_missing"}


def test_a_path_below_a_regular_file_is_database_missing(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")

    assert not_ready(tmp_path / "file" / "x.db").details == {"reason": "database_missing"}


def test_schema_problems_are_schema_mismatch_without_retry_after(tmp_path: Path) -> None:
    newer = make_database(tmp_path / "newer.db", candles=[slot(0)])
    raw = sqlite3.connect(newer)
    raw.execute("INSERT INTO schema_migrations VALUES (2, 'x', 'x', 0)")
    raw.commit()
    raw.close()
    text = tmp_path / "text.db"
    text.write_text("hello")  # SQLITE_NOTADB

    for path in (foreign_sqlite(tmp_path / "foreign.db"), newer, text):
        error = not_ready(path)
        assert error.details == {"reason": "schema_mismatch"}
        assert error.headers == {}


@pytest.mark.parametrize(
    "stat_error",
    [PermissionError(errno.EACCES, POISON), OSError(errno.EIO, "io"), OSError(errno.ELOOP, "loop")],
)
def test_a_permission_error_while_inspecting_the_path_is_database_unreadable(
    db: Path, monkeypatch: pytest.MonkeyPatch, stat_error: OSError
) -> None:
    def factory(path: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    fail_stat_for(monkeypatch, db, stat_error)

    error = not_ready(db, open_connection=factory)

    assert error.details == {"reason": "database_unreadable"}
    assert error.headers == {"Retry-After": "5"}


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="root and Windows bypass directory permissions"
)
def test_a_real_unsearchable_directory_is_database_unreadable(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    path = make_database(locked / "market.db", candles=[slot(0)])
    locked.chmod(0o000)
    try:
        assert not_ready(path).details == {"reason": "database_unreadable"}
    finally:
        locked.chmod(0o755)


def test_a_directory_at_the_path_is_database_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "dir.db"
    path.mkdir()

    assert not_ready(path).details == {"reason": "database_unreadable"}


@pytest.mark.parametrize("error", [PermissionError(errno.EACCES, "denied"), OSError(errno.EMFILE, "too many")])
def test_an_oserror_from_the_connection_factory_is_database_unreadable(db: Path, error: OSError) -> None:
    def factory(path: Path) -> sqlite3.Connection:
        raise error

    assert not_ready(db, open_connection=factory).details == {"reason": "database_unreadable"}


def test_there_is_no_filesystem_check_before_the_open(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fail_stat_for(monkeypatch, db, AssertionError("stat before open"))

    page = read_candles(db, req())

    assert [c.open_time_ms for c in page.candles] == [slot(0), slot(2)]


def test_a_file_that_appears_while_the_open_fails_is_unreadable_not_missing_and_not_500(tmp_path: Path) -> None:
    path = tmp_path / "late.db"

    def factory(target: Path) -> sqlite3.Connection:
        make_database(target, candles=[slot(0)])
        raise sqlite3.OperationalError("unable to open database file")

    assert not_ready(path, open_connection=factory).details == {"reason": "database_unreadable"}
    assert read_candles(path, req()).candles  # the next call succeeds


def test_a_file_that_disappears_while_the_open_fails_is_missing(db: Path) -> None:
    def factory(target: Path) -> sqlite3.Connection:
        target.unlink()
        raise sqlite3.OperationalError("unable to open database file")

    assert not_ready(db, open_connection=factory).details == {"reason": "database_missing"}


@pytest.mark.parametrize("how", ["removed", "replaced"])
def test_a_file_removed_or_replaced_after_the_open_is_a_page_or_a_503_never_a_500_or_an_empty_200(
    tmp_path: Path, how: str
) -> None:
    pristine = read_candles(make_database(tmp_path / "pristine.db", candles=[slot(0), slot(2)]), req())
    path = make_database(tmp_path / "market.db", candles=[slot(0), slot(2)])
    foreign = foreign_sqlite(tmp_path / "foreign.db")
    spies: list[Spy] = []
    inner = spy_factory(spies)

    def factory(target: Path) -> sqlite3.Connection:
        conn = inner(target)
        if how == "removed":
            target.unlink()
        else:
            os.replace(foreign, target)
        return conn

    try:
        result: CandlesPage | ApiError = read_candles(path, req(), open_connection=factory)
    except ApiError as error:
        result = error

    if isinstance(result, ApiError):
        assert result.status == 503
        assert result.code == "not_ready"
        assert result.details["reason"] in {"schema_mismatch", "database_unreadable"}
    else:
        assert result == pristine
    assert spies[0].closed


@pytest.mark.parametrize(
    "case",
    ["open_failed", "malformed", "locked", "unsupported_sqlite", "oserror_on_read"],
)
def test_unreadable_conditions_are_database_unreadable_with_retry_after(db: Path, case: str) -> None:
    spies: list[Spy] = []
    factory: Callable[[Path], sqlite3.Connection]
    if case == "open_failed":

        def factory(path: Path) -> sqlite3.Connection:
            raise sqlite3.OperationalError("unable to open database file")

    elif case == "unsupported_sqlite":

        def factory(path: Path) -> sqlite3.Connection:
            raise UnsupportedSqliteError("too old")

    else:
        errors: dict[str, BaseException] = {
            "malformed": sqlite3.DatabaseError("database disk image is malformed"),
            "locked": sqlite3.OperationalError("database is locked"),
            "oserror_on_read": OSError(errno.EIO, "io"),
        }
        factory = spy_factory(spies, error=errors[case])

    error = not_ready(db, open_connection=factory)

    assert error.details == {"reason": "database_unreadable"}
    assert error.headers == {"Retry-After": "5"}
    assert all(spy.closed for spy in spies)


@pytest.mark.parametrize(
    "bug",
    [
        sqlite3.ProgrammingError("Incorrect number of bindings"),
        sqlite3.InterfaceError("x"),
        sqlite3.IntegrityError("x"),
        TypeError("x"),
        KeyError("x"),
        AssertionError("x"),
        RuntimeError("x"),
    ],
)
def test_programming_errors_propagate_unchanged(db: Path, bug: BaseException) -> None:
    spies: list[Spy] = []

    with pytest.raises(type(bug)) as caught:
        read_candles(db, req(), open_connection=spy_factory(spies, error=bug))

    assert caught.value is bug
    assert spies[0].closed


def _sqlite_error(code: int, message: str = "x") -> sqlite3.OperationalError:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize("code", [5, 6, 10, 11, 13, 14, 15, 3, 7, 9, 264, 520, 1544])  # BUSY ... READONLY_DIRECTORY
def test_sqlite_environment_codes_are_database_unreadable(db: Path, code: int) -> None:
    spies: list[Spy] = []

    error = not_ready(db, open_connection=spy_factory(spies, error=_sqlite_error(code)))

    assert error.details == {"reason": "database_unreadable"}
    assert spies[0].closed


@pytest.mark.parametrize("code", [1, 8, 19, 21, 25, 2, 12])  # ERROR, plain READONLY, CONSTRAINT, MISUSE, RANGE ...
def test_sqlite_bug_codes_propagate_as_bugs(db: Path, code: int) -> None:
    bug = _sqlite_error(code)

    with pytest.raises(sqlite3.OperationalError) as caught:
        read_candles(db, req(), open_connection=spy_factory([], error=bug))

    assert caught.value is bug


def test_a_real_bad_statement_and_a_real_write_attempt_are_bugs_not_a_503(db: Path) -> None:
    conn = open_reader(db)
    try:
        with pytest.raises(sqlite3.OperationalError) as typo:
            conn.execute("SELECT opn FROM candles")
        with pytest.raises(sqlite3.OperationalError) as write:
            conn.execute("DELETE FROM candles")
    finally:
        conn.close()
    assert typo.value.sqlite_errorcode == 1
    assert write.value.sqlite_errorcode == 8

    for bug in (typo.value, write.value):
        with pytest.raises(sqlite3.OperationalError) as caught:
            read_candles(db, req(), open_connection=spy_factory([], error=bug))
        assert caught.value is bug


def test_using_a_closed_connection_is_a_bug_not_a_503(db: Path) -> None:
    def factory(path: Path) -> sqlite3.Connection:
        conn = open_reader(path)
        conn.close()
        return conn

    with pytest.raises(sqlite3.ProgrammingError):
        read_candles(db, req(), open_connection=factory)


def test_the_connection_is_closed_on_every_path(db: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    spies: list[Spy] = []
    read_candles(db, req(), open_connection=spy_factory(spies))  # success
    with pytest.raises(ApiError):
        read_candles(db, req("X/Y"), open_connection=spy_factory(spies))  # unknown series
    with pytest.raises(ApiError):
        read_candles(foreign_sqlite(tmp_path / "f.db"), req(), open_connection=spy_factory(spies))  # schema
    with pytest.raises(ApiError):
        read_candles(db, req(), open_connection=spy_factory(spies, error=sqlite3.OperationalError("locked")))
    with pytest.raises(RuntimeError):
        read_candles(db, req(), open_connection=spy_factory(spies, error=RuntimeError("x")))
    assert len(spies) == 5
    assert all(spy.closed for spy in spies)

    caplog.set_level(logging.WARNING, logger="quasar_market_data.api.database")
    closing: list[Spy] = []
    page = read_candles(db, req(), open_connection=spy_factory(closing, close_error=True))  # close() raises
    assert page.candles  # the result is kept
    assert "closing" in caplog.text


def test_no_internal_text_reaches_the_error(
    db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="quasar_market_data.api.database")
    errors: list[ApiError] = []
    errors.append(not_ready(db, open_connection=spy_factory([], error=sqlite3.OperationalError(POISON))))
    errors.append(not_ready(db, open_connection=spy_factory([], error=OSError(errno.EIO, POISON))))

    def factory(path: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    fail_stat_for(monkeypatch, db, PermissionError(errno.EACCES, POISON))
    errors.append(not_ready(db, open_connection=factory))

    for error in errors:
        visible = f"{error.message} {error.details} {error.headers} {error.body()}"
        assert not any(text in visible for text in ("secret", "/var", "SELECT"))
    assert "secret /var/db/x SELECT" in caplog.text


def test_readiness_is_checked_on_every_call(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0)])
    original = tmp_path / "original.db"
    shutil.copy(path, original)

    assert read_candles(path, req()).candles
    os.replace(foreign_sqlite(tmp_path / "foreign.db"), path)
    assert not_ready(path).details == {"reason": "schema_mismatch"}
    path.unlink()
    assert not_ready(path).details == {"reason": "database_missing"}
    shutil.copy(original, path)
    assert read_candles(path, req()).candles


def test_unknown_series_is_a_404_after_readiness(db: Path, tmp_path: Path) -> None:
    with pytest.raises(ApiError) as caught:
        read_candles(db, req("X/Y"))
    assert caught.value.status == 404
    assert caught.value.code == "unknown_series"
    assert caught.value.details == {"venue": VENUE, "symbol": "X/Y", "timeframe": "5m"}

    broken = foreign_sqlite(tmp_path / "foreign.db")
    with pytest.raises(ApiError) as caught_503:
        read_candles(broken, req("X/Y"))
    assert caught_503.value.status == 503


def test_reads_a_database_after_the_collector_closed_it(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0), slot(1)])
    writer = open_writer(path)
    insert_candle(writer, slot(2))
    writer.close()

    page = read_candles(path, req())

    assert [c.open_time_ms for c in page.candles] == [slot(0), slot(1), slot(2)]


def test_reads_only_committed_data_while_a_writer_transaction_is_open(db: Path) -> None:
    writer = open_writer(db, busy_timeout_ms=1000)
    try:
        writer.execute("BEGIN IMMEDIATE")
        insert_candle(writer, slot(3))
        started = time.monotonic()

        page = read_candles(db, req())

        assert time.monotonic() - started < 5
        assert [c.open_time_ms for c in page.candles] == [slot(0), slot(2)]
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_the_reader_page_matches_read_candles_page(db: Path) -> None:
    conn = open_reader(db)
    try:
        assert read_candles(db, req()) == read_candles_page(conn, req())
    finally:
        conn.close()
