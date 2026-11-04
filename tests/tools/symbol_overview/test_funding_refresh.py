"""The three-phase refresh with funding (FR-6 design 5): candles, then open interest, then funding."""

from typing import Any, cast
from urllib.parse import parse_qsl, urlsplit

import pytest

from tests.tools.symbol_overview.fake_api import FakeApi, FakeReply
from tests.tools.symbol_overview.funding_helpers import (
    ABSENT,
    BEHIND_AFTER_MS,
    FWINDOW,
    funding_doc,
    funding_stub_routes,
    health_object,
)
from tests.tools.symbol_overview.helpers import T0, instrument
from tests.tools.symbol_overview.oi_helpers import (
    Clock,
    Reply,
    StubFetch,
    error_reply,
    json_reply,
    stub_routes,
)
from tests.tools.symbol_overview.oi_helpers import (
    health_object as oi_health_object,
)
from tools.symbol_overview.fetch import TransportError, http_fetch
from tools.symbol_overview.model import MAX_BODY_BYTES, TF_MS
from tools.symbol_overview.refresh import Overviewer, Snapshot
from tools.symbol_overview.window import window_for

NEWEST = T0 - 2 * TF_MS
FUNDING = "/v1/funding-rates"
OI = "/v1/open-interest/coverage"
CANDLE = "/v1/coverage"
HEALTHY = health_object()
OI_HEALTHY = oi_health_object()


def rows(count: int = 6) -> list[Any]:
    return [instrument(f"S{i:02d}/USDT:USDT", newest=NEWEST) for i in range(count)]


def run(fetch: StubFetch, clock: Clock | None = None, **kwargs: Any) -> Snapshot:
    kwargs.setdefault("monotonic", clock or Clock())
    return Overviewer(fetch, **kwargs).get()


def body_of(snapshot: Snapshot) -> dict[str, Any]:
    return cast(dict[str, Any], snapshot.body)


