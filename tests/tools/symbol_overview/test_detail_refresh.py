"""The Detailer: calls, paging, fail-closed parts, cache and concurrency (design 5.2, 5.5), against a fake API."""

import dataclasses
import json
import threading
from collections.abc import Collection
from typing import Any

from tests.tools.symbol_overview import funding_helpers, oi_helpers
from tests.tools.symbol_overview.detail_helpers import (
    Route,
    api_error,
    candle_item,
    candles_doc,
    candles_route,
    funding_route,
    oi_route,
    reply,
)
from tests.tools.symbol_overview.fake_api import FakeApi, FakeReply
from tests.tools.symbol_overview.helpers import SYMBOL, T0, instrument
from tools.symbol_overview.detail import detail_windows
from tools.symbol_overview.detail_refresh import DetailAnswer, Detailer
from tools.symbol_overview.fetch import http_fetch
from tools.symbol_overview.model import TF_MS, InstrumentRow

W24 = detail_windows(T0, "24h")
W30 = detail_windows(T0, "30d")
S, E = W24.shown.start, W24.shown.end
NEWEST = T0 - TF_MS
HOUR = 3_600_000
SETTLEMENTS = [T0 - k * 8 * HOUR - 5 for k in range(1, 11)]  # a few ms after the settlement hour


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def instruments(rows: list[InstrumentRow] | None = None) -> Route:
    listed = rows if rows is not None else [instrument(newest=NEWEST)]
    return lambda query: reply(
        {
            "instruments": [
                {
                    "venue": r.venue,
                    "symbol": r.symbol,
                    "timeframe": r.timeframe,
                    "newest_open_time_ms": r.newest_open_time_ms,
                    **({"open_interest_period": r.open_interest_period} if r.open_interest_known else {}),
                }
                for r in listed
            ]
        }
    )


def routes(
    *,
    candle_times: Collection[int] | None = None,
    oi_times: Collection[int] | None = None,
    funding_times: Collection[int] | None = None,
    oi_on: bool = True,
    funding_on: bool = True,
    instrument_rows: list[InstrumentRow] | None = None,
) -> dict[str, Route]:
    ctimes = candle_times if candle_times is not None else range(W30.shown.start - 5 * TF_MS, NEWEST + TF_MS, TF_MS)
    otimes = oi_times if oi_times is not None else [t + TF_MS for t in ctimes]
    health = funding_helpers.health_body(
        funding_rate=funding_helpers.health_object() if funding_on else None,
        open_interest=oi_helpers.health_object() if oi_on else None,
    )
    return {
        "/health/ingest": lambda query: FakeReply(200, health),
        "/v1/instruments": instruments(instrument_rows),
        "/v1/candles": candles_route(ctimes),
        "/v1/open-interest": oi_route(otimes),
        "/v1/funding-rates": funding_route(funding_times if funding_times is not None else SETTLEMENTS),
    }


def get(
    api: FakeApi, range_name: str = "24h", symbol: str = SYMBOL, venue: str = "binance-usdm", **options: Any
) -> Any:
    answer = Detailer(http_fetch(api.url), **options).get(symbol, venue, "5m", range_name)
    return answer.status, json.loads(answer.body)


def test_one_detail_makes_the_documented_calls() -> None:
    with FakeApi(routes()) as api:
        status, doc = get(api)
    assert status == 200 and doc["range"] == "24h" and doc["window"] == {"start": S, "end": E}
    assert [api.count(p) for p in ("/health/ingest", "/v1/instruments", "/v1/candles", "/v1/open-interest")] == [
        1,
        1,
        1,
        1,
    ]
    assert api.count("/v1/funding-rates") == 1
    by_path = {path.split("?")[0]: q for path, q in zip(api.requests, api.queries, strict=True)}
    assert by_path["/v1/candles"] == {
        "venue": "binance-usdm",
        "symbol": SYMBOL,
        "timeframe": "5m",
        "start": str(S - TF_MS),
        "end": str(E),
        "limit": "1000",
    }
    assert by_path["/v1/open-interest"]["start"] == str(W24.open_interest.start)
    assert by_path["/v1/open-interest"]["end"] == str(W24.open_interest.end)
    assert by_path["/v1/open-interest"]["period"] == "5m"
    assert by_path["/v1/funding-rates"]["start"] == str(W24.funding.start)
    assert by_path["/v1/funding-rates"]["end"] == str(T0)
    assert all(doc[part]["status"] == "ok" for part in ("candles", "open_interest", "funding_rate"))
    assert doc["candles"]["rows"][-1]["t"] == NEWEST and doc["candles"]["change"] == {
        "percent": "0.00",
        "previous_close": "100",
    }
    assert doc["open_interest"]["rows"][-1]["t"] == T0 and doc["open_interest"]["change"]["percent"] == "0.00"
    assert doc["funding_rate"]["latest"]["t"] == SETTLEMENTS[0]


