import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.api.database import read_ingest, read_ready
from quasar_market_data.api.errors import ApiError
from quasar_market_data.api.health_reader import _NEWEST, _NEXT_SERIES, StoredSeries, read_stored_series
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from quasar_market_data.storage.schema import SCHEMA_VERSION
from tests.api.seed import AT, SYMBOL, VENUE, insert_candle, make_database

TF = 300_000
S = 1792643400000


def slot(n: int) -> int:
    return S + n * TF


def not_ready(call: Callable[[], Any]) -> ApiError:
    with pytest.raises(ApiError) as caught:
        call()
    assert caught.value.status == 503
    assert caught.value.code == "not_ready"
    return caught.value


def foreign_sqlite(path: Path) -> Path:
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA application_id = 1")
    raw.execute("CREATE TABLE t (x)")
    raw.commit()
    raw.close()
    return path


def newer_schema(path: Path) -> Path:
    make_database(path, candles=[slot(0)])
    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO schema_migrations VALUES (?, 'x', 'x', 0)", (SCHEMA_VERSION + 1,))
    raw.commit()
    raw.close()
    return path


def text_file(path: Path) -> Path:
    path.write_text("hello")
    return path


def empty_file(path: Path) -> Path:
    path.touch()
    return path


@pytest.mark.parametrize("reader", [read_ready, read_ingest])
@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda p: p, "database_missing"),
        (empty_file, "database_unreadable"),
        (foreign_sqlite, "schema_mismatch"),
        (newer_schema, "schema_mismatch"),
        (text_file, "schema_mismatch"),
    ],
)
def test_missing_empty_foreign_newer_and_corrupt_database_map_to_the_503_reasons(
    tmp_path: Path, reader: Callable[[Path], Any], make: Callable[[Path], Path], reason: str
) -> None:
    path = make(tmp_path / "market.db")

    error = not_ready(lambda: reader(path))

    assert error.details == {"reason": reason}
    assert error.headers == ({} if reason == "schema_mismatch" else {"Retry-After": "5"})


def test_ready_returns_the_schema_version(tmp_path: Path) -> None:
    assert read_ready(make_database(tmp_path / "market.db")) == SCHEMA_VERSION == 2


@pytest.mark.parametrize("reader", [read_ready, read_ingest])
def test_readiness_is_checked_on_every_call(tmp_path: Path, reader: Callable[[Path], Any]) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0)])
    reader(path)

    path.unlink()
    assert not_ready(lambda: reader(path)).details == {"reason": "database_missing"}

    shutil.copy(foreign_sqlite(tmp_path / "other.db"), path)
    assert not_ready(lambda: reader(path)).details == {"reason": "schema_mismatch"}

    path.unlink()
    make_database(path, candles=[slot(0)])
    reader(path)  # a good file at the same path is served again


def test_no_series_gives_an_empty_tuple(tmp_path: Path) -> None:
    assert read_ingest(make_database(tmp_path / "market.db")) == ()


def test_rows_in_other_tables_do_not_create_a_series(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", unavailable=[slot(0), slot(1)])

    assert read_ingest(path) == ()


def test_the_series_list_describes_only_what_is_stored(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0)])  # a symbol configured elsewhere never wrote

    assert [s.symbol for s in read_ingest(path)] == [SYMBOL]