def by_symbol(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    return {s["symbol"]: s for s in body_of(snapshot)["symbols"]}


def queries(fetch: StubFetch, path: str) -> list[dict[str, str]]:
    return [dict(parse_qsl(urlsplit(p).query)) for p in fetch.paths if urlsplit(p).path == path]


def without_funding(body: dict[str, Any]) -> dict[str, Any]:
    """The overview without anything funding added: what a refresh without funding would have produced."""
    kept = {key: value for key, value in body.items() if key != "funding_rate"}
    kept["refresh"] = {k: v for k, v in body["refresh"].items() if not k.startswith("funding")}
    kept["symbols"] = [{k: v for k, v in s.items() if k != "funding_rate"} for s in body["symbols"]]
    return kept


# --- the health variants ------------------------------------------------------------------------------------------------


def test_disabled_makes_no_funding_call_and_changes_nothing_else() -> None:
    rs = rows()
    fetch = StubFetch(funding_stub_routes(rs, health_funding=None, health_open_interest=OI_HEALTHY))
    with_oi_only = run(StubFetch(stub_routes(rs, health_open_interest=OI_HEALTHY)))

    snapshot = run(fetch)

    assert fetch.count(FUNDING) == 0
    block = body_of(snapshot)["funding_rate"]
    assert block["status"] == "disabled" and block["summary"] is None and block["window"] is None
    assert all(s["funding_rate"] is None for s in by_symbol(snapshot).values())
    assert without_funding(body_of(snapshot)) == without_funding(body_of(with_oi_only))


@pytest.mark.parametrize(
    "value",
    [
        ABSENT,
        3,
        "on",
        health_object(as_of_ms=T0 + 5_000),
        {**HEALTHY, "rule": {"behind_after_ms": 1}},
        {**HEALTHY, "status": "fine"},
    ],
    ids=["key absent", "number", "string", "as_of differs from T0", "malformed rule", "unknown status"],
)
def test_a_malformed_or_missing_health_object_makes_the_funding_part_unknown(value: object) -> None:
    rs = rows()
    fetch = StubFetch(funding_stub_routes(rs, health_funding=value, health_open_interest=OI_HEALTHY))

    snapshot = run(fetch)

    assert fetch.count(FUNDING) == 0
    block = body_of(snapshot)["funding_rate"]
    assert block["status"] == "unknown" and block["detail"].startswith("schema")
    assert block["summary"]["unknown"] == len(rs) and block["window"] is None
    for symbol in by_symbol(snapshot).values():
        assert symbol["funding_rate"]["state"] == "unknown"
        assert symbol["state"] == "ok" and symbol["open_interest"]["state"] == "ok"  # the other parts are untouched


def test_a_rule_that_cannot_make_a_window_is_unknown_with_no_call() -> None:
    rule = {**HEALTHY["rule"], "behind_after_ms": 40 * 86_400_000}  # the window would end before it starts
    fetch = StubFetch(funding_stub_routes(rows(2), health_funding={**HEALTHY, "rule": rule}))

    block = body_of(run(fetch))["funding_rate"]

    assert fetch.count(FUNDING) == 0
    assert block["status"] == "unknown" and block["detail"] == "schema:funding_window"


# --- the calls ----------------------------------------------------------------------------------------------------------


def test_every_instrument_gets_one_call_with_exactly_the_documented_window() -> None:
    rs = [*rows(3), instrument("NEW/USDT:USDT", newest=None)]
    fetch = StubFetch(funding_stub_routes(rs, health_funding=HEALTHY))

    snapshot = run(fetch)

    sent = queries(fetch, FUNDING)
    assert len(sent) == len(rs)  # also the instrument with no candle: only funding says "no data"
    window = FWINDOW
    assert window.end == T0 - BEHIND_AFTER_MS + 1 and window.start == window_for(T0).start
    assert all(
        set(q) == {"venue", "symbol", "start", "end", "limit"}
        and (q["start"], q["end"], q["limit"]) == (str(window.start), str(window.end), "2")
        for q in sent
    )
    assert by_symbol(snapshot)["NEW/USDT:USDT"]["funding_rate"]["state"] == "no_data"
    block = body_of(snapshot)["funding_rate"]
    assert block["status"] == "enabled" and block["window"] == {
        "start": window.start,
        "end": window.end,
        "clamped": False,
    }
    assert block["summary"]["ok"] == 3 and block["summary"]["no_data"] == 1


def test_a_short_backfill_clamps_the_window_and_says_so() -> None:
    rule = {**HEALTHY["rule"], "backfill_days": 7}
    fetch = StubFetch(funding_stub_routes(rows(2), health_funding={**HEALTHY, "rule": rule}))

    snapshot = run(fetch)

    assert {q["start"] for q in queries(fetch, FUNDING)} == {str(T0 - 7 * 86_400_000)}
    assert body_of(snapshot)["funding_rate"]["window"]["clamped"] is True
    assert all(s["funding_rate"]["state"] == "ok" for s in by_symbol(snapshot).values())


def test_the_phases_run_in_order_candles_then_open_interest_then_funding() -> None:
    fetch = StubFetch(funding_stub_routes(rows(8), health_funding=HEALTHY, health_open_interest=OI_HEALTHY))

    run(fetch)

    kinds = [urlsplit(p).path for p in fetch.paths]
    assert kinds[:2] == ["/health/ingest", "/v1/instruments"]
    last = {k: max(i for i, x in enumerate(kinds) if x == k) for k in (CANDLE, OI, FUNDING)}
    first = {k: min(i for i, x in enumerate(kinds) if x == k) for k in (CANDLE, OI, FUNDING)}
    assert last[CANDLE] < first[OI] and last[OI] < first[FUNDING]


def test_funding_runs_without_open_interest_after_the_candles() -> None:
    fetch = StubFetch(funding_stub_routes(rows(4), health_funding=HEALTHY, health_open_interest=None))

    snapshot = run(fetch)

    assert fetch.count(OI) == 0 and fetch.count(FUNDING) == 4
    assert body_of(snapshot)["open_interest"]["status"] == "disabled"
    assert body_of(snapshot)["funding_rate"]["status"] == "enabled"


def test_a_symbol_whose_funding_call_fails_does_not_touch_the_other_parts() -> None:
    rs = rows(6)
    a, b, c, d, e, f = (r.symbol for r in rs)
    bad_echo = funding_doc(symbol="OTHER/USDT:USDT", window=FWINDOW)
    overrides: dict[str, Any] = {
        a: TransportError("timeout"),
        b: error_reply(404, "funding_rate_disabled"),  # contradicts the health answer
        c: error_reply(404, "unknown_series"),
        d: error_reply(500, "internal_error"),
        e: json_reply(bad_echo),
        f: FakeReply(200, b"x" * (MAX_BODY_BYTES + 1)),
    }
    fetch = StubFetch(
        funding_stub_routes(rs, health_funding=HEALTHY, health_open_interest=OI_HEALTHY, funding=overrides)
    )
    baseline = run(StubFetch(stub_routes(rs, health_open_interest=OI_HEALTHY)))

    snapshot = run(fetch)
    symbols = by_symbol(snapshot)

    expected = {
        a: ("unknown", "transport:timeout"),
        b: ("unknown", "api_error:funding_rate_disabled"),
        c: ("no_data", ""),
        d: ("unknown", "api_error:internal_error"),
        e: ("unknown", "echo"),
        f: ("unknown", "too_large"),
    }
    for symbol, (state, detail) in expected.items():
        block = symbols[symbol]["funding_rate"]
        assert block["state"] == state
        if detail:
            assert block["reasons"][0]["detail"] == detail
    assert without_funding(body_of(snapshot)) == without_funding(body_of(baseline))


def test_the_states_of_the_api_answers_reach_the_overview() -> None:
    rs = rows(4)
    healthy, conflicted, behind, quiet = (r.symbol for r in rs)
    overrides = {
        conflicted: json_reply(funding_doc(conflicted=2, symbol=conflicted)),
        behind: json_reply(funding_doc(span=(FWINDOW.start - 5, FWINDOW.end - 1), symbol=behind)),
        quiet: json_reply(funding_doc(oldest=None, newest=None, events=0, symbol=quiet)),
    }
    fetch = StubFetch(funding_stub_routes(rs, health_funding=HEALTHY, funding=overrides))

    snapshot = run(fetch)

    states = {s: v["funding_rate"]["state"] for s, v in by_symbol(snapshot).items()}
    assert states == {healthy: "ok", conflicted: "conflict", behind: "behind", quiet: "quiet"}
    assert body_of(snapshot)["funding_rate"]["health"]["status"] == "current"


# --- the deadline -------------------------------------------------------------------------------------------------------


def test_the_other_parts_are_identical_to_a_refresh_without_funding_whatever_funding_costs() -> None:
    rs = rows(6)
    clock = Clock()

    def slow_funding(path: str) -> None:
        if urlsplit(path).path == FUNDING:
            clock.now += 30.0

    fetch = StubFetch(
        funding_stub_routes(rs, health_funding=HEALTHY, health_open_interest=OI_HEALTHY), on_call=slow_funding
    )

    snapshot = run(fetch, clock, concurrency=1, deadline_s=60.0)

    refresh = body_of(snapshot)["refresh"]
    assert refresh["deadline_hit"] is False and refresh["oi_deadline_hit"] is False  # the other parts' keys
    assert refresh["funding_deadline_hit"] is True and refresh["funding_not_asked"] == 3  # calls at 0, 30, 60 s
    assert fetch.count(FUNDING) == 3
    missed = [
        s
        for s in by_symbol(snapshot).values()
        if s["funding_rate"]["reasons"] and s["funding_rate"]["reasons"][0]["detail"] == "deadline"
    ]
    assert len(missed) == 3
    assert all(s["funding_rate"]["state"] == "unknown" and s["state"] == "ok" for s in missed)
    assert all(s["open_interest"]["state"] == "ok" for s in missed)
    plain = run(StubFetch(stub_routes(rs, health_open_interest=OI_HEALTHY)))
    got, expected = without_funding(body_of(snapshot)), without_funding(body_of(plain))
    got["refresh"] = expected["refresh"] = {}
    assert got == expected


def test_no_call_starts_after_the_deadline_in_any_phase() -> None:
    rs = rows(6)
    clock = Clock()
    after: list[str] = []

    def slow_candles(path: str) -> None:
        if clock.now > 60.0:
            after.append(path)
        if urlsplit(path).path == CANDLE:
            clock.now += 30.0

    fetch = StubFetch(
        funding_stub_routes(rs, health_funding=HEALTHY, health_open_interest=OI_HEALTHY), on_call=slow_candles
    )

    snapshot = run(fetch, clock, concurrency=1, deadline_s=60.0)

    refresh = body_of(snapshot)["refresh"]
    assert refresh["deadline_hit"] is True and refresh["oi_not_asked"] == len(rs)
    assert refresh["funding_deadline_hit"] is True and refresh["funding_not_asked"] == len(rs)
    assert fetch.count(OI) == 0 and fetch.count(FUNDING) == 0 and after == []
    assert snapshot.status == 200
    assert all(s["funding_rate"]["state"] == "unknown" for s in by_symbol(snapshot).values())


def test_a_partial_funding_snapshot_is_a_200_cached_for_15_seconds_and_nothing_older_is_served() -> None:
    rs = rows(2)
    clock = Clock()
    broken = {"on": False}

    def funding(query: dict[str, str]) -> Reply:
        return error_reply(500, "internal_error") if broken["on"] else json_reply(funding_doc(symbol=query["symbol"]))

    fetch = StubFetch(funding_stub_routes(rs, health_funding=HEALTHY, funding={r.symbol: funding for r in rs}))
    shared = Overviewer(fetch, monotonic=clock, min_interval_s=15.0)

    first = shared.get()
    calls = len(fetch.paths)
    clock.now = 14.9
    assert shared.get() is first and len(fetch.paths) == calls
    broken["on"] = True
    clock.now = 15.1
    second = shared.get()

    assert first.status == second.status == 200
    assert all(s["funding_rate"]["state"] == "ok" for s in by_symbol(first).values())
    assert all(s["funding_rate"]["state"] == "unknown" for s in by_symbol(second).values())


# --- bounds -------------------------------------------------------------------------------------------------------------


def test_at_most_four_calls_are_in_flight_and_only_allowed_paths_are_used() -> None:
    rs = rows(12)
    routes = funding_stub_routes(rs, health_funding=HEALTHY, health_open_interest=OI_HEALTHY)
    with FakeApi(routes, delay_s=0.03) as api:  # type: ignore[arg-type]
        snapshot = Overviewer(http_fetch(api.url, timeout_s=5.0), concurrency=4).get()

        assert snapshot.status == 200
        assert 1 < api.max_in_flight <= 4
        assert api.count(FUNDING) == 12 and api.count(OI) == 12 and api.count(CANDLE) == 12
        assert {urlsplit(request).path for request in api.requests} == {
            "/health/ingest",
            "/v1/instruments",
            CANDLE,
            OI,
            FUNDING,
        }
        assert body_of(snapshot)["funding_rate"]["summary"]["ok"] == 12


def test_the_part_keeps_one_small_report_per_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.symbol_overview import judge as judge_module
    from tools.symbol_overview.model import FundingReport

    captured: list[Any] = []
    real = judge_module.build_overview

    def spy(*args: Any, **kwargs: Any) -> dict[str, object]:
        captured.append(kwargs["funding"])
        return real(*args, **kwargs)

    monkeypatch.setattr("tools.symbol_overview.refresh.build_overview", spy)

    run(StubFetch(funding_stub_routes(rows(3), health_funding=HEALTHY)))

    assert len(captured) == 1 and len(captured[0].reports) == 3
    assert all(isinstance(report, FundingReport) for report in captured[0].reports)
