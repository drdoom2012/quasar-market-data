"""The ``funding_rate`` object of ``GET /health/ingest`` (contract api-v1 6.7.8, ADR-0004 P-13)."""

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from quasar_market_data.api.app import create_app
from quasar_market_data.api.health_reader import (
    _FUNDING_CONFLICT_PROBE,
    _NEWEST_FUNDING,
    _NEXT_FUNDING_SPAN,
)
from quasar_market_data.domain.funding_rate_coverage import FundingRateRule, behind_after_ms
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.storage.connection import open_reader, open_writer
from tests.api.seed import (
    SAMPLE_RULE,
    insert_funding_conflict,
    insert_funding_rate,
    make_database,
    set_funding_span,
)
from tests.api.test_funding_rate_endpoint import contract_example
from tests.api.test_health_universe import AS_OF as GOLDEN_AS_OF
from tests.api.test_health_universe import GOLDEN, golden_database, s17_fields

DAY = 86_400_000
H = 3_600_000
DELAY = 30_000
BACKFILL_DAYS = 365
RULE = FundingRateRule(publication_delay_ms=DELAY, backfill_days=BACKFILL_DAYS)
OI_RULE = OpenInterestRule(cadence=1, expected_after_ms=500_000, lag_threshold_ms=60_000, period=Timeframe("5m"))
AS_OF = 1_792_727_045_000
WIDTH = BACKFILL_DAYS * DAY


def sym(i: int) -> str:
    return f"F{i:03d}/USDT:USDT"


def client(path: Path, as_of: int = AS_OF, **kwargs: Any) -> TestClient:
    kwargs.setdefault("funding_rate", RULE)
    kwargs.setdefault("universe", ())
    return TestClient(
        create_app(path, rule=SAMPLE_RULE, now_ms=lambda: as_of, **kwargs),
        raise_server_exceptions=False,
    )


def ingest(path: Path, as_of: int = AS_OF, **kwargs: Any) -> dict[str, Any]:
    response = client(path, as_of, **kwargs).get("/health/ingest")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def funding(path: Path, as_of: int = AS_OF, **kwargs: Any) -> dict[str, Any]:
    found = ingest(path, as_of, **kwargs)["funding_rate"]
    assert isinstance(found, dict)
    return found


def seed(
    path: Path,
    series: dict[str, tuple[int, int, int | None]],
    conflicts: Sequence[tuple[str, int]] = (),
) -> Path:
    """``{symbol: (from_ms, through_ms, newest row time or None)}``, one span (and one row) each, conflicts ``(symbol, time)``."""
    make_database(path)
    conn = open_writer(path)
    try:
        for symbol, (from_ms, through_ms, newest) in series.items():
            set_funding_span(conn, from_ms, through_ms, symbol=symbol)
            if newest is not None:
                insert_funding_rate(conn, newest, symbol=symbol)
        for symbol, time_ms in conflicts:
            insert_funding_conflict(conn, time_ms, 1, symbol=symbol)
    finally:
        conn.close()
    return path


def current_series(
    i: int = 0, *, through: int = AS_OF - 60_000, width: int = WIDTH
) -> dict[str, tuple[int, int, int | None]]:
    return {sym(i): (through - width, through, AS_OF - 3 * H)}


# --- the contract example -----------------------------------------------------------------------------------------


def test_the_contract_example_is_what_the_server_returns(tmp_path: Path) -> None:
    example = contract_example("### 6.7.8 The `funding_rate` object", fragment=True)["funding_rate"]
    as_of = example["as_of_ms"]
    series: dict[str, tuple[int, int, int | None]] = {}
    for i in range(600):
        if i < 2:  # behind, exactly at the inclusive edge
            through = as_of - behind_after_ms(DELAY)
            width = WIDTH
        else:
            through = as_of - 60_000
            width = WIDTH - 1 if 2 <= i < 14 else WIDTH  # twelve series whose history is one millisecond short
        series[sym(i)] = (through - width, through, as_of - 3 * H)
    path = seed(tmp_path / "m.db", series, conflicts=[(sym(0), as_of - 2 * H)])

    found = funding(path, as_of)

    assert found == example
    assert list(found) == list(example)
    assert list(found["rule"]) == list(example["rule"])


# --- the candle part is untouched; the key; null ------------------------------------------------------------------


