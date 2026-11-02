import asyncio
import sqlite3
from pathlib import Path

import pytest

from quasar_market_data.collector.open_interest_reader import OpenInterestReadError, read_series
from quasar_market_data.collector.open_interest_writer import OpenInterestWriter
from quasar_market_data.domain.open_interest import OpenInterest, OpenInterestKey
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import load_migrations, migrate

P5 = Timeframe("5m")
STEP = P5.milliseconds
BASE = 1_793_620_800_000
VENUE = "binance-usdm"
SYM = "BTC/USDT:USDT"


def row(n: int, symbol: str = SYM, value: str = "1", venue: str = VENUE) -> OpenInterest:
    return OpenInterest(OpenInterestKey(venue, symbol, P5, BASE + n * STEP), value, "2")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "market.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=BASE)
    writer = OpenInterestWriter(conn)
    writer.write_transaction([row(n) for n in (1, 2, 3, 5, 8)], ingested_at_ms=BASE)
    writer.write_transaction([row(2, value="9"), row(8, value="9")], ingested_at_ms=BASE)  # two conflicts
    writer.write_transaction([row(2, "ETH/USDT:USDT"), row(4, "ETH/USDT:USDT")], ingested_at_ms=BASE)
    writer.close()
    return path


def read(db: Path, **kw: object) -> object:
    args: dict[str, object] = {
        "venue": VENUE,
        "symbol": SYM,
        "period": P5,
        "oldest_ms": BASE,
        "newest_ms": BASE + 10 * STEP,
    }
    args.update(kw)
    return asyncio.run(read_series(db, **args))  # type: ignore[arg-type]


def test_rows_and_conflicts_inside_the_range_are_returned_ascending_and_inclusive(db: Path) -> None:
    facts = read(db, oldest_ms=BASE + 2 * STEP, newest_ms=BASE + 8 * STEP)
    assert facts.stored == tuple(BASE + n * STEP for n in (2, 3, 5, 8))  # type: ignore[attr-defined]
    assert facts.conflicted == tuple(BASE + n * STEP for n in (2, 8))  # type: ignore[attr-defined]
    assert facts.first_stored_ms is None and facts.stored_older is None  # type: ignore[attr-defined]


def test_rows_outside_the_range_are_excluded(db: Path) -> None:
    facts = read(db, oldest_ms=BASE + 4 * STEP, newest_ms=BASE + 7 * STEP)
    assert facts.stored == (BASE + 5 * STEP,) and facts.conflicted == ()  # type: ignore[attr-defined]


def test_only_the_requested_series_is_returned(db: Path) -> None:
    eth = read(db, symbol="ETH/USDT:USDT")
    assert eth.stored == (BASE + 2 * STEP, BASE + 4 * STEP)  # type: ignore[attr-defined]
    assert read(db, venue="other").stored == ()  # type: ignore[attr-defined]
    assert read(db, period=Timeframe("15m")).stored == ()  # type: ignore[attr-defined]


def test_history_figures_are_exact(db: Path) -> None:
    facts = read(db, oldest_ms=BASE + 4 * STEP, newest_ms=BASE + 10 * STEP, with_history=True)
    assert facts.first_stored_ms == BASE + 1 * STEP  # type: ignore[attr-defined]
    assert facts.stored_older == 3  # type: ignore[attr-defined]  # rows 1, 2, 3 are older than row 4
    empty = read(db, symbol="NONE/USDT:USDT", with_history=True)
    assert (empty.first_stored_ms, empty.stored_older) == (None, 0)  # type: ignore[attr-defined]


def test_8352_rows_are_read_in_one_call(tmp_path: Path) -> None:
    path = tmp_path / "big.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=BASE)
    writer = OpenInterestWriter(conn)
    for start in range(0, 8352, 100):
        writer.write_transaction([row(n) for n in range(start, min(start + 100, 8352))], ingested_at_ms=BASE)
    writer.close()
    facts = read(path, oldest_ms=BASE, newest_ms=BASE + 8351 * STEP)
    assert len(facts.stored) == 8352  # type: ignore[attr-defined]


def test_a_schema_1_database_is_a_read_error(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=BASE, migrations=load_migrations()[:1])
    conn.close()
    with pytest.raises(OpenInterestReadError):
        read(path)


def test_a_missing_or_corrupt_file_is_a_read_error(tmp_path: Path) -> None:
    with pytest.raises(OpenInterestReadError):
        read(tmp_path / "missing.db")
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database" * 100)
    with pytest.raises(OpenInterestReadError):
        read(garbage)


def test_the_reader_cannot_write(db: Path) -> None:
    conn = open_reader(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM open_interest")
    conn.close()
