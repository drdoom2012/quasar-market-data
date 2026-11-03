"""The two-phase refresh with open interest (OI-6b, design 8.1, 8.2, 8.9): candles first, then open interest."""

import json
from typing import Any, cast
from urllib.parse import parse_qsl, urlsplit

import pytest

from tests.tools.symbol_overview.fake_api import FakeApi
from tests.tools.symbol_overview.helpers import T0, instrument
from tests.tools.symbol_overview.oi_helpers import (
    ABSENT,
    OI_WINDOW,
    Clock,
    Reply,
    StubFetch,
    api_doc,
    error_reply,
    health_object,
    json_reply,
    slot,
    stub_routes,
)
from tools.symbol_overview import judge as judge_module
from tools.symbol_overview.fetch import TransportError, http_fetch
from tools.symbol_overview.judge import OiPart
from tools.symbol_overview.model import MAX_BODY_BYTES, TF_MS, OiReport
from tools.symbol_overview.refresh import Overviewer, Snapshot

NEWEST = T0 - 2 * TF_MS
OI = "/v1/open-interest/coverage"
CANDLE = "/v1/coverage"
HEALTHY = health_object()


def rows(count: int = 6) -> list[Any]:
    return [instrument(f"S{i:02d}/USDT:USDT", newest=NEWEST) for i in range(count)]


def run(fetch: StubFetch, clock: Clock | None = None, **kwargs: Any) -> Snapshot:
    kwargs.setdefault("monotonic", clock or Clock())
    return Overviewer(fetch, **kwargs).get()


def candle_part(body: dict[str, Any]) -> dict[str, Any]:
    """The overview without anything open interest added: what a candle-only refresh would have produced."""
    kept = {key: value for key, value in body.items() if key != "open_interest"}
    kept["refresh"] = {key: body["refresh"][key] for key in ("duration_ms", "deadline_hit")}
    kept["symbols"] = [{k: v for k, v in s.items() if k != "open_interest"} for s in body["symbols"]]
    return kept


def baseline(rs: list[Any]) -> dict[str, Any]:
    """A candle-only refresh against the same candle routes: the health body has no open interest key at all."""
    return candle_part(run(StubFetch(stub_routes(rs))).body)


def body_of(snapshot: Snapshot) -> dict[str, Any]:
    return cast(dict[str, Any], snapshot.body)


