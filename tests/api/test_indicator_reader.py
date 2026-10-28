from pathlib import Path

import pytest

from quasar_market_data.api.candle_reader import UnknownSeries
from quasar_market_data.api.database import read_indicators
from quasar_market_data.api.indicator_reader import IndicatorInputs, read_indicator_inputs
from quasar_market_data.api.params import IndicatorsRequest
from quasar_market_data.domain.indicators import parse_indicators
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from tests.api.seed import (
    AT,
    SYMBOL,
    VENUE,
    candle_values,
    insert_candle,
    insert_conflict,
    insert_unavailable,
    make_database,
)

TF = 300_000
S = 1792643400000


def slot(n: int) -> int:
    return S + n * TF


def request(start: int, end: int | None = None, symbol: str = SYMBOL) -> IndicatorsRequest:
    return IndicatorsRequest(VENUE, symbol, Timeframe("5m"), start, end, parse_indicators("rsi:period=14"))


def test_reads_exactly_the_read_range(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=AT)
    for n in range(-1, 8):  # slot(-1) before the read range, slot(7) == end
        insert_candle(conn, slot(n))
        insert_conflict(conn, slot(n), 0)
        insert_unavailable(conn, slot(n))
    conn.close()

    read_start, end = slot(0), slot(7)
    statements: list[str] = []
    reader = open_reader(path)
    reader.set_trace_callback(statements.append)
    try:
        inputs = read_indicator_inputs(reader, request(slot(2), end), end=end, read_start=read_start)
    finally:
        reader.close()

    wanted = {slot(n) for n in range(7)}
    assert set(inputs.prices) == wanted
    assert inputs.conflicts == wanted
    assert inputs.unavailable == wanted
    assert (inputs.read_start, inputs.end, inputs.start) == (read_start, end, slot(2))
    assert (inputs.oldest_open_time_ms, inputs.newest_open_time_ms) == (slot(-1), slot(7))
    ranged = [s for s in statements if "open_time_ms >=" in s or ">= " in s and "open_time_ms" in s]
    assert len(ranged) == 3
    for statement in ranged:
        assert str(read_start) in statement and str(end) in statement
        assert str(slot(-1)) not in statement


def test_one_transaction(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0), slot(1)])
    statements: list[str] = []
    reader = open_reader(path)
    reader.set_trace_callback(statements.append)
    try:
        read_indicator_inputs(reader, request(slot(0), slot(2)), end=slot(2), read_start=slot(0))
    finally:
        reader.close()
    assert statements[0] == "BEGIN"
    assert statements[-1] == "ROLLBACK"
    assert statements.count("BEGIN") == 1 and statements.count("ROLLBACK") == 1


def test_unknown_series_raises(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", candles=[slot(0)])
    reader = open_reader(path)
    try:
        with pytest.raises(UnknownSeries):
            read_indicator_inputs(reader, request(slot(0), slot(1), "ETH/USDT:USDT"), end=slot(1), read_start=slot(0))
        assert not reader.in_transaction
    finally:
        reader.close()


def test_closes_are_the_stored_text(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=AT)
    insert_candle(conn, slot(0), close="80000.1")
    insert_candle(conn, slot(1), close="79999.123456789012")
    conn.close()

    inputs = read_indicators(path, request(slot(0), slot(2)), end=slot(2), read_start=slot(0))
    assert isinstance(inputs, IndicatorInputs)
    assert {t: prices[2] for t, prices in inputs.prices.items()} == {
        slot(0): "80000.1",
        slot(1): "79999.123456789012",
    }
    assert all(type(v) is str for prices in inputs.prices.values() for v in prices)


def test_reads_high_low_close_of_the_read_range(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=AT)
    for n in range(-2, 4):  # slots -2 and -1 are before the read range
        insert_candle(conn, slot(n))
    conn.close()

    inputs = read_indicators(path, request(slot(0), slot(4)), end=slot(4), read_start=slot(0))
    assert isinstance(inputs, IndicatorInputs)
    assert set(inputs.prices) == {slot(n) for n in range(4)}
    for n in range(4):
        _open, high, low, close, *_ = candle_values(slot(n))
        assert inputs.prices[slot(n)] == (high, low, close)
