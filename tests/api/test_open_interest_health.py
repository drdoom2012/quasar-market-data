"""The ``open_interest`` object of ``GET /health/ingest`` (contract api-v1 6.5.8, ADR-0003 P-8, P-11)."""

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.testclient import TestClient

from quasar_market_data.api.app import HEALTH_BEHIND_MAX, create_app
from quasar_market_data.api.health_reader import (
    _CONFLICT_PROBE,
    _NEWEST_OI,
    _NEXT_OI_SERIES,
    read_ingest_snapshot,
)
from quasar_market_data.collector.open_interest_writer import OpenInterestWriter
from quasar_market_data.config.settings import Instrument
from quasar_market_data.domain.open_interest import OpenInterest, OpenInterestKey
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from quasar_market_data.storage.migrate import migrate
from tests.api.probe import InterleavingConnection
from tests.api.seed import (
    SAMPLE_RULE,
    VENUE,
    insert_candle,
    insert_open_interest,
    insert_open_interest_conflict,
    make_database,
)
from tests.api.test_health_universe import AS_OF as GOLDEN_AS_OF
from tests.api.test_health_universe import GOLDEN, golden_database, s17_fields
from tests.api.test_open_interest_endpoint import contract_example
from tests.config_files import TEN_SYMBOLS

P5 = Timeframe("5m")
TF = 300_000
E = 500_000
L = 60_000
DAY = 86_400_000
RULE = OpenInterestRule(cadence=1, expected_after_ms=E, lag_threshold_ms=L, period=P5)
S = 1792643400000
WROTE_AT = 1_793_621_000_000
AS_OF = S + 1000 * TF  # aligned, so that AS_OF - 24 h is aligned too


def slot(n: int) -> int:
    return S + n * TF


def inst(symbol: str) -> Instrument:
    return Instrument(VENUE, symbol, "5m")


def client(path: Path, universe: Sequence[Instrument], as_of: int = AS_OF, **kwargs: Any) -> TestClient:
    kwargs.setdefault("open_interest", RULE)
    return TestClient(
        create_app(path, rule=SAMPLE_RULE, universe=universe, now_ms=lambda: as_of, **kwargs),
        raise_server_exceptions=False,
    )


def ingest(path: Path, universe: Sequence[Instrument], as_of: int = AS_OF, **kwargs: Any) -> dict[str, Any]:
    response = client(path, universe, as_of, **kwargs).get("/health/ingest")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def oi_object(path: Path, universe: Sequence[Instrument], as_of: int = AS_OF, **kwargs: Any) -> dict[str, Any]:
    found = ingest(path, universe, as_of, **kwargs)["open_interest"]
    assert isinstance(found, dict)
    return found


def seed(
    path: Path,
    rows: dict[tuple[str, str], int],
    conflicts: Sequence[tuple[str, int]] = (),
) -> Path:
    """``{(symbol, period): newest time_ms}``, one open interest row each, and conflict records ``(symbol, time_ms)``."""
    make_database(path)
    conn = open_writer(path)
    try:
        for (symbol, period), time_ms in rows.items():
            insert_open_interest(conn, time_ms, symbol=symbol, period=period)
        for symbol, time_ms in conflicts:
            insert_open_interest_conflict(conn, time_ms, 1, symbol=symbol)
    finally:
        conn.close()
    return path


def sym(i: int) -> str:
    return f"Q{i:03d}/USDT:USDT"


# --- the contract example -------------------------------------------------------------------------------------


def test_the_contract_example_is_what_the_server_returns(tmp_path: Path) -> None:
    example = contract_example("### 6.5.8 The `open_interest` object", fragment=True)["open_interest"]
    as_of = example["as_of_ms"]
    rows = {(symbol, "5m"): 1792677000000 for symbol in TEN_SYMBOLS}
    rows[("ETH/USDT:USDT", "5m")] = 1792676700000
    path = seed(tmp_path / "m.db", rows, conflicts=[("BTC/USDT:USDT", 1792676400000)])
    universe = tuple(inst(symbol) for symbol in TEN_SYMBOLS)

    found = oi_object(path, universe, as_of)

    assert found == example
    assert list(found) == list(example)
    assert list(found["behind"][0]) == list(example["behind"][0])