def test_funding_rate_is_the_last_key_and_null_when_not_enabled(tmp_path: Path) -> None:
    path = golden_database(tmp_path / "m.db")

    body = ingest(path, GOLDEN_AS_OF, funding_rate=None)

    assert list(body)[-4:] == ["truncated", "universe", "open_interest", "funding_rate"]
    assert body["funding_rate"] is None


def test_the_other_parts_of_the_body_are_identical_with_funding_enabled_or_not(tmp_path: Path) -> None:
    path = golden_database(tmp_path / "m.db")
    conn = open_writer(path)
    for i in range(4):
        set_funding_span(conn, AS_OF - WIDTH, AS_OF - 60_000, symbol=sym(i))
        insert_funding_rate(conn, AS_OF - 3 * H, symbol=sym(i))
    insert_funding_conflict(conn, AS_OF - H, 1, symbol=sym(0))
    conn.close()

    off = ingest(path, GOLDEN_AS_OF, funding_rate=None, open_interest=OI_RULE)
    on = ingest(path, GOLDEN_AS_OF, funding_rate=RULE, open_interest=OI_RULE)

    assert {k: v for k, v in on.items() if k != "funding_rate"} == {k: v for k, v in off.items() if k != "funding_rate"}
    assert s17_fields(on) == GOLDEN
    assert on["funding_rate"] is not None


