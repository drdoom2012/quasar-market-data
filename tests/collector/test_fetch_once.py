import asyncio
import json
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector.async_candle_writer import AsyncCandleWriter
from quasar_market_data.collector.binance_klines import KlineParseError
from quasar_market_data.collector.candle_writer import CandleWriteOutcomeUnknown, WriteReport
from quasar_market_data.collector.fetch_once import (
    MAX_LIMIT,
    FetchOnceConfig,
    FetchOnceReport,
    fetch_once,
)
from quasar_market_data.collector.kline_source import SourceError
from quasar_market_data.domain.server_time import TimeRejection
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_writer
from quasar_market_data.storage.migrate import migrate
from tests.collector.test_candle_writer import AT
from tests.typing_helpers import untyped

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "binance"
SYMBOL = "BTC/USDT:USDT"
FIVE_MINUTES = Timeframe("5m")
FORMING_SERVER_TIME_MS = 1792677035725  # recorded server time of the forming fixture
ROW0_END_MS = 1792677000000  # end of the closed row of the forming fixture
WALL_START_MS = 1792677036000
MONO_START_NS = 18_053_000_000_000
NS_PER_MS = 1_000_000


def forming_rows() -> list[list[Any]]:
    rows: list[list[Any]] = json.loads((FIXTURES / "btcusdt_5m_forming_raw.json").read_text())
    return rows


def closed_rows() -> list[list[Any]]:
    rows: list[list[Any]] = json.loads((FIXTURES / "btcusdt_5m_closed_raw.json").read_text())
    return rows


class FakeClock:
    def __init__(self) -> None:
        self._mono_ns = MONO_START_NS
        self._wall_ms = WALL_START_MS

    def advance(self, ms: int) -> None:
        self._mono_ns += ms * NS_PER_MS
        self._wall_ms += ms

    def monotonic_ns(self) -> int:
        return self._mono_ns

    def wall_ms(self) -> int:
        return self._wall_ms


class FakeSource:
    def __init__(
        self,
        clock: FakeClock,
        rows: Sequence[object],
        server_time_ms: int = FORMING_SERVER_TIME_MS,
        rtt_ms: int = 270,
        time_error: Exception | None = None,
        klines_error: Exception | None = None,
        on_klines: Callable[[], None] | None = None,
    ) -> None:
        self.clock = clock
        self.rows = rows
        self.server_time_ms = server_time_ms
        self.rtt_ms = rtt_ms
        self.time_error = time_error
        self.klines_error = klines_error
        self.on_klines = on_klines
        self.time_calls = 0
        self.kline_calls: list[tuple[str, Timeframe, int]] = []
        self.closed = False

    async def fetch_server_time_ms(self) -> int:
        self.time_calls += 1
        self.clock.advance(self.rtt_ms)
        if self.time_error is not None:
            raise self.time_error
        return self.server_time_ms

    async def fetch_klines(self, symbol: str, timeframe: Timeframe, limit: int) -> Sequence[object]:
        self.kline_calls.append((symbol, timeframe, limit))
        if self.on_klines is not None:
            self.on_klines()
        if self.klines_error is not None:
            raise self.klines_error
        return self.rows

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "market.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=AT)
    conn.close()
    return path


def config(limit: int = 100) -> FetchOnceConfig:
    return FetchOnceConfig(symbol=SYMBOL, timeframe=FIVE_MINUTES, limit=limit)


def run(source: FakeSource, db: Path, clock: FakeClock, cfg: FetchOnceConfig | None = None) -> FetchOnceReport:
    async def scenario() -> FetchOnceReport:
        writer = await AsyncCandleWriter.open(db)
        try:
            return await fetch_once(source, writer, cfg or config(), clock)
        finally:
            await writer.close()

    return asyncio.run(scenario())


