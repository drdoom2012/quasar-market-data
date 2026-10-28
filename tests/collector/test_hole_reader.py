import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.collector import hole_reader
from quasar_market_data.collector.hole_reader import (
    MAX_RUNS,
    PRESENT_SQL,
    HoleReadError,
    SeriesHoles,
    read_holes,
    read_present,
)
from quasar_market_data.domain.backfill import BackfillTarget
from quasar_market_data.storage.connection import StorageError, open_reader, open_writer
from tests.collector.statement_proxy import StatementProxy
from tests.collector.test_reconcile_reader import (
    AT,
    TF,
    TFMS,
    VENUE,
    A,
    B,
    mark_unavailable,
    put,
    slot,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "market.db"


def holes(db: Path, horizon: int = 0, symbol: str = A) -> SeriesHoles:
    return asyncio.run(read_holes(db, venue=VENUE, symbol=symbol, timeframe=TF, horizon_start_ms=slot(horizon)))


def present(db: Path, targets: list[BackfillTarget], symbol: str = A) -> tuple[frozenset[int], ...]:
    return asyncio.run(read_present(db, venue=VENUE, symbol=symbol, timeframe=TF, targets=targets))


def span(a: int, b: int) -> BackfillTarget:
    return BackfillTarget(slot(a), slot(b))


def test_interior_runs_are_newest_first(db: Path) -> None:
    put(db, [0, 1, 4, 5, 9, 10])
    assert holes(db) == SeriesHoles(slot(0), ((slot(6), slot(9)), (slot(2), slot(4))))


def test_interior_runs_are_capped(db: Path) -> None:
    put(db, list(range(0, 2 * (MAX_RUNS + 5), 2)))  # every other slot: one-slot holes
    result = holes(db)
    assert len(result.interior_runs) == MAX_RUNS
    newest = max(range(0, 2 * (MAX_RUNS + 5), 2))
    assert result.interior_runs[0] == (slot(newest - 1), slot(newest))


def test_runs_older_than_the_horizon_are_clipped_or_dropped(db: Path) -> None:
    put(db, [0, 5, 6, 12, 20])
    result = holes(db, horizon=8)
    assert result.interior_runs == ((slot(13), slot(20)), (slot(8), slot(12)))
    result = holes(db, horizon=3)
    assert result.interior_runs == ((slot(13), slot(20)), (slot(7), slot(12)), (slot(3), slot(5)))
    assert result.oldest_stored_ms == slot(0)


def test_conflicted_and_unavailable_slots_are_not_holes(db: Path) -> None:
    put(db, [0, 9])
    mark_unavailable(db, [3])
    conn = open_writer(db)
    conn.execute(
        "INSERT INTO candle_conflicts (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
        " first_seen_at_ms) VALUES (?, ?, ?, ?, '1', '1', '1', '1', '1', ?)",
        (VENUE, A, TF.label, slot(6), AT),
    )
    conn.close()
    assert holes(db).interior_runs == ((slot(7), slot(9)), (slot(4), slot(6)), (slot(1), slot(3)))


def test_an_empty_series_has_no_oldest_and_no_runs(db: Path) -> None:
    put(db, [0, 1, 5], symbol=B)
    assert holes(db) == SeriesHoles(None, ())


def test_the_reader_uses_a_connection_that_cannot_write(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(db, [0, 3])
    attempts: list[str] = []

    def opener(path: Path) -> StatementProxy:
        conn = open_reader(path)

        def before(sql: str, params: Any) -> None:
            if not attempts:
                with pytest.raises(sqlite3.OperationalError):
                    conn.execute(
                        "INSERT INTO unavailable_candles VALUES ('v', 's', '5m', 946684800000, 'EXCHANGE_NO_DATA', 1)"
                    )
                attempts.append("refused")

        return StatementProxy(conn, before)

    monkeypatch.setattr(hole_reader, "open_reader", opener)
    holes(db)
    assert attempts == ["refused"]


def test_a_missing_or_unmigrated_database_raises_a_storage_error(tmp_path: Path) -> None:
    unmigrated = tmp_path / "unmigrated.db"
    open_writer(unmigrated).close()
    for path in (unmigrated, tmp_path / "missing.db"):
        with pytest.raises(HoleReadError) as info:
            holes(path)
        assert isinstance(info.value, StorageError)
        with pytest.raises(HoleReadError):
            present(path, [span(0, 3)])
    assert not (tmp_path / "missing.db").exists()


def test_read_present_returns_the_stored_slots_per_target_and_ignores_conflict_and_unavailable_rows(db: Path) -> None:
    put(db, [0, 1, 2, 5, 8])
    mark_unavailable(db, [3])
    assert present(db, [span(0, 4), span(5, 9)]) == (
        frozenset({slot(0), slot(1), slot(2)}),
        frozenset({slot(5), slot(8)}),
    )


def test_read_present_excludes_other_symbols_venues_and_timeframes_and_the_exclusive_end(db: Path) -> None:
    put(db, [0, 1, 2, 3, 4])
    put(db, [1, 2], symbol=B)
    put(db, [1, 2], venue="other-venue")
    from quasar_market_data.domain.timeframe import Timeframe

    put(db, [6, 7, 8], timeframe=Timeframe("1m"))  # these open times fall inside the target
    result = present(db, [span(1, 3)])
    assert result == (frozenset({slot(1), slot(2)}),)  # not slot(0) (before), not slot(3) (the exclusive end)


def test_read_present_sees_one_snapshot(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(db, [0, 1])
    fired: list[bool] = []

    def opener(path: Path) -> StatementProxy:
        def before(sql: str, params: Any) -> None:
            if sql == PRESENT_SQL and params["start"] == slot(10) and not fired:
                fired.append(True)
                put(db, [2, 11])  # committed by another connection after the first target was read

        return StatementProxy(open_reader(path), before)

    monkeypatch.setattr(hole_reader, "open_reader", opener)
    first_two = present(db, [span(0, 5), span(10, 12)])
    assert fired == [True]
    assert first_two == (frozenset({slot(0), slot(1)}), frozenset())
    assert present(db, [span(0, 5)]) == (frozenset({slot(0), slot(1), slot(2)}),)


def test_read_present_uses_a_connection_that_cannot_write(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(db, [0])
    seen: list[bool] = []

    def opener(path: Path) -> StatementProxy:
        conn = open_reader(path)

        def before(sql: str, params: Any) -> None:
            if not seen:
                with pytest.raises(sqlite3.OperationalError):
                    conn.execute("DELETE FROM unavailable_candles")
                seen.append(True)

        return StatementProxy(conn, before)

    monkeypatch.setattr(hole_reader, "open_reader", opener)
    present(db, [span(0, 2)])
    assert seen == [True]


def test_read_present_is_a_primary_key_range_read(db: Path) -> None:
    put(db, [0, 1])
    conn = open_reader(db)
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + PRESENT_SQL,
            {"venue": VENUE, "symbol": A, "timeframe": TF.label, "start": slot(0), "end": slot(9)},
        ).fetchall()
    finally:
        conn.close()
    details = " ".join(row[3] for row in plan)
    assert "SEARCH" in details and "PRIMARY KEY" in details, details
    assert "SCAN" not in details and "TEMP B-TREE" not in details, details
    assert TFMS == 300_000