def test_a_thirty_day_range_pages_candles_until_next_is_null() -> None:
    with FakeApi(routes()) as api:
        status, doc = get(api, "30d")
    assert status == 200
    assert api.count("/v1/candles") == 9 and api.count("/v1/open-interest") == 9
    assert len(doc["candles"]["rows"]) == 8639  # 8640 shown slots, the one still open has no candle
    assert doc["candles"]["rows"][0]["t"] == W30.shown.start


def test_one_failing_page_fails_the_whole_part_and_only_that_part() -> None:
    inner = candles_route(range(W30.shown.start - 5 * TF_MS, NEWEST + TF_MS, TF_MS))
    calls: list[int] = []

    def flaky(query: dict[str, str]) -> FakeReply:
        calls.append(1)
        return api_error(503, "not_ready") if len(calls) == 5 else inner(query)

    table = routes() | {"/v1/candles": flaky}
    with FakeApi(table) as api:
        status, doc = get(api, "30d")
    assert status == 200 and len(calls) == 5
    assert doc["candles"] == {"status": "error", "code": "api_error", "detail": "not_ready"}  # no rows at all
    assert doc["open_interest"]["status"] == "ok" and doc["funding_rate"]["status"] == "ok"


def test_a_part_never_loops_more_than_the_page_limit() -> None:
    times = list(range(W30.shown.start - 5 * TF_MS, NEWEST + TF_MS, TF_MS))

    def one_row_pages(query: dict[str, str]) -> FakeReply:
        start = int(query["start"])
        rows = [candle_item(start)]
        index = (start - times[0]) // TF_MS
        nxt = times[index + 1] if index + 1 < len(times) else None
        return reply(
            candles_doc(rows, start=start, end=int(query["end"]), next_=nxt, oldest=times[0], newest=times[-1])
        )

    with FakeApi(routes() | {"/v1/candles": one_row_pages}) as api:
        _, doc = get(api, "30d")
    assert doc["candles"]["status"] == "error" and doc["candles"]["code"] == "schema"
    assert api.count("/v1/candles") == 12


def test_rows_overlapping_between_pages_are_an_error() -> None:
    times = list(range(W30.shown.start - 5 * TF_MS, NEWEST + TF_MS, TF_MS))
    inner = candles_route(times)
    calls: list[int] = []

    def overlapping(query: dict[str, str]) -> FakeReply:
        calls.append(1)
        if len(calls) == 2:  # the second page starts one slot before its cursor
            start = int(query["start"]) - TF_MS
            return reply(
                candles_doc(
                    [candle_item(start)],
                    start=int(query["start"]),
                    end=int(query["end"]),
                    oldest=times[0],
                    newest=times[-1],
                )
            )
        return inner(query)

    with FakeApi(routes() | {"/v1/candles": overlapping}) as api:
        _, doc = get(api, "30d")
    assert doc["candles"]["status"] == "error"


def test_an_unlisted_unsupported_or_never_stored_series() -> None:
    with FakeApi(routes()) as api:
        assert get(api, symbol="ETH/USDT:USDT")[0] == 404
        assert get(api, symbol="ETH/USDT:USDT")[1] == {"api_error": {"code": "unknown_series", "detail": ""}}
        assert api.count("/v1/candles") == 0 and api.count("/health/ingest") == 2 and api.count("/v1/instruments") == 2
    with FakeApi(routes()) as api:
        status, doc = get(api, venue="x")
        assert (status, doc["api_error"]["code"]) == (404, "unsupported_series") and api.requests == []
    with FakeApi(routes()) as api:
        answer = Detailer(http_fetch(api.url)).get(SYMBOL, "binance-usdm", "1m", "24h")
        assert answer.status == 404 and json.loads(answer.body)["api_error"]["code"] == "unsupported_series"
        assert api.requests == []  # an unsupported timeframe is refused before any call
    with FakeApi(routes(instrument_rows=[instrument(newest=None)])) as api:
        status, doc = get(api)
        assert status == 200 and doc["candles"]["status"] == "empty"
        assert api.count("/v1/candles") == 0 and api.count("/v1/open-interest") == 1


def test_disabled_open_interest_and_funding_make_no_call() -> None:
    with FakeApi(routes(oi_on=False, funding_on=False)) as api:
        _, doc = get(api)
    assert doc["open_interest"]["status"] == "disabled" and doc["funding_rate"]["status"] == "disabled"
    assert api.count("/v1/open-interest") == 0 and api.count("/v1/funding-rates") == 0