def test_funding_enabled_with_open_interest_disabled_issues_no_open_interest_statement(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", current_series())
    log = traced_log(path)

    assert not any("open_interest" in statement for statement in log)
    body = ingest(path, open_interest=None)
    assert body["open_interest"] is None and body["funding_rate"]["series_count"] == 1


# --- counts and status ----------------------------------------------------------------------------------------------


def test_no_span_means_no_series_whatever_rows_exist(tmp_path: Path) -> None:
    path = make_database(tmp_path / "m.db", funding=[AS_OF - H], funding_conflicts=[AS_OF - H])  # rows, no span

    found = funding(path)

    assert (found["status"], found["series_count"], found["current"], found["quiet_series"]) == ("no_series", 0, 0, 0)
    assert found["conflicted_series"] == 0  # a series without a span is invisible, conflicts included


def test_all_current_is_current_and_the_counts_partition(tmp_path: Path) -> None:
    series = {**current_series(0), **current_series(1), **current_series(2)}
    found = funding(seed(tmp_path / "m.db", series))

    assert (found["status"], found["series_count"], found["current"], found["behind"]) == ("current", 3, 3, 0)
    assert (found["catching_up"], found["conflicted_series"], found["quiet_series"]) == (0, 0, 0)


def test_the_behind_edge_is_inclusive(tmp_path: Path) -> None:
    edge = AS_OF - behind_after_ms(DELAY)
    path = seed(tmp_path / "m.db", {**current_series(0, through=edge), **current_series(1, through=edge + 1)})

    found = funding(path)

    assert (found["behind"], found["current"], found["status"]) == (1, 1, "behind")
    assert found["series_count"] == found["current"] + found["behind"]


def test_catching_up_is_exact_at_the_configured_width_and_follows_no_clock(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", {**current_series(0, width=WIDTH), **current_series(1, width=WIDTH - 1)})

    found = funding(path)
    later = funding(path, AS_OF + 50_000)  # a later clock does not move it

    assert (found["catching_up"], found["status"]) == (1, "catching_up")
    assert later["catching_up"] == 1


def test_precedence_degraded_over_behind_over_catching_up_over_current(tmp_path: Path) -> None:
    behind = AS_OF - behind_after_ms(DELAY)
    base = {**current_series(0, width=WIDTH - 1), **current_series(1, through=behind)}  # catching up and behind
    assert funding(seed(tmp_path / "a.db", base))["status"] == "behind"
    assert funding(seed(tmp_path / "b.db", current_series(0, width=WIDTH - 1)))["status"] == "catching_up"
    degraded = funding(seed(tmp_path / "c.db", base, conflicts=[(sym(0), AS_OF - H)]))
    assert (degraded["status"], degraded["conflicted_series"]) == ("degraded", 1)


# --- quiet -----------------------------------------------------------------------------------------------------------


def test_quiet_after_exactly_48_hours_strictly_and_without_a_row(tmp_path: Path) -> None:
    through = AS_OF - 60_000
    span = (through - WIDTH, through)
    path = seed(
        tmp_path / "m.db",
        {
            sym(0): (*span, AS_OF - 2 * DAY),  # exactly 48 h: not quiet
            sym(1): (*span, AS_OF - 2 * DAY - 1),  # one millisecond more: quiet
            sym(2): (*span, None),  # no row at all: quiet
        },
    )

    found = funding(path)

    assert (found["quiet_series"], found["status"], found["series_count"]) == (2, "degraded", 3)
    assert found["rule"]["quiet_after_ms"] == 2 * DAY


# --- conflicts -------------------------------------------------------------------------------------------------------


def test_the_conflict_window_edge_is_the_slot_time_strictly_after_as_of_minus_24_hours(tmp_path: Path) -> None:
    series = {**current_series(0), **current_series(1), **current_series(2), **current_series(3)}
    path = seed(
        tmp_path / "m.db",
        series,
        conflicts=[
            (sym(0), AS_OF - DAY),  # exactly at the cut-off: not counted
            (sym(1), AS_OF - DAY + 1),  # one millisecond inside: counted
            (sym(2), AS_OF - 3 * DAY),  # old: not counted
            (sym(3), AS_OF - H),
            (sym(3), AS_OF - 2 * H),  # two records, one series
        ],
    )

    found = funding(path)

    assert found["conflicted_series"] == 2 and found["status"] == "degraded"


def test_a_candle_or_open_interest_conflict_never_counts(tmp_path: Path) -> None:
    from tests.api.seed import insert_conflict, insert_open_interest_conflict

    path = seed(tmp_path / "m.db", current_series())
    conn = open_writer(path)
    insert_conflict(conn, AS_OF - 300_000, 1)
    insert_open_interest_conflict(conn, AS_OF - 300_000, 1)
    conn.close()

    assert funding(path)["conflicted_series"] == 0


def test_a_conflict_of_a_series_with_rows_but_no_span_is_not_counted(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", current_series(0))  # sym(0) has a span; sym(1) below has rows and a conflict only
    conn = open_writer(path)
    insert_funding_rate(conn, AS_OF - H, symbol=sym(1))
    insert_funding_conflict(conn, AS_OF - H, 1, symbol=sym(1))
    conn.close()

    found = funding(path)

    assert (found["series_count"], found["conflicted_series"], found["status"]) == (1, 0, "current")


# --- shape ------------------------------------------------------------------------------------------------------------


def test_the_object_has_exactly_the_keys_of_the_contract_and_no_suspended_series(tmp_path: Path) -> None:
    found = funding(seed(tmp_path / "m.db", current_series()))

    assert list(found) == [
        "as_of_ms",
        "rule",
        "scope",
        "status",
        "series_count",
        "current",
        "behind",
        "catching_up",
        "conflicted_series",
        "quiet_series",
    ]
    assert list(found["rule"]) == ["publication_delay_ms", "behind_after_ms", "quiet_after_ms", "backfill_days"]
    assert found["scope"] == "stored_series" and found["as_of_ms"] == AS_OF
    assert "suspended_series" not in found


def test_the_api_uses_the_collectors_functions_for_behind_and_catching_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quasar_market_data.domain import funding_rate_coverage as coverage
    from quasar_market_data.domain import funding_rate_span

    calls: list[str] = []
    real_behind, real_pending = funding_rate_span.is_behind, funding_rate_span.history_pending

    def spy_behind(b_now_ms: int, span: Any) -> bool:
        calls.append(f"is_behind:{b_now_ms}")
        return real_behind(b_now_ms, span)

    def spy_pending(span: Any, backfill_days: int) -> bool:
        calls.append(f"history_pending:{backfill_days}")
        return real_pending(span, backfill_days)

    monkeypatch.setattr(coverage, "is_behind", spy_behind)
    monkeypatch.setattr(coverage, "history_pending", spy_pending)
    funding(seed(tmp_path / "m.db", current_series()))

    assert calls == [f"is_behind:{AS_OF - DELAY}", f"history_pending:{BACKFILL_DAYS}"]


def test_a_clock_too_close_to_the_minimum_time_fails_closed(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", {sym(0): (946_684_800_000, 946_684_800_001, None)})

    response = client(path, as_of=946_684_800_010).get("/health/ingest")

    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"


# --- one snapshot, bounded cost ---------------------------------------------------------------------------------------


def traced_log(path: Path, **kwargs: Any) -> list[str]:
    log: list[str] = []

    def traced(target: Path) -> sqlite3.Connection:
        conn = open_reader(target)
        conn.set_trace_callback(log.append)
        return conn

    ingest(path, open_connection=traced, **kwargs)
    return log


def test_every_statement_runs_in_one_read_transaction_with_candles_open_interest_and_funding(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", current_series(), conflicts=[(sym(0), AS_OF - H)])
    conn = open_writer(path)
    from tests.api.seed import insert_candle, insert_open_interest

    insert_candle(conn, 1_792_727_100_000 - 300_000 * 5)
    insert_open_interest(conn, 1_792_727_100_000 - 300_000 * 5)
    conn.close()

    log = traced_log(path, open_interest=OI_RULE)

    texts = [statement.lstrip().upper() for statement in log]
    assert texts[0].startswith("BEGIN") and texts[-1].startswith("ROLLBACK")
    assert sum(text.startswith("BEGIN") for text in texts) == 1
    for table in ("FROM CANDLES", "FROM OPEN_INTEREST", "FROM FUNDING_RATE_VERIFIED", "FROM FUNDING_RATES"):
        assert any(table in text for text in texts), table


@pytest.mark.parametrize("series", [2, 40])
def test_funding_statements_per_series_are_constant_whatever_the_rows_and_conflicts(
    tmp_path: Path, series: int
) -> None:
    path = seed(tmp_path / "m.db", {sym(i): (AS_OF - WIDTH, AS_OF - 60_000, AS_OF - H) for i in range(series)})
    conn = open_writer(path)
    for n in range(300):  # many rows and conflicts in the first series: they must not add statements
        insert_funding_rate(conn, AS_OF - 2 * H - n, symbol=sym(0))
        insert_funding_conflict(conn, AS_OF - 2 * H - n, n, symbol=sym(0))
    conn.close()

    log = traced_log(path)

    assert sum("FROM funding_rate_conflicts" in s for s in log) == series  # one probe per series
    assert sum("MAX(funding_time_ms) FROM funding_rates " in s for s in log) == series
    assert sum("FROM funding_rate_verified WHERE (venue" in s for s in log) == series + 1  # the walk, ending on no row


def test_statement_plans_search_by_index_and_never_scan(tmp_path: Path) -> None:
    path = seed(tmp_path / "m.db", current_series(), conflicts=[(sym(0), AS_OF - H)])
    conn = open_reader(path)
    try:
        plans = [
            " ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", args).fetchall())
            for sql, args in (
                (_NEXT_FUNDING_SPAN, ("", "")),
                (_NEWEST_FUNDING, ("a", "b")),
                (_FUNDING_CONFLICT_PROBE, ("a", "b", 0)),
            )
        ]
    finally:
        conn.close()

    assert "SEARCH funding_rate_verified USING" in plans[0]
    assert "SEARCH funding_rates USING" in plans[1]
    assert "SEARCH funding_rate_conflicts USING" in plans[2]  # either of the two indexes the planner may choose
    for plan in plans:
        assert "SCAN" not in plan, plan


def test_a_commit_after_the_read_started_is_not_in_the_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from quasar_market_data.api import health_reader

    path = seed(tmp_path / "m.db", current_series())
    real = health_reader._walk_funding_series
    committed: list[int] = []

    def walk(conn: sqlite3.Connection, conflicts_after_ms: int) -> Any:
        if not committed:  # only during the first request
            writer = open_writer(path, busy_timeout_ms=1000)
            set_funding_span(writer, AS_OF - WIDTH, AS_OF - 60_000, symbol=sym(9))
            committed.append(1)
            writer.close()
        return real(conn, conflicts_after_ms)

    monkeypatch.setattr(health_reader, "_walk_funding_series", walk)

    found = funding(path)

    assert committed == [1]  # the commit really happened during the request ...
    assert found["series_count"] == 1  # ... and the snapshot does not contain it
    assert funding(path)["series_count"] == 2  # a later request does


def test_a_missing_or_mismatched_database_is_a_503_and_the_probes_are_unchanged(tmp_path: Path) -> None:
    response = client(tmp_path / "missing.db").get("/health/ingest")

    assert response.status_code == 503 and response.json()["error"]["code"] == "not_ready"
    assert client(tmp_path / "missing.db").get("/health").status_code == 200
