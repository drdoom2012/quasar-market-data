import json
import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

from tests.tools.symbol_overview.fake_api import FakeApi, FakeReply, Route
from tests.tools.symbol_overview.helpers import T0, coverage_doc, health_doc, instrument
from tools.symbol_overview.fetch import Fetch, RawResponse, http_fetch
from tools.symbol_overview.model import TF_MS, InstrumentRow, Window
from tools.symbol_overview.refresh import Overviewer, Snapshot

NEWEST = T0 - 2 * TF_MS
NOT_READY = FakeReply(503, json.dumps({"error": {"code": "not_ready", "message": "x", "details": {}}}).encode())


def reply(doc: object) -> FakeReply:
    return FakeReply(200, json.dumps(doc).encode())


def instruments_reply(rows: Sequence[InstrumentRow]) -> FakeReply:
    return reply(
        {
            "instruments": [
                {
                    "venue": r.venue,
                    "symbol": r.symbol,
                    "timeframe": r.timeframe,
                    "newest_open_time_ms": r.newest_open_time_ms,
                }
                for r in rows
            ]
        }
    )


def stale(window: Window, newest: int) -> Mapping[int, str]:
    return {t: "stale" for t in range(newest + TF_MS, T0, TF_MS)}


def hole(window: Window, newest: int) -> Mapping[int, str]:
    return {window.start + 10 * TF_MS: "missing"}


def routes(
    rows: Sequence[InstrumentRow],
    *,
    states: Mapping[str, Callable[[Window, int], Mapping[int, str]]] | None = None,
    failing: Mapping[str, FakeReply] | None = None,
) -> dict[str, Route]:
    """A consistent fake API: the coverage answer is built from the request, so the echo always matches."""
    by_symbol = {r.symbol: r for r in rows}

    def coverage(query: dict[str, str]) -> FakeReply:
        symbol = query["symbol"]
        if failing and symbol in failing:
            return failing[symbol]
        window = Window(int(query["start"]), int(query["end"]))
        newest = by_symbol[symbol].newest_open_time_ms
        assert newest is not None
        overrides = states[symbol](window, newest) if states and symbol in states else None
        return reply(coverage_doc(window, oldest=window.start, newest=newest, symbol=symbol, slot_states=overrides))

    return {
        "/health/ingest": lambda query: reply(health_doc()),
        "/v1/instruments": lambda query: instruments_reply(rows),
        "/v1/coverage": coverage,
    }


def overviewer(api: FakeApi, **kwargs: Any) -> Overviewer:
    return Overviewer(http_fetch(api.url, timeout_s=5.0), **kwargs)


