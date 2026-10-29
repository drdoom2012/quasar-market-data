"""``/health/ingest`` since S20: the S17 fields are independent of the universe; the ``universe`` block."""

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from quasar_market_data.api import database
from quasar_market_data.api.app import HEALTH_MISSING_MAX, create_app
from quasar_market_data.api.health_reader import StoredSeries, read_stored_series
from quasar_market_data.config.settings import Instrument
from quasar_market_data.storage.connection import open_reader, open_writer
from tests.api.seed import SAMPLE_RULE, VENUE, insert_candle, make_database

TF = 300_000
S = 1792643400000
AS_OF = S + 1000 * TF + 20_000
GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "health_ingest_s17.json"
GOLDEN: dict[str, Any] = json.loads(GOLDEN_PATH.read_text())
S17_KEYS = ("as_of_ms", "rule", "scope", "status", "series_count", "current", "due", "stale", "behind", "truncated")


def slot(n: int) -> int:
    return S + n * TF


def inst(symbol: str, timeframe: str = "5m", venue: str = VENUE) -> Instrument:
    return Instrument(venue, symbol, timeframe)


def golden_symbol(i: int) -> str:
    return f"G{i:03d}/USDT:USDT"


def s17_fields(body: dict[str, Any]) -> dict[str, Any]:
    out = {k: body[k] for k in S17_KEYS}
    out["behind"] = [{k: v for k, v in e.items() if k != "configured"} for e in body["behind"]]
    return out


def assert_equalities(body: dict[str, Any]) -> None:
    u = body["universe"]
    assert body["series_count"] == u["configured_stored"] + u["unconfigured_stored"]
    assert u["configured_count"] == u["configured_stored"] + u["missing_count"]
    assert len(u["missing"]) == min(u["missing_count"], HEALTH_MISSING_MAX)
    assert u["missing_truncated"] == (u["missing_count"] > HEALTH_MISSING_MAX)