def test_unknown_series_of_open_interest_and_funding_is_empty_not_an_error() -> None:
    table = routes() | {
        "/v1/open-interest": lambda query: api_error(404, "unknown_series"),
        "/v1/funding-rates": lambda query: api_error(404, "unknown_series"),
    }
    with FakeApi(table) as api:
        _, doc = get(api)
    assert doc["open_interest"]["status"] == "empty" and doc["funding_rate"]["status"] == "empty"
    assert doc["candles"]["status"] == "ok"


def test_an_explicit_null_period_is_not_collected_and_makes_no_open_interest_call() -> None:
    unlisted = dataclasses.replace(instrument(newest=NEWEST), open_interest_period=None, open_interest_known=True)
    with FakeApi(routes(instrument_rows=[unlisted])) as api:
        status, doc = get(api)
    assert status == 200 and doc["open_interest"] == {"status": "not_collected", "code": "", "detail": ""}
    assert api.count("/v1/open-interest") == 0
    assert doc["candles"]["status"] == "ok" and doc["funding_rate"]["status"] == "ok"


def test_a_period_string_or_an_absent_key_reads_open_interest_as_before() -> None:
    listed = dataclasses.replace(instrument(newest=NEWEST), open_interest_period="5m", open_interest_known=True)
    for row in (listed, instrument(newest=NEWEST)):
        with FakeApi(routes(instrument_rows=[row])) as api:
            _, doc = get(api)
        assert doc["open_interest"]["status"] == "ok" and api.count("/v1/open-interest") >= 1
    table = routes() | {"/v1/open-interest": lambda query: api_error(404, "unknown_series")}
    with FakeApi(table) as api:  # an API older than A-6: an unknown series is still "nothing stored"
        _, doc = get(api)
    assert doc["open_interest"]["status"] == "empty"


def test_unknown_series_of_candles_for_a_stored_instrument_is_inconsistent() -> None:
    with FakeApi(routes() | {"/v1/candles": lambda query: api_error(404, "unknown_series")}) as api:
        _, doc = get(api)
    assert doc["candles"] == {"status": "error", "code": "inconsistent_with_instruments", "detail": ""}


def test_the_deadline_stops_new_calls() -> None:
    clock = Clock()
    inner = candles_route(range(W30.shown.start - 5 * TF_MS, NEWEST + TF_MS, TF_MS))

    def slow(query: dict[str, str]) -> FakeReply:
        clock.now = 100.0  # the first page "took" 100 s
        return inner(query)

    with FakeApi(routes() | {"/v1/candles": slow}) as api:
        _, doc = get(api, "30d", deadline_s=30.0, monotonic=clock)
    assert doc["candles"]["status"] == "error" and doc["candles"]["code"] == "deadline"
    assert api.count("/v1/candles") == 1


def test_health_or_instruments_failure_is_a_page_error() -> None:
    with FakeApi(routes() | {"/health/ingest": lambda query: api_error(503, "not_ready")}) as api:
        status, doc = get(api)
    assert status == 502 and doc == {"api_error": {"code": "api_error", "detail": "not_ready"}}
    assert api.count("/v1/instruments") == 0
    with FakeApi(routes() | {"/v1/instruments": lambda query: FakeReply(500, b"oops", "text/plain")}) as api:
        status, doc = get(api)
    assert status == 502 and api.count("/v1/candles") == 0


def test_cache_by_range_and_symbol() -> None:
    clock = Clock()
    with FakeApi(routes()) as api:
        detailer = Detailer(http_fetch(api.url), monotonic=clock)
        first = detailer.get(SYMBOL, "binance-usdm", "5m", "24h")
        assert detailer.get(SYMBOL, "binance-usdm", "5m", "24h") == first and api.count("/v1/candles") == 1
        clock.now = 14.9
        detailer.get(SYMBOL, "binance-usdm", "5m", "24h")
        assert api.count("/v1/candles") == 1
        clock.now = 15.1  # the lifetime of 24h is 15 s
        detailer.get(SYMBOL, "binance-usdm", "5m", "24h")
        assert api.count("/v1/candles") == 2
        detailer.get(SYMBOL, "binance-usdm", "5m", "7d")  # another range is another entry (2017 slots: 3 pages)
        assert api.count("/v1/candles") == 5
        clock.now = 15.1 + 59.0
        detailer.get(SYMBOL, "binance-usdm", "5m", "7d")  # 7d lives 60 s
        assert api.count("/v1/candles") == 5