def symbols(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    return {s["symbol"]: s for s in snapshot.body["symbols"]}  # type: ignore[attr-defined]


# one refresh


def test_full_refresh_judges_every_symbol() -> None:
    rows = [
        instrument("ADA/USDT:USDT", newest=T0 - 30 * TF_MS),
        instrument("BTC/USDT:USDT"),
        instrument("ETH/USDT:USDT"),
        instrument("XRP/USDT:USDT", newest=None),
    ]
    with FakeApi(routes(rows, states={"ADA/USDT:USDT": stale, "ETH/USDT:USDT": hole})) as api:
        snapshot = overviewer(api).get()
        assert snapshot.status == 200
        by_symbol = symbols(snapshot)
        assert {s: v["state"] for s, v in by_symbol.items()} == {
            "ADA/USDT:USDT": "stale",
            "BTC/USDT:USDT": "ok",
            "ETH/USDT:USDT": "holes",
            "XRP/USDT:USDT": "no_data",
        }
        assert api.count("/v1/coverage") == 3  # nothing was asked for XRP, which has nothing stored
        assert not any("XRP" in q.get("symbol", "") for q in api.queries)
        assert snapshot.body["generated_at_ms"] == T0
        assert snapshot.body["summary"] == {
            "total": 4,
            "no_data": 1,
            "unknown": 0,
            "stale": 1,
            "conflict": 0,
            "holes": 1,
            "behind": 0,
            "ok": 1,
        }


def test_coverage_call_has_exactly_the_documented_parameters() -> None:
    with FakeApi(routes([instrument()])) as api:
        overviewer(api).get()
        coverage_queries = [q for q in api.queries if "symbol" in q]
        assert [set(q) for q in coverage_queries] == [{"venue", "symbol", "timeframe", "start", "end"}]
        assert api.requests[0] == "/health/ingest"


# page-level failures


def test_page_error_when_health_fails() -> None:
    table = routes([instrument()]) | {"/health/ingest": lambda query: NOT_READY}
    with FakeApi(table) as api:
        snapshot = overviewer(api).get()
        assert snapshot.status == 502
        assert snapshot.body == {"api_error": {"code": "api_error", "detail": "not_ready"}}
        assert api.requests == ["/health/ingest"]


def test_page_error_when_instruments_fail() -> None:
    table = routes([instrument()]) | {"/v1/instruments": lambda query: FakeReply(500, b"oops", "text/plain")}
    with FakeApi(table) as api:
        snapshot = overviewer(api).get()
        assert snapshot.status == 502
        assert snapshot.body == {"api_error": {"code": "bad_status", "detail": "500"}}
        assert api.count("/v1/coverage") == 0


def test_page_error_when_the_universe_is_empty() -> None:
    with FakeApi(routes([])) as api:
        snapshot = overviewer(api).get()
        assert (snapshot.status, snapshot.body) == (502, {"api_error": {"code": "empty_universe", "detail": ""}})


def test_connection_refused_is_a_page_error() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    snapshot = Overviewer(http_fetch(f"http://127.0.0.1:{port}")).get()
    assert (snapshot.status, snapshot.body) == (502, {"api_error": {"code": "transport", "detail": "connection"}})


def test_all_instruments_no_data() -> None:
    rows = [instrument("A/USDT:USDT", newest=None), instrument("B/USDT:USDT", newest=None)]
    with FakeApi(routes(rows)) as api:
        snapshot = overviewer(api).get()
        assert snapshot.status == 200
        assert {v["state"] for v in symbols(snapshot).values()} == {"no_data"}
        assert snapshot.body["summary"]["ok"] == 0  # type: ignore[index]
        assert api.count("/v1/coverage") == 0


# per-symbol failures


def test_one_coverage_503_marks_only_that_symbol_unknown() -> None:
    rows = [instrument("BTC/USDT:USDT"), instrument("ETH/USDT:USDT")]
    with FakeApi(routes(rows, failing={"ETH/USDT:USDT": NOT_READY})) as api:
        by_symbol = symbols(overviewer(api).get())
        assert by_symbol["BTC/USDT:USDT"]["state"] == "ok"
        assert by_symbol["ETH/USDT:USDT"]["state"] == "unknown"
        assert by_symbol["ETH/USDT:USDT"]["reasons"][0]["detail"] == "api_error:not_ready"


def test_unsupported_timeframe_symbol_is_unknown_without_request() -> None:
    odd = InstrumentRow("binance-usdm", "BTC/USDT:USDT", "1h", NEWEST)
    rows = [odd, instrument("ETH/USDT:USDT")]
    with FakeApi(routes(rows)) as api:
        by_symbol = symbols(overviewer(api).get())
        assert by_symbol["BTC/USDT:USDT"]["state"] == "unknown"
        assert by_symbol["BTC/USDT:USDT"]["reasons"][0]["detail"] == "schema:unsupported_series"
        assert by_symbol["ETH/USDT:USDT"]["state"] == "ok"
        assert [q["symbol"] for q in api.queries if "symbol" in q] == ["ETH/USDT:USDT"]


# concurrency and deadline


def test_concurrency_is_bounded() -> None:
    rows = [instrument(f"S{i:02d}/USDT:USDT") for i in range(20)]
    with FakeApi(routes(rows), delay_s=0.05) as api:
        snapshot = overviewer(api, concurrency=4).get()
        assert snapshot.status == 200
        assert 1 < api.max_in_flight <= 4


def test_deadline_marks_remaining_symbols_unknown_and_starts_no_new_call() -> None:
    rows = [instrument(f"S{i:02d}/USDT:USDT") for i in range(10)]
    with FakeApi(routes(rows), delay_s=0.1) as api:
        snapshot = overviewer(api, concurrency=1, deadline_s=0.55).get()
        by_symbol = symbols(snapshot)
        unknown = [s for s, v in by_symbol.items() if v["state"] == "unknown"]
        answered = [s for s, v in by_symbol.items() if v["state"] == "ok"]
        assert snapshot.body["refresh"]["deadline_hit"] is True  # type: ignore[index]
        assert answered and len(unknown) >= 3
        assert all(by_symbol[s]["reasons"][0]["detail"] == "deadline" for s in unknown)
        assert api.count("/v1/coverage") == len(answered)  # no request was made for a symbol that timed out


# single flight and cache


def test_simultaneous_gets_share_one_refresh() -> None:
    results: list[Snapshot] = []
    barrier = threading.Barrier(8)
    with FakeApi(routes([instrument()]), delay_s=0.05) as api:
        shared = overviewer(api)

        def call() -> None:
            barrier.wait()
            results.append(shared.get())

        threads = [threading.Thread(target=call) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert api.count("/health/ingest") == 1
        assert len(results) == 8
        assert all(result == results[0] for result in results)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_get_within_min_interval_makes_no_api_call() -> None:
    clock = Clock()
    with FakeApi(routes([instrument()])) as api:
        shared = overviewer(api, monotonic=clock, min_interval_s=15.0)
        first = shared.get()
        requests = len(api.requests)
        clock.now = 14.9
        assert shared.get() is first
        assert len(api.requests) == requests
        clock.now = 15.1
        shared.get()
        assert api.count("/health/ingest") == 2


def test_errors_are_cached_for_the_min_interval() -> None:
    clock = Clock()
    table = routes([instrument()]) | {"/health/ingest": lambda query: NOT_READY}
    with FakeApi(table) as api:
        shared = overviewer(api, monotonic=clock, min_interval_s=15.0)
        shared.get()
        clock.now = 5.0
        shared.get()
        assert api.count("/health/ingest") == 1


def test_a_bug_in_fetch_is_not_cached() -> None:
    with FakeApi(routes([instrument()])) as api:
        real = http_fetch(api.url)
        calls = {"n": 0}

        def flaky(path: str) -> RawResponse:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("bug")
            return real(path)

        fetch: Fetch = flaky
        shared = Overviewer(fetch)
        with pytest.raises(RuntimeError):
            shared.get()
        assert shared.get().status == 200  # the failed refresh left nothing behind, not even the in-flight marker