def test_series_and_newest_open_time_across_venues_symbols_and_timeframes(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db")
    conn = open_writer(path)
    keys = [(VENUE, SYMBOL, "5m"), (VENUE, SYMBOL, "15m"), (VENUE, "ETH/USDT:USDT", "5m"), ("bybit", SYMBOL, "5m")]
    for i, (venue, symbol, timeframe) in enumerate(keys):
        for n in range(i + 1):
            insert_candle(conn, slot(3 * n), symbol=symbol, venue=venue, timeframe=timeframe)
    conn.close()

    assert read_ingest(path) == (
        StoredSeries("binance-usdm", "BTC/USDT:USDT", "15m", slot(3)),
        StoredSeries("binance-usdm", "BTC/USDT:USDT", "5m", slot(0)),
        StoredSeries("binance-usdm", "ETH/USDT:USDT", "5m", slot(6)),
        StoredSeries("bybit", "BTC/USDT:USDT", "5m", slot(9)),
    )


def test_symbol_with_non_ascii_characters_round_trips(tmp_path: Path) -> None:
    symbol = "测试/USDT:USDT"
    path = make_database(tmp_path / "market.db", candles=[slot(0), slot(4)], symbol=symbol)

    assert read_ingest(path) == (StoredSeries(VENUE, symbol, "5m", slot(4)),)


def _oracle(conn: sqlite3.Connection) -> tuple[StoredSeries, ...]:
    rows = conn.execute(
        "SELECT venue, symbol, timeframe, MAX(open_time_ms) FROM candles"
        " GROUP BY venue, symbol, timeframe ORDER BY venue, symbol, timeframe"
    ).fetchall()
    return tuple(StoredSeries(*row) for row in rows)


def test_cursor_handles_prefix_colliding_and_unicode_keys(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db")
    conn = open_writer(path)
    n = 0
    for venue in ("v", "v2", "vé", "w"):
        for symbol in ("A", "A/B", "AB", "Aé", "A\x7f", "a"):
            for timeframe in ("5m", "15m", "1h"):
                for k in range(1 + (n % 3)):
                    insert_candle(conn, slot(k), symbol=symbol, venue=venue, timeframe=timeframe)
                n += 1
    conn.close()
    reader = open_reader(path)
    try:
        expected = _oracle(reader)
        assert len(expected) == 4 * 6 * 3  # no series was skipped or repeated by the cursor
        assert read_stored_series(reader) == expected
    finally:
        reader.close()


def test_a_series_at_the_ends_of_the_valid_time_range_is_read(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db")
    conn = open_writer(path)
    insert_candle(conn, 946_684_800_000, symbol="FIRST")
    insert_candle(conn, 4_102_444_800_000, symbol="LAST")
    conn.close()

    assert read_ingest(path) == (
        StoredSeries(VENUE, "FIRST", "5m", 946_684_800_000),
        StoredSeries(VENUE, "LAST", "5m", 4_102_444_800_000),
    )


def _bulk(path: Path, rows_per_series: list[int]) -> None:
    conn = open_writer(path)
    migrate(conn, applied_at_ms=AT)
    conn.execute("BEGIN")
    for i, count in enumerate(rows_per_series):
        conn.executemany(
            "INSERT INTO candles (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume, turnover,"
            " ingested_at_ms) VALUES (?, ?, ?, ?, '1', '2', '1', '1', '1', NULL, ?)",
            [(VENUE, f"S{i:04d}/USDT:USDT", "5m", slot(k), AT) for k in range(count)],
        )
    conn.execute("COMMIT")
    conn.close()


def _steps(path: Path, read: Callable[[sqlite3.Connection], Any]) -> int:
    conn = open_reader(path)
    count = 0

    def tick() -> int:
        nonlocal count
        count += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        read(conn)
    finally:
        conn.set_progress_handler(None, 1)
        conn.close()
    return count


def _group_by_reader(conn: sqlite3.Connection) -> tuple[StoredSeries, ...]:
    conn.execute("BEGIN")
    try:
        return _oracle(conn)
    finally:
        conn.execute("ROLLBACK")


def test_work_depends_on_series_not_on_rows(tmp_path: Path) -> None:
    few = tmp_path / "few.db"  # 600 series x 3 rows
    uneven = tmp_path / "uneven.db"  # one series with 200 000 rows, 200 with 1 row, 399 with 300
    _bulk(few, [3] * 600)
    _bulk(uneven, [200_000] + [1] * 200 + [300] * 399)

    few_steps = _steps(few, read_stored_series)
    uneven_steps = _steps(uneven, read_stored_series)

    assert uneven_steps < 2 * few_steps
    # the test can fail: a reader that scans every row is far outside the bound on the same databases
    assert _steps(uneven, _group_by_reader) > 2 * _steps(few, _group_by_reader)


def test_discovery_statement_plan_has_no_table_scan(tmp_path: Path) -> None:
    path = make_database(tmp_path / "market.db", candles=[slot(0)])
    conn = open_reader(path)
    try:
        plans = [
            " ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", args).fetchall())
            for sql, args in ((_NEXT_SERIES, ("", "", "", -1)), (_NEWEST, ("a", "b", "c")))
        ]
    finally:
        conn.close()

    for plan in plans:
        assert "SEARCH candles USING PRIMARY KEY" in plan or "SEARCH candles USING COVERING INDEX" in plan, plan
        assert "SCAN candles" not in plan, plan


class WriterAfterNthSeries:
    """A connection proxy: after the n-th next-series read, a second writer connection commits new data."""

    def __init__(self, real: sqlite3.Connection, writer_path: Path, after: int) -> None:
        self.real = real
        self.writer_path = writer_path
        self.after = after
        self.next_reads = 0

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        cursor = self.real.execute(sql, params)
        if "ORDER BY venue" in sql:
            self.next_reads += 1
            if self.next_reads == self.after:
                writer = open_writer(self.writer_path)
                insert_candle(
                    writer, slot(50), symbol="ZNEWSERIES"
                )  # a series that did not exist, sorting after the cursor
                insert_candle(writer, slot(60), symbol="S0000/USDT:USDT")  # newer data, series already read
                insert_candle(writer, slot(70), symbol="S0009/USDT:USDT")  # newer data, series not yet read
                writer.close()
        return cursor


def test_snapshot_is_consistent_while_a_writer_commits(tmp_path: Path) -> None:
    path = tmp_path / "market.db"
    _bulk(path, [2] * 10)
    expected = tuple(StoredSeries(VENUE, f"S{i:04d}/USDT:USDT", "5m", slot(1)) for i in range(10))
    reader = open_reader(path)
    try:
        seen = read_stored_series(WriterAfterNthSeries(reader, path, after=3))  # type: ignore[arg-type]
    finally:
        reader.close()

    assert seen == expected  # no new series, no newer newest_open_time_ms, from the first or the last series
    after = read_ingest(path)
    assert len(after) == 11
    assert StoredSeries(VENUE, "S0009/USDT:USDT", "5m", slot(70)) in after