def test_the_byte_budget_evicts_the_oldest_and_an_oversized_answer_is_not_kept() -> None:
    with FakeApi(routes()) as api:
        size = len(Detailer(http_fetch(api.url)).get(SYMBOL, "binance-usdm", "5m", "24h").body)
        detailer = Detailer(http_fetch(api.url), cache_bytes=int(size * 1.2))
        detailer.get(SYMBOL, "binance-usdm", "5m", "6h")
        detailer.get(SYMBOL, "binance-usdm", "5m", "24h")  # fits alone, not together with the 6h answer: evicts it
        before = api.count("/v1/candles")
        detailer.get(SYMBOL, "binance-usdm", "5m", "24h")
        assert api.count("/v1/candles") == before  # kept
        detailer.get(SYMBOL, "binance-usdm", "5m", "6h")
        assert api.count("/v1/candles") == before + 1  # evicted
    with FakeApi(routes()) as api:
        tiny = Detailer(http_fetch(api.url), cache_bytes=10)
        tiny.get(SYMBOL, "binance-usdm", "5m", "1h")
        tiny.get(SYMBOL, "binance-usdm", "5m", "1h")
        assert api.count("/v1/candles") == 2  # served, never kept


def test_errors_and_not_found_are_kept_only_briefly() -> None:
    clock = Clock()
    table = routes() | {"/v1/candles": lambda query: api_error(503, "not_ready")}
    with FakeApi(table) as api:
        detailer = Detailer(http_fetch(api.url), monotonic=clock)
        detailer.get(SYMBOL, "binance-usdm", "5m", "24h")
        detailer.get(SYMBOL, "binance-usdm", "5m", "24h")
        assert api.count("/v1/candles") == 1  # within the 5 s
        clock.now = 6.0  # a good answer would still be cached (15 s)
        detailer.get(SYMBOL, "binance-usdm", "5m", "24h")
        assert api.count("/v1/candles") == 2


def test_a_second_identical_request_waiting_on_the_lock_does_not_repeat_the_work() -> None:
    with FakeApi(routes(), delay_s=0.05) as api:
        detailer = Detailer(http_fetch(api.url))
        answers: list[DetailAnswer] = []

        def run() -> None:
            answers.append(detailer.get(SYMBOL, "binance-usdm", "5m", "24h"))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert len(answers) == 2 and answers[0] == answers[1]
        assert api.count("/v1/candles") == 1 and api.count("/health/ingest") == 1


def test_a_waiter_gives_up_with_busy() -> None:
    with FakeApi(routes(), delay_s=0.3) as api:
        detailer = Detailer(http_fetch(api.url), wait_s=0.05)
        worker = threading.Thread(target=lambda: detailer.get(SYMBOL, "binance-usdm", "5m", "24h"))
        worker.start()
        while api.count("/health/ingest") == 0:
            pass
        answer = detailer.get(SYMBOL, "binance-usdm", "5m", "6h")
        worker.join(timeout=20)
    assert answer.status == 503 and json.loads(answer.body) == {"api_error": {"code": "busy", "detail": ""}}


def test_at_most_three_calls_are_in_flight() -> None:
    with FakeApi(routes(), delay_s=0.02) as api:
        get(api, "30d")
    assert 2 <= api.max_in_flight <= 3


def test_funding_rows_are_only_the_shown_window_and_latest_is_kept() -> None:
    with FakeApi(routes(funding_times=SETTLEMENTS)) as api:
        _, doc = get(api, "1h")
    assert doc["funding_rate"]["rows"] == []  # every settlement is older than the hour on the chart
    assert doc["funding_rate"]["latest"]["t"] == SETTLEMENTS[0]
    assert doc["funding_rate"]["newest_ms"] == SETTLEMENTS[0]
    with FakeApi(routes(funding_times=SETTLEMENTS)) as api:
        _, doc = get(api, "24h")
    assert [r["t"] for r in doc["funding_rate"]["rows"]] == [t for t in sorted(SETTLEMENTS) if t >= S]


def test_changes_use_the_lookback_rows() -> None:
    with FakeApi(routes(candle_times=[S - TF_MS, S], oi_times=[], funding_times=[])) as api:
        _, doc = get(api, "24h")
    assert [r["t"] for r in doc["candles"]["rows"]] == [S]  # the lookback row is not shown
    assert doc["candles"]["change"] == {"percent": "0.00", "previous_close": "100"}


def test_a_change_uses_exact_decimals() -> None:
    def closes(t: int) -> str:
        return "0.3" if t == NEWEST else "0.1"

    table = routes() | {"/v1/candles": candles_route(range(S - TF_MS, NEWEST + TF_MS, TF_MS), close=closes)}
    with FakeApi(table) as api:
        _, doc = get(api)
    assert doc["candles"]["change"] == {"percent": "+200.00", "previous_close": "0.1"}