def by_symbol(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    return {s["symbol"]: s for s in body_of(snapshot)["symbols"]}


def queries(fetch: StubFetch, path: str) -> list[dict[str, str]]:
    return [dict(parse_qsl(urlsplit(p).query)) for p in fetch.paths if urlsplit(p).path == path]


# --- the health variants ---------------------------------------------------------------------------------------------


def test_disabled_makes_no_open_interest_call() -> None:
    rs = rows()
    fetch = StubFetch(stub_routes(rs, health_open_interest=None))

    snapshot = run(fetch)

    assert fetch.count(OI) == 0
    assert body_of(snapshot)["open_interest"]["status"] == "disabled"
    assert all(s["open_interest"] is None for s in by_symbol(snapshot).values())
    assert candle_part(snapshot.body) == baseline(rs)


@pytest.mark.parametrize(
    "value",
    [ABSENT, 3, "on", health_object(as_of_ms=T0 + 5_000), {**HEALTHY, "rule": {"cadence": 1}}],
    ids=["key absent", "number", "string", "as_of differs from T0", "malformed rule"],
)
def test_a_malformed_or_missing_health_object_makes_the_open_interest_part_unknown(value: object) -> None:
    rs = rows()
    fetch = StubFetch(stub_routes(rs, health_open_interest=value))

    snapshot = run(fetch)

    assert fetch.count(OI) == 0
    block = body_of(snapshot)["open_interest"]
    assert block["status"] == "unknown" and block["detail"].startswith("schema")
    assert block["summary"]["unknown"] == len(rs)
    for symbol in by_symbol(snapshot).values():
        assert symbol["open_interest"]["state"] == "unknown" and symbol["open_interest"]["strip"] is None
        assert symbol["state"] == "ok"  # the candle headline is untouched
    assert candle_part(snapshot.body) == baseline(rs)


# --- the calls -------------------------------------------------------------------------------------------------------


def test_every_instrument_gets_an_open_interest_call_with_the_shifted_window() -> None:
    rs = [*rows(3), instrument("NEW/USDT:USDT", newest=None)]
    fetch = StubFetch(stub_routes(rs, health_open_interest=HEALTHY))

    snapshot = run(fetch)

    sent = queries(fetch, OI)
    assert len(sent) == len(rs)  # also the instrument with no candle: only open interest says "no data"
    assert all(
        q["period"] == "5m" and q["start"] == str(OI_WINDOW.start) and q["end"] == str(OI_WINDOW.end) for q in sent
    )
    assert fetch.count(CANDLE) == 3  # the candle phase still skips an instrument with nothing stored
    assert by_symbol(snapshot)["NEW/USDT:USDT"]["open_interest"]["state"] == "no_data"
    assert by_symbol(snapshot)["NEW/USDT:USDT"]["state"] == "no_data"
    assert body_of(snapshot)["open_interest"]["status"] == "enabled"


def test_candles_run_first() -> None:
    fetch = StubFetch(stub_routes(rows(8), health_open_interest=HEALTHY))

    run(fetch)

    kinds = [urlsplit(p).path for p in fetch.paths]
    assert kinds[:2] == ["/health/ingest", "/v1/instruments"]
    assert max(i for i, k in enumerate(kinds) if k == CANDLE) < min(i for i, k in enumerate(kinds) if k == OI)


def test_a_failed_candle_call_does_not_skip_the_open_interest_call_and_the_reverse() -> None:
    rs = rows(2)
    a, b = rs[0].symbol, rs[1].symbol
    fetch = StubFetch(
        stub_routes(
            rs,
            health_open_interest=HEALTHY,
            candle={a: error_reply(500, "internal_error")},
            open_interest={b: json_reply({"not": "an answer"})},
        )
    )

    symbols = by_symbol(run(fetch))

    assert (symbols[a]["state"], symbols[a]["open_interest"]["state"]) == ("unknown", "ok")
    assert (symbols[b]["state"], symbols[b]["open_interest"]["state"]) == ("ok", "unknown")
    assert symbols[b]["open_interest"]["reasons"][0]["detail"] == "schema"


def test_other_failures_of_the_open_interest_call_touch_only_that_part() -> None:
    rs = rows(5)
    a, b, c, d, e = (r.symbol for r in rs)
    overrides: dict[str, Reply] = {
        a: TransportError("timeout"),
        b: error_reply(404, "open_interest_disabled"),  # contradicts the health answer
        c: error_reply(404, "unknown_series"),
        d: error_reply(500, "internal_error"),
    }
    fetch = StubFetch(stub_routes(rs, health_open_interest=HEALTHY, open_interest=overrides))

    snapshot = run(fetch)
    symbols = by_symbol(snapshot)

    expected = {
        a: ("unknown", "transport:timeout"),
        b: ("unknown", "api_error:open_interest_disabled"),
        c: ("no_data", ""),
        d: ("unknown", "api_error:internal_error"),
        e: ("ok", None),
    }
    for symbol, (state, detail) in expected.items():
        block = symbols[symbol]["open_interest"]
        assert block["state"] == state
        if detail:
            assert block["reasons"][0]["detail"] == detail
    assert candle_part(snapshot.body) == baseline(rs)  # no candle field moved


# --- the deadline ----------------------------------------------------------------------------------------------------


def test_the_candle_part_is_identical_to_a_candle_only_refresh_whatever_open_interest_costs() -> None:
    rs = rows(6)
    clock = Clock()

    def slow_open_interest(path: str) -> None:
        if urlsplit(path).path == OI:
            clock.now += 30.0  # each open interest call takes 30 s on the monotonic clock

    fetch = StubFetch(stub_routes(rs, health_open_interest=HEALTHY), on_call=slow_open_interest)

    snapshot = run(fetch, clock, concurrency=1, deadline_s=60.0)

    refresh = body_of(snapshot)["refresh"]
    assert refresh["deadline_hit"] is False and refresh["candle_not_asked"] == 0  # candle-only keys
    assert (
        refresh["oi_deadline_hit"] is True and refresh["oi_not_asked"] == 3
    )  # calls at 0 s, 30 s and 60 s were allowed
    assert fetch.count(OI) == 3
    symbols = by_symbol(snapshot)
    missed = [
        s
        for s in symbols.values()
        if s["open_interest"]["reasons"] and s["open_interest"]["reasons"][0]["detail"] == "deadline"
    ]
    assert len(missed) == 3
    assert all(s["open_interest"]["state"] == "unknown" and s["state"] == "ok" for s in missed)
    candle_only = candle_part(snapshot.body)
    candle_only["refresh"] = {"duration_ms": 0, "deadline_hit": False}
    expected = baseline(rs)
    expected["refresh"] = {"duration_ms": 0, "deadline_hit": False}
    assert candle_only == expected


def test_no_call_starts_after_the_deadline_in_either_phase() -> None:
    rs = rows(6)
    clock = Clock()
    seen_after_deadline: list[str] = []

    def slow_candles(path: str) -> None:
        if clock.now > 60.0:
            seen_after_deadline.append(path)
        if urlsplit(path).path == CANDLE:
            clock.now += 30.0

    fetch = StubFetch(stub_routes(rs, health_open_interest=HEALTHY), on_call=slow_candles)

    snapshot = run(fetch, clock, concurrency=1, deadline_s=60.0)

    refresh = body_of(snapshot)["refresh"]
    assert refresh["deadline_hit"] is True and refresh["candle_not_asked"] == 3
    assert refresh["oi_deadline_hit"] is True and refresh["oi_not_asked"] == len(rs)
    assert fetch.count(CANDLE) == 3 and fetch.count(OI) == 0
    assert seen_after_deadline == []
    assert snapshot.status == 200
    assert all(s["open_interest"]["state"] == "unknown" for s in by_symbol(snapshot).values())


def test_a_partial_snapshot_is_a_200_cached_for_15_seconds_and_nothing_older_is_served() -> None:
    rs = rows(2)
    clock = Clock()
    broken = {"on": False}

    def open_interest(query: dict[str, str]) -> Reply:
        return (
            error_reply(500, "internal_error")
            if broken["on"]
            else json_reply(api_doc(rows=[slot(0), slot(8000)], symbol=query["symbol"]))
        )

    fetch = StubFetch(
        stub_routes(rs, health_open_interest=HEALTHY, open_interest={r.symbol: open_interest for r in rs})
    )
    shared = Overviewer(fetch, monotonic=clock, min_interval_s=15.0)

    first = shared.get()
    calls = len(fetch.paths)
    clock.now = 14.9
    assert shared.get() is first and len(fetch.paths) == calls  # the cache: no call at all
    broken["on"] = True
    clock.now = 15.1
    second = shared.get()

    assert first.status == second.status == 200
    assert all(s["open_interest"]["state"] != "unknown" for s in by_symbol(first).values())
    assert all(s["open_interest"]["state"] == "unknown" for s in by_symbol(second).values())  # no "last good" answer


# --- bounds ----------------------------------------------------------------------------------------------------------


def test_at_most_four_calls_are_in_flight_across_both_phases_and_only_allowed_paths_are_used() -> None:
    rs = rows(12)
    with FakeApi(stub_routes(rs, health_open_interest=HEALTHY), delay_s=0.03) as api:  # type: ignore[arg-type]
        snapshot = Overviewer(http_fetch(api.url, timeout_s=5.0), concurrency=4).get()

        assert snapshot.status == 200
        assert 1 < api.max_in_flight <= 4
        assert api.count(OI) == 12 and api.count(CANDLE) == 12
        paths = {urlsplit(request).path for request in api.requests}
        assert paths == {"/health/ingest", "/v1/instruments", CANDLE, OI}


def test_a_judged_open_interest_report_is_all_the_part_keeps(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[OiPart] = []
    real = judge_module.build_overview

    def spy(*args: Any, **kwargs: Any) -> dict[str, object]:
        captured.append(kwargs["open_interest"])
        return real(*args, **kwargs)

    monkeypatch.setattr("tools.symbol_overview.refresh.build_overview", spy)

    run(StubFetch(stub_routes(rows(3), health_open_interest=HEALTHY)))

    assert len(captured) == 1 and len(captured[0].reports) == 3
    assert all(isinstance(report, OiReport) for report in captured[0].reports)  # no runs: the worker dropped them


def test_the_largest_feasible_body_passes_through_a_refresh() -> None:
    rs = rows(1)
    huge = api_doc(
        rows=[slot(0), slot(8639)],
        conflicts=[slot(i) for i in range(0, 8639, 2)],
        symbol=rs[0].symbol,
    )  # about 8 640 runs: conflicted and missing / no_longer_recoverable alternate
    body = json.dumps(huge).encode()
    assert 500_000 < len(body) < MAX_BODY_BYTES and len(huge["runs"]) > 8_000
    fetch = StubFetch(stub_routes(rs, health_open_interest=HEALTHY, open_interest={rs[0].symbol: json_reply(huge)}))

    symbol = by_symbol(run(fetch))[rs[0].symbol]

    assert symbol["open_interest"]["state"] == "conflict"
    assert symbol["open_interest"]["reasons"][0]["code"] == "conflict"
    assert symbol["open_interest"]["strip"] is not None


def test_a_body_over_the_cap_is_too_large() -> None:
    rs = rows(1)
    from tests.tools.symbol_overview.fake_api import FakeReply

    oversized = FakeReply(200, b" " * (MAX_BODY_BYTES + 1))
    fetch = StubFetch(stub_routes(rs, health_open_interest=HEALTHY, open_interest={rs[0].symbol: oversized}))

    symbol = by_symbol(run(fetch))[rs[0].symbol]

    assert symbol["open_interest"]["state"] == "unknown"
    assert symbol["open_interest"]["reasons"][0]["detail"] == "too_large"