def golden_database(path: Path) -> Path:
    """55 stale series, one due, one current (5m, ``G000``..), and one ``1m`` row of ``G000``, at ``AS_OF``."""
    make_database(path)
    conn = open_writer(path)
    try:
        for i in range(55):
            insert_candle(conn, slot(900 - i // 3), symbol=golden_symbol(i))  # ties in lag_slots
        insert_candle(conn, slot(998), symbol=golden_symbol(55))  # due
        insert_candle(conn, slot(999), symbol=golden_symbol(56))  # current
        insert_candle(conn, S + 990 * TF - 60_000, symbol=golden_symbol(0), timeframe="1m")
    finally:
        conn.close()
    return path


GOLDEN_SUBSET = tuple(inst(golden_symbol(i)) for i in (0, 3, 55, 56)) + (inst(golden_symbol(0), "1m"),)
MISSING_60 = tuple(inst(f"M{i:03d}/USDT:USDT") for i in range(60))


def client(path: Path, universe: Sequence[Instrument], as_of: int = AS_OF, **kwargs: Any) -> TestClient:
    return TestClient(
        create_app(path, rule=SAMPLE_RULE, universe=universe, now_ms=lambda: as_of, **kwargs),
        raise_server_exceptions=False,
    )


def ingest(path: Path, as_of: int, universe: Sequence[Instrument], **kwargs: Any) -> dict[str, Any]:
    response = client(path, universe, as_of, **kwargs).get("/health/ingest")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def seed(path: Path, rows: dict[tuple[str, str], int]) -> Path:
    """``{(symbol, timeframe): newest slot index}``, one row each."""
    make_database(path)
    conn = open_writer(path)
    try:
        for (symbol, timeframe), n in rows.items():
            insert_candle(conn, slot(n), symbol=symbol, timeframe=timeframe)
    finally:
        conn.close()
    return path


@pytest.mark.parametrize("universe", [(), GOLDEN_SUBSET, GOLDEN_SUBSET + MISSING_60])
def test_s17_fields_equal_the_golden_answer_whatever_the_universe(
    tmp_path: Path, universe: tuple[Instrument, ...]
) -> None:
    # INV-066
    body = ingest(golden_database(tmp_path / "m.db"), AS_OF, universe)

    assert s17_fields(body) == GOLDEN
    assert_equalities(body)


def test_both_equalities_hold_on_every_fixture(tmp_path: Path) -> None:
    golden = golden_database(tmp_path / "golden.db")
    small = seed(tmp_path / "small.db", {("A", "5m"): 995, ("B", "5m"): 999, ("X", "1m"): 995})
    empty = make_database(tmp_path / "empty.db")
    cases: list[tuple[Path, Sequence[Instrument]]] = [
        (golden, ()),
        (golden, GOLDEN_SUBSET),
        (golden, GOLDEN_SUBSET + MISSING_60),
        (small, (inst("A"), inst("B"), inst("C"), inst("X", "5m"))),
        (small, tuple(inst(f"M{i}") for i in range(51))),
        (empty, MISSING_60),
        (empty, ()),
    ]
    for path, universe in cases:
        assert_equalities(ingest(path, AS_OF, universe))


def test_a_configured_instrument_with_no_row_is_missing_and_status_is_unchanged(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", {("A", "5m"): 999})
    reference = ingest(path, AS_OF, (inst("A"),))

    body = ingest(path, AS_OF, (inst("A"), inst("LTC")))

    assert reference["status"] == "current"
    assert body["status"] == "current"
    assert s17_fields(body) == s17_fields(reference)
    assert body["universe"] == {
        "configured_count": 2,
        "configured_stored": 1,
        "unconfigured_stored": 0,
        "missing_count": 1,
        "missing": [{"venue": VENUE, "symbol": "LTC", "timeframe": "5m"}],
        "missing_truncated": False,
    }


def test_a_configured_series_with_an_old_row_is_not_missing_and_stays_stale(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", {("OLD", "5m"): 10})

    body = ingest(path, AS_OF, (inst("OLD"),))

    assert body["status"] == "behind"
    assert body["stale"] == 1
    assert body["universe"]["missing"] == []
    assert body["universe"]["missing_count"] == 0
    assert body["universe"]["configured_stored"] == 1


def test_a_retired_stale_series_stays_in_behind_marked_not_configured(tmp_path: Path) -> None:
    # INV-067
    path = seed(tmp_path / "m.db", {("RETIRED", "5m"): 10, ("LIVE", "5m"): 999})

    body = ingest(path, AS_OF, (inst("LIVE"),))

    assert body["status"] == "behind"
    assert body["series_count"] == 2
    assert body["behind"][0] == {
        "venue": VENUE,
        "symbol": "RETIRED",
        "timeframe": "5m",
        "newest_open_time_ms": slot(10),
        "lag_slots": body["behind"][0]["lag_slots"],
        "state": "stale",
        "configured": False,
    }
    assert body["universe"]["unconfigured_stored"] == 1
    assert body["universe"]["configured_stored"] == 1
    assert_equalities(body)


def test_configured_flag_on_behind_entries(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", {("A", "5m"): 10, ("B", "5m"): 10, ("C", "5m"): 998, ("D", "1m"): 10})

    body = ingest(path, AS_OF, (inst("A"), inst("C"), inst("D", "5m")))

    flags = {(e["symbol"], e["timeframe"]): e["configured"] for e in body["behind"]}
    assert flags == {("A", "5m"): True, ("B", "5m"): False, ("C", "5m"): True, ("D", "1m"): False}
    assert all(list(e)[-1] == "configured" and isinstance(e["configured"], bool) for e in body["behind"])


def test_the_universe_block_follows_truncated_with_the_keys_of_the_contract(tmp_path: Path) -> None:
    body = ingest(seed(tmp_path / "m.db", {("A", "5m"): 999}), AS_OF, (inst("A"),))

    assert list(body)[-2:] == ["truncated", "universe"]
    assert list(body["universe"]) == [
        "configured_count",
        "configured_stored",
        "unconfigured_stored",
        "missing_count",
        "missing",
        "missing_truncated",
    ]


@pytest.mark.parametrize(("count", "truncated"), [(50, False), (51, True), (60, True)])
def test_missing_is_capped_with_a_complete_count(tmp_path: Path, count: int, truncated: bool) -> None:
    path = seed(tmp_path / "m.db", {("A", "5m"): 999})

    body = ingest(path, AS_OF, (inst("A"),) + MISSING_60[:count])

    u = body["universe"]
    assert HEALTH_MISSING_MAX == 50
    assert len(u["missing"]) == min(count, 50)
    assert u["missing_count"] == count
    assert u["missing_truncated"] is truncated
    assert_equalities(body)


def test_all_configured_missing_is_no_series(tmp_path: Path) -> None:
    body = ingest(make_database(tmp_path / "m.db"), AS_OF, MISSING_60[:5])

    assert body["status"] == "no_series"
    assert body["series_count"] == 0
    assert body["universe"]["missing_count"] == body["universe"]["configured_count"] == 5
    assert body["universe"]["configured_stored"] == 0
    assert body["behind"] == []


def test_missing_is_ordered_by_triple(tmp_path: Path) -> None:
    universe = (
        inst("B", "5m"),
        inst("A", "5m", venue="zeta"),
        inst("A", "5m"),
        inst("B", "15m"),
        inst("A", "1m"),
    )

    body = ingest(make_database(tmp_path / "m.db"), AS_OF, universe)

    got = [(m["venue"], m["symbol"], m["timeframe"]) for m in body["universe"]["missing"]]
    assert got == sorted(got)
    assert got == [(VENUE, "A", "1m"), (VENUE, "A", "5m"), (VENUE, "B", "15m"), (VENUE, "B", "5m"), ("zeta", "A", "5m")]


def test_another_timeframe_of_a_configured_symbol_is_unconfigured(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", {("BTC", "1m"): 999})

    body = ingest(path, AS_OF, (inst("BTC", "5m"),))

    assert body["universe"]["missing"] == [{"venue": VENUE, "symbol": "BTC", "timeframe": "5m"}]
    assert body["universe"]["unconfigured_stored"] == 1
    assert body["universe"]["configured_stored"] == 0
    assert_equalities(body)


def test_503_when_the_database_is_not_ready_with_a_universe(tmp_path: Path) -> None:
    response = client(tmp_path / "absent.db", MISSING_60[:3]).get("/health/ingest")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_ready"
    assert response.json()["error"]["details"] == {"reason": "database_missing"}


class CommitDuringRead:
    """Connection proxy (pattern of ``test_health_reader.WriterAfterNthSeries``, plus ``close``): after the
    ``after``-th next-series statement, a separate writer connection commits while the read transaction is open."""

    def __init__(self, real: sqlite3.Connection, writer_path: Path, after: int) -> None:
        self.real = real
        self.writer_path = writer_path
        self.after = after
        self.next_reads = 0
        self.commits = 0
        self.began = False
        self.begin_open_at_commit = False

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        cursor = self.real.execute(sql, params)
        if sql == "BEGIN":
            self.began = True
        elif sql == "ROLLBACK":
            self.began = False
        if "ORDER BY venue" in sql:
            self.next_reads += 1
            if self.next_reads == self.after:
                self.begin_open_at_commit = self.began
                commit_new_data(self.writer_path)
                self.commits += 1
        return cursor

    def close(self) -> None:
        self.real.close()


def commit_new_data(path: Path) -> None:
    writer = open_writer(path)
    try:
        insert_candle(writer, slot(999), symbol="M")  # the first row of a missing configured series
        insert_candle(writer, slot(999), symbol="A")  # a current row for a series already read
        insert_candle(writer, slot(999), symbol="Z")  # a current row for a series not yet read
    finally:
        writer.close()


COMMIT_UNIVERSE = (inst("A"), inst("M"), inst("Z"))


def commit_fixture(path: Path) -> Path:
    return seed(path, {("A", "5m"): 10, ("Z", "5m"): 10, ("X", "5m"): 10})  # X is stored and not configured


def test_a_commit_inside_the_open_read_transaction_changes_neither_s17_fields_nor_universe(tmp_path: Path) -> None:
    path = commit_fixture(tmp_path / "m.db")
    reference = ingest(path, AS_OF, COMMIT_UNIVERSE)
    assert reference["universe"]["missing"] == [{"venue": VENUE, "symbol": "M", "timeframe": "5m"}]
    proxies: list[CommitDuringRead] = []

    def open_connection(p: Path) -> Any:
        proxy = CommitDuringRead(open_reader(p), p, after=2)
        proxies.append(proxy)
        return proxy

    body = ingest(path, AS_OF, COMMIT_UNIVERSE, open_connection=open_connection)

    assert [p.commits for p in proxies] == [1]
    assert proxies[0].begin_open_at_commit
    assert s17_fields(body) == s17_fields(reference)
    assert body["universe"] == reference["universe"]
    assert_equalities(body)
    fresh = ingest(path, AS_OF, COMMIT_UNIVERSE)
    assert fresh["universe"]["missing"] == []
    assert fresh["universe"]["configured_stored"] == reference["universe"]["configured_stored"] + 1
    assert fresh["current"] == 3  # A, Z and the new M
    assert fresh["series_count"] == reference["series_count"] + 1
    assert_equalities(fresh)


def test_a_commit_after_the_read_finished_is_not_in_the_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = commit_fixture(tmp_path / "m.db")
    reference = ingest(path, AS_OF, COMMIT_UNIVERSE)

    def read_then_commit(conn: sqlite3.Connection) -> tuple[StoredSeries, ...]:
        result = read_stored_series(conn)
        commit_new_data(path)
        return result

    monkeypatch.setattr(database, "read_stored_series", read_then_commit)

    body = ingest(path, AS_OF, COMMIT_UNIVERSE)

    assert body == reference
    assert body["universe"]["missing"] == [{"venue": VENUE, "symbol": "M", "timeframe": "5m"}]
    assert_equalities(body)
