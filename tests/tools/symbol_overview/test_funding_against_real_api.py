"""The tool with funding against the real API application on a seeded temporary database (FR-6).

The guard against drift between the fakes of the other tests and the contract: the real answers of
``GET /v1/funding-rates`` must pass the tool's validation (no symbol may come out ``unknown``) and be judged as
designed. It also holds the page's three comparisons (a span against the asked window, a settlement against the
API's quiet rule) to the API's own ``is_behind`` and quiet rule at their boundaries, and to the counts of
``/health/ingest``. The API's clock is fixed, so every figure is exact. The expected states are written out here, not
computed by the tool.
"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import uvicorn

from quasar_market_data.api.app import create_app
from quasar_market_data.config.settings import Instrument
from quasar_market_data.domain.funding_rate_coverage import FundingRateRule
from quasar_market_data.storage.connection import open_writer
from tests.api.seed import (
    AT,
    SAMPLE_RULE,
    insert_candle,
    insert_funding_conflict,
    insert_funding_rate,
    make_database,
    set_funding_span,
)
from tools.symbol_overview.fetch import Fetch, RawResponse, http_fetch
from tools.symbol_overview.model import DAY_MS, TF_MS
from tools.symbol_overview.refresh import Overviewer, Snapshot
from tools.symbol_overview.window import window_for

DELAY_MS = 30_000
BEHIND_AFTER_MS = DELAY_MS + 7_200_000
QUIET_AFTER_MS = 172_800_000
S = window_for(AT).start
END = AT - BEHIND_AFTER_MS + 1  # the end of the funding window the tool asks for (design 4.1)
FUNDING_PATH = "/v1/funding-rates"

OK, EDGE, BEHIND, SHALLOW, CONFLICT, QUIET_ROW, QUIET_EDGE, QUIET_NONE, UNVERIFIED, NODATA = (
    "OK/USDT:USDT",
    "EDGE/USDT:USDT",
    "BEHIND/USDT:USDT",
    "SHALLOW/USDT:USDT",
    "CONFLICT/USDT:USDT",
    "QROW/USDT:USDT",
    "QEDGE/USDT:USDT",
    "QNONE/USDT:USDT",
    "UNVER/USDT:USDT",
    "NODATA/USDT:USDT",
)
SYMBOLS = (OK, EDGE, BEHIND, SHALLOW, CONFLICT, QUIET_ROW, QUIET_EDGE, QUIET_NONE, UNVERIFIED, NODATA)


def universe() -> list[Instrument]:
    return [Instrument("binance-usdm", symbol, "5m") for symbol in SYMBOLS]


@contextmanager
def serve(db_path: Path, *, funding: FundingRateRule | None) -> Iterator[str]:
    app = create_app(db_path, rule=SAMPLE_RULE, universe=universe(), now_ms=lambda: AT, funding_rate=funding)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the API did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def rows_for(newest: int) -> list[int]:
    return [newest - 16 * 3_600_000, newest - 8 * 3_600_000, newest]  # settlements a few hours apart


def seed(path: Path) -> None:
    make_database(path)
    conn = open_writer(path)
    try:
        conn.execute("BEGIN")
        for symbol in SYMBOLS:
            for open_time_ms in range(S, AT - 2 * TF_MS + 1, TF_MS):
                insert_candle(conn, open_time_ms, symbol=symbol)

        def series(symbol: str, span: tuple[int, int] | None, newest: int | None, conflict: bool = False) -> None:
            for time_ms in rows_for(newest) if newest is not None else ():
                insert_funding_rate(conn, time_ms, symbol=symbol)
            if conflict and newest is not None:
                insert_funding_conflict(conn, newest, 0, symbol=symbol)
            if span is not None:
                set_funding_span(conn, *span, symbol=symbol)

        recent = END - 100  # the newest settlement of a healthy series: inside the span, well inside 48 hours
        series(OK, (S - DAY_MS, END + 500), recent)  # through a little past `end`: not behind
        series(EDGE, (S, END), recent)  # from == start and through == end: both boundaries are inclusive
        series(BEHIND, (S - DAY_MS, END - 1), recent - 1000)  # one millisecond short: behind by the API's rule
        series(SHALLOW, (S + 1, END + 3_600_000), recent)  # one millisecond short at the start
        series(CONFLICT, (S - DAY_MS, END + 500), recent, conflict=True)
        series(QUIET_ROW, (S - DAY_MS, END + 500), AT - QUIET_AFTER_MS - 1)  # the API's strict rule: one past is quiet
        series(QUIET_EDGE, (S - DAY_MS, END + 500), AT - QUIET_AFTER_MS)  # == is not quiet
        series(QUIET_NONE, (S - DAY_MS, END + 500), None)  # a span without a row: quiet and, by the API, complete
        series(UNVERIFIED, None, recent)  # rows without a span (a crash before the first merge)
        conn.execute("COMMIT")
    finally:
        conn.close()


def body_of(snapshot: Snapshot) -> dict[str, Any]:
    return cast(dict[str, Any], snapshot.body)


def by_symbol(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    return {s["symbol"]: s for s in body_of(snapshot)["symbols"]}


def recording(url: str) -> tuple[Fetch, list[str]]:
    real = http_fetch(url)
    paths: list[str] = []

    def fetch(path: str) -> RawResponse:
        paths.append(path.partition("?")[0])
        return real(path)

    return fetch, paths


RULE = FundingRateRule(publication_delay_ms=DELAY_MS, backfill_days=30)

EXPECTED_STATES = {
    OK: "ok",
    EDGE: "ok",
    BEHIND: "behind",
    SHALLOW: "partly_verified",
    CONFLICT: "conflict",
    QUIET_ROW: "quiet",
    QUIET_EDGE: "ok",
    QUIET_NONE: "quiet",
    UNVERIFIED: "unverified",
    NODATA: "no_data",
}


def test_the_real_answers_pass_validation_and_are_judged_as_designed(tmp_path: Path) -> None:
    db = tmp_path / "quasar.db"
    seed(db)
    with serve(db, funding=RULE) as url:
        fetch, _ = recording(url)
        snapshot = Overviewer(fetch).get()

    assert snapshot.status == 200
    body = body_of(snapshot)
    block = body["funding_rate"]
    assert block["status"] == "enabled"
    assert block["window"] == {"start": S, "end": END, "clamped": False}
    symbols = by_symbol(snapshot)
    assert {s: v["state"] for s, v in symbols.items() if s != NODATA} == dict.fromkeys(SYMBOLS[:-1], "ok")  # candles
    assert {s: v["funding_rate"]["state"] for s, v in symbols.items()} == EXPECTED_STATES  # nothing is "unknown"
    summary = block["summary"]
    assert summary == {
        "total": 10,
        "no_data": 1,
        "unknown": 0,
        "conflict": 1,
        "quiet": 2,
        "behind": 1,
        "unverified": 1,
        "partly_verified": 1,
        "ok": 3,
    }

    ok = symbols[OK]["funding_rate"]
    assert ok["reasons"] == [] and ok["complete"] is True and ok["window_state"] == "verified"
    assert (ok["verified_from_ms"], ok["verified_through_ms"]) == (S - DAY_MS, END + 500)
    assert ok["newest_funding_time_ms"] == END - 100 and ok["events_in_window"] == 3
    edge = symbols[EDGE]["funding_rate"]
    assert edge["window_state"] == "verified" and edge["reasons"] == []
    behind = symbols[BEHIND]["funding_rate"]
    assert [r["code"] for r in behind["reasons"]] == ["behind"] and behind["window_state"] == "partly_verified"
    shallow = symbols[SHALLOW]["funding_rate"]
    assert [r["code"] for r in shallow["reasons"]] == ["partly_verified"]
    conflict = symbols[CONFLICT]["funding_rate"]
    assert (
        conflict["reasons"] == [{"code": "conflict", "slots": 1, "runs": 0, "detail": ""}] and not conflict["complete"]
    )
    assert [r["code"] for r in symbols[QUIET_ROW]["funding_rate"]["reasons"]] == ["quiet"]
    none = symbols[QUIET_NONE]["funding_rate"]
    assert [r["code"] for r in none["reasons"]] == ["quiet"] and none["newest_funding_time_ms"] is None
    assert none["complete"] is True  # the API's statement stays visible next to the page's `quiet`
    unverified = symbols[UNVERIFIED]["funding_rate"]
    assert [r["code"] for r in unverified["reasons"]] == ["unverified"] and unverified["verified_from_ms"] is None
    assert symbols[NODATA]["funding_rate"]["reasons"] == [{"code": "no_data", "slots": 0, "runs": 0, "detail": ""}]


def test_the_pages_behind_and_quiet_counts_equal_the_apis_own_health_counts(tmp_path: Path) -> None:
    db = tmp_path / "quasar.db"
    seed(db)
    with serve(db, funding=RULE) as url:
        snapshot = Overviewer(http_fetch(url)).get()

    block = body_of(snapshot)["funding_rate"]
    health = block["health"]
    # the API counts series that have a verified span: BEHIND is its only behind series; QUIET_ROW and QUIET_NONE its quiet
    assert health["behind"] == block["summary"]["behind"] == 1
    assert health["quiet_series"] == block["summary"]["quiet"] == 2
    assert health["conflicted_series"] == block["summary"]["conflict"] == 1  # a conflict of the last 24 hours
    assert health["status"] == "degraded"
    assert block["rule"] == {"behind_after_ms": BEHIND_AFTER_MS, "quiet_after_ms": QUIET_AFTER_MS, "backfill_days": 30}


def test_one_millisecond_decides_behind_the_same_way_for_the_api_and_the_page(tmp_path: Path) -> None:
    """The API's `behind` count and the page's must flip at the same millisecond of the span end."""
    results = {}
    for through in (END - 1, END):
        db = tmp_path / f"quasar{through}.db"
        make_database(db)
        conn = open_writer(db)
        try:
            for symbol in SYMBOLS:
                insert_candle(conn, S, symbol=symbol)
            insert_funding_rate(conn, END - 100, symbol=OK)
            set_funding_span(conn, S - DAY_MS, through, symbol=OK)
        finally:
            conn.close()
        with serve(db, funding=RULE) as url:
            block = body_of(Overviewer(http_fetch(url)).get())["funding_rate"]
        results[through] = (block["health"]["behind"], block["summary"]["behind"])

    assert results == {END - 1: (1, 1), END: (0, 0)}


def test_a_short_backfill_clamps_the_window_and_a_series_as_deep_as_configured_is_ok(tmp_path: Path) -> None:
    db = tmp_path / "quasar.db"
    seed(db)
    with serve(db, funding=FundingRateRule(publication_delay_ms=DELAY_MS, backfill_days=7)) as url:
        snapshot = Overviewer(http_fetch(url)).get()

    block = body_of(snapshot)["funding_rate"]
    assert block["window"] == {"start": AT - 7 * DAY_MS, "end": END, "clamped": True}
    symbols = by_symbol(snapshot)
    assert symbols[SHALLOW]["funding_rate"]["state"] == "ok"  # its span reaches back further than 7 days
    assert symbols[OK]["funding_rate"]["state"] == "ok"
    assert symbols[BEHIND]["funding_rate"]["state"] == "behind"


def test_disabled_makes_no_funding_call_and_leaves_the_other_parts_as_they_were(tmp_path: Path) -> None:
    db = tmp_path / "quasar.db"
    seed(db)
    with serve(db, funding=RULE) as url:
        enabled = Overviewer(recording(url)[0]).get()
    with serve(db, funding=None) as url:
        fetch, paths = recording(url)
        disabled = Overviewer(fetch).get()

    assert FUNDING_PATH not in paths
    assert body_of(disabled)["funding_rate"]["status"] == "disabled"
    assert all(s["funding_rate"] is None for s in by_symbol(disabled).values())
    for symbol in SYMBOLS:  # every candle and open interest field is the same with funding enabled and disabled
        on = {k: v for k, v in by_symbol(enabled)[symbol].items() if k != "funding_rate"}
        off = {k: v for k, v in by_symbol(disabled)[symbol].items() if k != "funding_rate"}
        assert on == off
    assert body_of(enabled)["summary"] == body_of(disabled)["summary"]