# --- the candle part is untouched; the key; null -------------------------------------------------------------


def test_open_interest_is_the_last_key_and_null_when_not_enabled(tmp_path: Path) -> None:
    path = golden_database(tmp_path / "m.db")

    body = ingest(path, (), GOLDEN_AS_OF, open_interest=None)

    assert list(body)[-4:] == ["truncated", "universe", "open_interest", "funding_rate"]
    assert body["funding_rate"] is None
    assert body["open_interest"] is None


@pytest.mark.parametrize("enabled", [False, True])
def test_the_candle_fields_are_the_golden_answer_with_or_without_open_interest(tmp_path: Path, enabled: bool) -> None:
    path = golden_database(tmp_path / "m.db")
    conn = open_writer(path)
    for i in range(5):
        insert_open_interest(conn, slot(900 + i), symbol=f"G{i:03d}/USDT:USDT")
    insert_open_interest_conflict(conn, slot(990), 1, symbol="G000/USDT:USDT")
    conn.close()

    body = ingest(path, (), GOLDEN_AS_OF, open_interest=RULE if enabled else None)

    assert s17_fields(body) == GOLDEN
    assert (body["open_interest"] is not None) is enabled


def test_a_disabled_answer_issues_no_open_interest_statement(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", {("BTC/USDT:USDT", "5m"): slot(990)})
    log: list[str] = []

    def traced(target: Path) -> sqlite3.Connection:
        conn = open_reader(target)
        conn.set_trace_callback(log.append)
        return conn

    ingest(path, (), open_interest=None, open_connection=traced)

    assert log and not any("open_interest" in statement for statement in log)


# --- counts and status --------------------------------------------------------------------------------------


def test_no_series(tmp_path: Path) -> None:
    found = oi_object(make_database(tmp_path / "m.db", candles=[slot(0)]), ())

    assert found["status"] == "no_series"
    assert (found["series_count"], found["current"], found["due"], found["stale"]) == (0, 0, 0, 0)
    assert (found["unjudged_series"], found["conflicted_series"], found["behind"], found["truncated"]) == (
        0,
        0,
        [],
        False,
    )
    assert found["scope"] == "stored_series" and found["as_of_ms"] == AS_OF
    assert found["rule"] == {"cadence": 1, "expected_after_ms": E, "lag_threshold_ms": L}


def test_current_catching_up_and_behind(tmp_path: Path) -> None:
    a, b = slot(100), slot(101)  # a is one slot behind b
    path = seed(tmp_path / "m.db", {(sym(0), "5m"): a, (sym(1), "5m"): b})
    x_a = a + TF + E  # when the slot after a's newest row is expected; then b's row is the newest expected slot

    only_b = oi_object(seed(tmp_path / "b.db", {(sym(1), "5m"): b}), (), x_a)
    due = oi_object(path, (), x_a)
    still_due = oi_object(path, (), x_a + L)
    stale = oi_object(path, (), x_a + L + 1)

    assert only_b["status"] == "current"
    assert (due["status"], due["current"], due["due"], due["stale"]) == ("catching_up", 1, 1, 0)
    assert (still_due["status"], still_due["due"]) == ("catching_up", 1)
    assert (stale["status"], stale["current"], stale["due"], stale["stale"]) == ("behind", 1, 0, 1)


@pytest.mark.parametrize("cadence", [1, 5])
def test_per_series_boundaries_in_exact_milliseconds(tmp_path: Path, cadence: int) -> None:
    expected_after = cadence * TF + 200_000
    rule = OpenInterestRule(cadence, expected_after, L, P5)
    newest = slot(100)
    path = seed(tmp_path / "m.db", {(sym(0), "5m"): newest})
    x = newest + TF + expected_after

    def judged(as_of: int) -> tuple[int, int, int, int]:
        found = oi_object(path, (inst(sym(0)),), as_of, open_interest=rule)
        lag = found["behind"][0]["lag_slots"] if found["behind"] else 0
        return found["current"], found["due"], found["stale"], lag

    assert judged(x - 1) == (1, 0, 0, 0)
    assert judged(x) == (0, 1, 0, 1)
    assert judged(x + L) == (0, 1, 0, 1)
    assert judged(x + L + 1) == (0, 0, 1, 1)
    assert judged(x + TF) == (0, 0, 1, 2)
    assert judged(newest - 10 * TF) == (1, 0, 0, 0)  # a clock behind the stored data


def test_a_retired_series_is_listed_as_not_configured_and_counted(tmp_path: Path) -> None:
    old = slot(100)
    path = seed(tmp_path / "m.db", {(sym(0), "5m"): old, (sym(1), "5m"): old})

    found = oi_object(path, (inst(sym(0)),), old + TF + E + L + 1)

    assert [(e["symbol"], e["configured"], e["state"]) for e in found["behind"]] == [
        (sym(0), True, "stale"),
        (sym(1), False, "stale"),
    ]
    assert found["status"] == "behind" and found["series_count"] == 2


def test_behind_is_ordered_stale_first_then_lag_and_cut_at_fifty(tmp_path: Path) -> None:
    rows = {(sym(i), "5m"): slot(900 - i // 3) for i in range(55)}  # ties in lag_slots
    rows[(sym(55), "5m")] = slot(998)  # due
    rows[(sym(56), "5m")] = slot(999)  # current
    path = seed(tmp_path / "m.db", rows)
    as_of = slot(999) + E + 20_000  # the newest expected slot is 999: sym(56) is current, sym(55) due, the rest stale

    found = oi_object(path, (), as_of)

    assert len(found["behind"]) == HEALTH_BEHIND_MAX and found["truncated"] is True
    assert (found["stale"], found["series_count"]) == (55, 57)
    keys = [(-e["lag_slots"], e["symbol"]) for e in found["behind"]]
    assert keys == sorted(keys) and all(e["state"] == "stale" for e in found["behind"])


# --- conflicted_series and degraded -------------------------------------------------------------------------------


def test_a_conflict_in_the_last_24_hours_makes_the_status_degraded_even_when_a_series_is_stale(tmp_path: Path) -> None:
    old = slot(100)
    path = seed(
        tmp_path / "m.db",
        {(sym(0), "5m"): old, (sym(1), "5m"): AS_OF},
        conflicts=[(sym(1), AS_OF - TF)],
    )

    found = oi_object(path, ())

    assert (found["stale"], found["conflicted_series"], found["status"]) == (1, 1, "degraded")


def test_the_24_hour_window_is_by_slot_and_exact(tmp_path: Path) -> None:
    edge = AS_OF - DAY  # aligned
    path = seed(
        tmp_path / "m.db",
        {(sym(0), "5m"): AS_OF, (sym(1), "5m"): AS_OF},
        conflicts=[(sym(0), edge), (sym(1), edge + TF)],
    )

    assert oi_object(path, ())["conflicted_series"] == 1  # edge + P counts, edge itself does not
    only_edge = seed(tmp_path / "e.db", {(sym(0), "5m"): AS_OF}, conflicts=[(sym(0), edge)])
    assert oi_object(only_edge, (), AS_OF)["conflicted_series"] == 0
    assert oi_object(only_edge, (), AS_OF - 1)["conflicted_series"] == 1  # one ms earlier the slot is inside the window


def test_a_conflict_of_an_older_slot_and_a_candle_conflict_do_not_count_and_two_conflicts_count_once(
    tmp_path: Path,
) -> None:
    path = seed(
        tmp_path / "m.db",
        {(sym(0), "5m"): AS_OF, (sym(1), "5m"): AS_OF},
        conflicts=[(sym(0), AS_OF - 3 * DAY)],
    )
    conn = open_writer(path)
    insert_candle(conn, AS_OF - TF, symbol=sym(1))
    conn.execute(
        "INSERT INTO candle_conflicts (venue, symbol, timeframe, open_time_ms, open, high, low, close, volume,"
        " turnover, first_seen_at_ms) VALUES (?, ?, '5m', ?, '1', '1', '1', '1', '1', NULL, 0)",
        (VENUE, sym(1), AS_OF - TF),
    )
    conn.close()

    assert oi_object(path, ())["conflicted_series"] == 0

    twice = seed(
        tmp_path / "t.db",
        {(sym(0), "5m"): AS_OF},
        conflicts=[(sym(0), AS_OF - TF), (sym(0), AS_OF - 2 * TF)],
    )
    assert oi_object(twice, ())["conflicted_series"] == 1


def test_a_conflict_written_by_the_collectors_writer_is_seen_by_health_and_the_endpoint(tmp_path: Path) -> None:
    """P-15 end to end: the writer's labels and the readers agree (a differing re-delivery of a stored row)."""
    path = tmp_path / "m.db"
    conn = open_writer(path)
    migrate(conn, applied_at_ms=WROTE_AT)
    writer = OpenInterestWriter(conn)
    key = OpenInterestKey(VENUE, sym(0), P5, AS_OF - TF)
    writer.write_transaction([OpenInterest(key, "100.5", "7000000")], ingested_at_ms=WROTE_AT)
    writer.write_transaction(
        [OpenInterest(key, "100.6", "7000001")], ingested_at_ms=WROTE_AT + 1
    )  # differs: a conflict
    writer.close()

    found = oi_object(path, ())
    rows = (
        client(path, ())
        .get(f"/v1/open-interest?venue={VENUE}&symbol={sym(0)}&period=5m&start={AS_OF - TF}&end={AS_OF}")
        .json()["rows"]
    )

    assert found["conflicted_series"] == 1 and found["status"] == "degraded"
    assert rows == [
        {"time_ms": AS_OF - TF, "open_interest": "100.5", "open_interest_value": "7000000", "conflicted": True}
    ]


# --- series that cannot be judged (OA9) ------------------------------------------------------------------------


def test_series_that_cannot_be_judged_are_counted_not_judged_and_never_a_500(tmp_path: Path) -> None:
    path = seed(
        tmp_path / "m.db",
        {(sym(0), "5m"): AS_OF, (sym(1), "15m"): AS_OF, (sym(2), "5m"): AS_OF - 1},  # another period; unaligned
    )
    candle_only = ingest(path, (), open_interest=None)

    body = ingest(path, ())
    found = body["open_interest"]

    assert (found["series_count"], found["current"], found["unjudged_series"], found["status"]) == (3, 1, 2, "degraded")
    assert found["behind"] == []
    assert s17_fields({**body, "open_interest": None}) == s17_fields(candle_only)


# --- one snapshot, bounded cost -------------------------------------------------------------------------------


def traced_log(path: Path, universe: Sequence[Instrument] = ()) -> list[str]:
    log: list[str] = []

    def traced(target: Path) -> sqlite3.Connection:
        conn = open_reader(target)
        conn.set_trace_callback(log.append)
        return conn

    ingest(path, universe, open_connection=traced)
    return log


def test_every_statement_runs_in_one_read_transaction(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", {(sym(0), "5m"): AS_OF}, conflicts=[(sym(0), AS_OF)])
    conn = open_writer(path)
    insert_candle(conn, slot(1), symbol=sym(0))
    conn.close()

    log = traced_log(path)

    texts = [statement.lstrip().upper() for statement in log]
    assert texts[0].startswith("BEGIN") and texts[-1].startswith("ROLLBACK")
    assert sum(text.startswith("BEGIN") for text in texts) == 1
    assert any("FROM CANDLES" in text for text in texts) and any("FROM OPEN_INTEREST" in text for text in texts)


@pytest.mark.parametrize("series", [2, 40])
def test_open_interest_statements_per_series_are_constant_whatever_the_rows_and_conflicts(
    tmp_path: Path, series: int
) -> None:
    path = seed(tmp_path / "m.db", {(sym(i), "5m"): AS_OF for i in range(series)})
    conn = open_writer(path)
    for n in range(300):  # many rows and conflicts in the first series: they must not add statements
        insert_open_interest(conn, slot(n), symbol=sym(0))
    for n in range(300):
        insert_open_interest_conflict(conn, slot(n), n, symbol=sym(0))
    conn.close()

    log = traced_log(path)

    assert sum("FROM open_interest_conflicts" in s for s in log) == series  # one probe per series
    assert sum("MAX(time_ms) FROM open_interest " in s for s in log) == series
    assert sum("FROM open_interest WHERE (venue" in s for s in log) == series + 1  # the walk, ending on no row


def test_statement_plans_search_by_index_and_never_scan(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", {(sym(0), "5m"): AS_OF}, conflicts=[(sym(0), AS_OF)])
    conn = open_reader(path)
    try:
        plans = [
            " ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", args).fetchall())
            for sql, args in (
                (_NEXT_OI_SERIES, ("", "", "", -1)),
                (_NEWEST_OI, ("a", "b", "c")),
                (_CONFLICT_PROBE, ("a", "b", "c", 0)),
            )
        ]
    finally:
        conn.close()

    assert (
        "SEARCH open_interest USING PRIMARY KEY" in plans[0] or "SEARCH open_interest USING COVERING INDEX" in plans[0]
    )
    assert (
        "SEARCH open_interest USING PRIMARY KEY" in plans[1] or "SEARCH open_interest USING COVERING INDEX" in plans[1]
    )
    assert "open_interest_conflicts_slot" in plans[2] and "SEARCH open_interest_conflicts" in plans[2]
    for plan in plans:
        assert "SCAN" not in plan, plan


def test_commits_between_the_statements_of_the_answer_are_not_in_it(tmp_path: Path) -> None:
    """One snapshot for the candle walk, the open interest walk and the conflict probes (contract 6.5.8)."""
    path = seed(tmp_path / "m.db", {(sym(0), "5m"): AS_OF})
    conn = open_writer(path)
    insert_candle(conn, slot(1), symbol=sym(0))
    conn.close()
    cutoff = AS_OF - DAY
    reference_conn = open_reader(path)
    try:
        reference = read_ingest_snapshot(reference_conn, cutoff)
    finally:
        reference_conn.close()
    writer = open_writer(path, busy_timeout_ms=1000)
    injected: list[str] = []

    def hook(n: int, sql: str) -> None:
        if n < 2:
            return
        insert_open_interest(writer, AS_OF, symbol=sym(10 + n))  # a new series sorting after the cursor
        insert_open_interest_conflict(
            writer, AS_OF - TF, n, symbol=sym(0)
        )  # a recent conflict in a series already read
        injected.append(sql)

    reader = open_reader(path)
    probe = InterleavingConnection(reader, hook)
    try:
        found = read_ingest_snapshot(cast(sqlite3.Connection, probe), cutoff)

        assert found == reference
        assert len(injected) >= 5
        assert any("FROM candles" in sql for sql in injected) and any(
            "FROM open_interest_conflicts" in sql for sql in injected
        )
        assert reader.in_transaction is False
        later = read_ingest_snapshot(reader, cutoff)
        assert later != reference  # the commits happened and a new snapshot sees them
        assert len(later[1].series) > len(reference[1].series) and later[1].conflicted == {(VENUE, sym(0), "5m")}
    finally:
        reader.close()
        writer.close()


def test_behind_orders_stale_before_due_then_lag_then_venue_symbol(tmp_path: Path) -> None:
    """With a threshold of two periods a series is ``due`` up to lag 2 and ``stale`` from lag 3 (stale lag >= due lag)."""
    rule = OpenInterestRule(1, E, 2 * TF + 10_000, P5)
    t_e = (AS_OF - E) // TF * TF
    path = make_database(tmp_path / "m.db")
    conn = open_writer(path)
    for k, symbol in ((1, "A-due1"), (2, "B-due2"), (3, "C-stale3"), (4, "D-stale4"), (4, "A-stale4")):
        insert_open_interest(conn, t_e - k * TF, symbol=symbol)
    # same lag, a venue that sorts first but a symbol that sorts last: the venue decides
    insert_open_interest(conn, t_e - 4 * TF, symbol="Z-stale4", venue="a-venue")
    conn.close()

    found = oi_object(path, (), open_interest=rule)

    assert [(e["venue"], e["symbol"], e["state"], e["lag_slots"]) for e in found["behind"]] == [
        ("a-venue", "Z-stale4", "stale", 4),
        (VENUE, "A-stale4", "stale", 4),
        (VENUE, "D-stale4", "stale", 4),
        (VENUE, "C-stale3", "stale", 3),
        (VENUE, "B-due2", "due", 2),
        (VENUE, "A-due1", "due", 1),
    ]


# --- unreadable databases ------------------------------------------------------------------------------------


def test_an_unreadable_database_is_still_a_503(tmp_path: Path) -> None:
    response = client(tmp_path / "missing.db", ()).get("/health/ingest")

    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"reason": "database_missing"}
    assert client(tmp_path / "missing.db", ()).get("/health").status_code == 200