def query(db: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def stored_open_times(db: Path) -> list[int]:
    return [row[0] for row in query(db, "SELECT open_time_ms FROM candles ORDER BY open_time_ms")]


def test_closed_candles_are_stored_and_the_forming_one_is_not(db: Path) -> None:
    # INV-007, trusted case: the last row of the response is still forming and must never reach the table.
    clock = FakeClock()
    report = run(FakeSource(clock, forming_rows()), db, clock)

    assert report.fetched == 2
    assert len(report.selection.eligible) == 1
    assert len(report.selection.pending) == 1
    assert report.write is not None and report.write.inserted == 1
    assert stored_open_times(db) == [1792676700000]
    assert 1792677000000 not in stored_open_times(db)


@pytest.mark.parametrize(
    ("reason", "source_args"),
    [
        (TimeRejection.NO_OBSERVATION, {"time_error": SourceError("time endpoint down")}),
        (TimeRejection.RTT_TOO_LARGE, {"rtt_ms": 3000}),
    ],
    ids=["time_request_failed", "round_trip_too_slow"],
)
def test_untrusted_time_stores_nothing(db: Path, reason: TimeRejection, source_args: dict[str, Any]) -> None:
    clock = FakeClock()
    source = FakeSource(clock, closed_rows(), **source_args)  # every row would be eligible if time were trusted

    report = run(source, db, clock)

    assert stored_open_times(db) == []
    assert report.selection.untrusted_reason is reason
    assert len(report.selection.pending) == report.fetched == 100
    assert report.write is None
    assert len(source.kline_calls) == 1  # the selection stays the single gate (plan decision 2)


@pytest.mark.parametrize(
    ("margin_ms", "stored"), [(20, 0), (60, 1)], ids=["pending_at_send_time", "eligible_at_send_time"]
)
def test_eligibility_is_judged_at_the_request_time_not_after_the_response(
    db: Path, margin_ms: int, stored: int
) -> None:
    # The klines request is sent at the instant the time observation is received (age 0), so the bound is
    # S - 50 ms. The response then takes 10 s: a bound taken after it would confirm row 0 in both cases.
    clock = FakeClock()
    server_time = ROW0_END_MS + 15_000 + margin_ms
    source = FakeSource(
        clock, forming_rows(), server_time_ms=server_time, rtt_ms=100, on_klines=lambda: clock.advance(10_000)
    )

    run(source, db, clock)

    assert len(stored_open_times(db)) == stored


def test_malformed_batch_stores_nothing(db: Path) -> None:
    rows = closed_rows()
    rows[3][1] = 100.5  # a JSON number in a decimal-text slot
    clock = FakeClock()
    with pytest.raises(KlineParseError):
        run(FakeSource(clock, rows), db, clock)
    assert stored_open_times(db) == []


def test_source_failure_on_klines_propagates_and_stores_nothing(db: Path) -> None:
    clock = FakeClock()
    with pytest.raises(SourceError, match="klines down"):
        run(FakeSource(clock, closed_rows(), klines_error=SourceError("klines down")), db, clock)
    assert stored_open_times(db) == []


def test_an_unknown_write_outcome_propagates_unchanged(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unknown = CandleWriteOutcomeUnknown("outcome unknown")

    async def raise_unknown(self: AsyncCandleWriter, *args: object, **kwargs: object) -> WriteReport:
        raise unknown

    monkeypatch.setattr(AsyncCandleWriter, "write", raise_unknown)
    clock = FakeClock()
    with pytest.raises(CandleWriteOutcomeUnknown) as caught:
        run(FakeSource(clock, closed_rows()), db, clock)
    assert caught.value is unknown


def test_rerunning_is_idempotent_and_survives_a_restart(db: Path) -> None:
    clock = FakeClock()
    first = run(FakeSource(clock, closed_rows()), db, clock)
    before = query(db, "SELECT * FROM candles ORDER BY open_time_ms")

    second = run(FakeSource(clock, closed_rows()), db, clock)  # a new writer on the same file: a restart

    assert first.write is not None and second.write is not None
    assert (second.write.inserted, second.write.duplicates) == (0, first.write.inserted)
    assert query(db, "SELECT * FROM candles ORDER BY open_time_ms") == before


def test_a_revised_value_is_recorded_as_a_conflict_not_overwritten(db: Path) -> None:
    clock = FakeClock()
    run(FakeSource(clock, closed_rows()), db, clock)
    stored_close = query(db, "SELECT close FROM candles WHERE open_time_ms = ?".replace("?", str(closed_rows()[10][0])))

    revised = closed_rows()
    revised[10][4] = (
        revised[10][3] if revised[10][3] != revised[10][4] else revised[10][2]
    )  # a valid but different close
    assert revised[10][4] != closed_rows()[10][4]
    report = run(FakeSource(clock, revised), db, clock)

    assert report.write is not None and len(report.write.conflicts) == 1
    assert len(query(db, "SELECT * FROM candle_conflicts")) == 1
    assert (
        query(db, "SELECT close FROM candles WHERE open_time_ms = ?".replace("?", str(closed_rows()[10][0])))
        == stored_close
    )


def test_invalid_eligible_candle_is_reported_and_the_rest_is_stored(db: Path) -> None:
    rows = closed_rows()
    rows[5][2] = "1"  # high far below open, low and close
    clock = FakeClock()

    report = run(FakeSource(clock, rows), db, clock)

    assert len(report.selection.invalid) == 1
    assert report.write is not None and report.write.inserted == 99
    assert rows[5][0] not in stored_open_times(db)


def test_the_request_uses_the_configured_symbol_timeframe_and_limit(db: Path) -> None:
    clock = FakeClock()
    source = FakeSource(clock, forming_rows())
    run(source, db, clock, config(limit=2))
    assert source.kline_calls == [(SYMBOL, FIVE_MINUTES, 2)]


@pytest.mark.parametrize("limit", [0, MAX_LIMIT + 1, True, "5", None], ids=["zero", "too_large", "bool", "str", "none"])
def test_config_rejects_a_bad_limit(limit: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        FetchOnceConfig(symbol=SYMBOL, timeframe=FIVE_MINUTES, limit=untyped(limit))


@pytest.mark.parametrize("symbol", ["", 5, None], ids=["empty", "int", "none"])
def test_config_rejects_a_bad_symbol(symbol: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        FetchOnceConfig(symbol=untyped(symbol), timeframe=FIVE_MINUTES, limit=10)
