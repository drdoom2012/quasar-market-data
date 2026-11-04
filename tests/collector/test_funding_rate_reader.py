import asyncio
from pathlib import Path

import pytest

from quasar_market_data.collector.funding_rate_reader import (
    FundingRateReadError,
    SeriesFacts,
    read_series_facts,
    read_span,
)
from quasar_market_data.collector.funding_rate_writer import FundingRateWriter, SpanMerge
from quasar_market_data.domain.funding_rate import FundingRate, FundingRateKey, RateType
from quasar_market_data.domain.funding_rate_span import Interval
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import load_migrations, migrate

AT = 1_793_840_000_000
X = 1_793_836_800_000
HOUR = 3_600_000
VENUE = "binance-usdm"
SYMBOL = "BTC/USDT:USDT"


def row(time_ms: int, rate: str = "0.0001", symbol: str = SYMBOL, venue: str = VENUE) -> FundingRate:
    return FundingRate(FundingRateKey(venue, symbol, time_ms, RateType.REGULAR), rate, "1")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "market.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=AT)
    conn.close()
    return path


def write(db: Path, rows: list[FundingRate], merge: SpanMerge | None = None) -> None:
    writer = FundingRateWriter(open_writer(db))
    try:
        writer.write_transaction(rows, ingested_at_ms=AT, merge=merge)
    finally:
        writer.close()


def facts(db: Path, symbol: str = SYMBOL) -> SeriesFacts:
    return asyncio.run(read_series_facts(db, VENUE, symbol))


def test_a_series_without_anything_has_empty_facts(db: Path) -> None:
    assert facts(db) == SeriesFacts(SYMBOL, None, None, 0, 0)


def test_a_span_with_rows_and_conflicts(db: Path) -> None:
    rows = [row(X + i * HOUR) for i in range(5)]
    write(db, rows, SpanMerge(VENUE, SYMBOL, Interval(X, X + 5 * HOUR), AT + 7))
    write(db, [row(X + HOUR, "0.0009"), row(X + 2 * HOUR, "0.0009")])  # two conflicts, on two events
    write(db, [row(X + HOUR, "0.0008")])  # another observation of an already conflicted event
    assert facts(db) == SeriesFacts(SYMBOL, Interval(X, X + 5 * HOUR), AT + 7, 5, 2)


def test_other_series_do_not_leak_into_the_facts(db: Path) -> None:
    write(db, [row(X, symbol="ETH/USDT:USDT")], SpanMerge(VENUE, "ETH/USDT:USDT", Interval(X, X + HOUR), AT))
    write(db, [row(X + HOUR, venue="other-venue")])
    assert facts(db) == SeriesFacts(SYMBOL, None, None, 0, 0)
    assert facts(db, "ETH/USDT:USDT").stored_rows == 1


def test_the_read_does_not_take_the_write_lock(db: Path) -> None:
    holder = open_writer(db)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert facts(db).stored_rows == 0
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_two_reads_do_not_share_a_snapshot(db: Path) -> None:
    assert facts(db).stored_rows == 0
    write(db, [row(X)])
    assert facts(db).stored_rows == 1


def test_read_span_returns_the_span_and_its_time(db: Path) -> None:
    assert asyncio.run(read_span(db, VENUE, SYMBOL)) == (None, None)
    write(db, [], SpanMerge(VENUE, SYMBOL, Interval(X, X + HOUR), AT + 3))
    assert asyncio.run(read_span(db, VENUE, SYMBOL)) == (Interval(X, X + HOUR), AT + 3)


def test_an_old_schema_and_a_missing_database_are_read_errors(tmp_path: Path) -> None:
    old = tmp_path / "v2.db"
    conn = open_writer(old)
    migrate(conn, applied_at_ms=AT, migrations=load_migrations()[:2])
    conn.close()
    with pytest.raises(FundingRateReadError):
        facts(old)
    with pytest.raises(FundingRateReadError):
        facts(tmp_path / "missing.db")
    with pytest.raises(FundingRateReadError):
        asyncio.run(read_span(old, VENUE, SYMBOL))


def test_a_database_newer_than_the_code_is_a_read_error(db: Path) -> None:
    conn = open_writer(db)
    conn.execute("INSERT INTO schema_migrations VALUES (4, 'future', 'x', 1)")
    conn.close()
    with pytest.raises(FundingRateReadError, match="Schema"):
        facts(db)
