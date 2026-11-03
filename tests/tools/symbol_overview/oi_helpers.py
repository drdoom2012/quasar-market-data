"""Shared constants and builders for the open interest tests of the symbol overview tool (OI-6b)."""

import json
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from quasar_market_data.domain.open_interest_coverage import OpenInterestRule, classify_open_interest_runs
from quasar_market_data.domain.timeframe import Timeframe
from tests.tools.symbol_overview.fake_api import FakeReply
from tests.tools.symbol_overview.helpers import RULE, SYMBOL, T0, coverage_doc, health_doc
from tools.symbol_overview.fetch import RawResponse, TransportError
from tools.symbol_overview.model import TF_MS, TIMEFRAME, VENUE, InstrumentRow, OiHealth, OiRule, Window
from tools.symbol_overview.window import open_interest_window, window_for

WINDOW = window_for(T0)  # the candle window [S, E)
OI_WINDOW = open_interest_window(WINDOW)  # [S + tf, E + tf): slot T is a period END
S1 = OI_WINDOW.start  # S'
E1 = OI_WINDOW.end  # E'


def slot(n: int) -> int:
    """The ``n``-th slot of the open interest window (``S' + n x tf``)."""
    return S1 + n * TF_MS


# --- the health object and the answers ------------------------------------------------------------------------------


# --- the health object and the answers ------------------------------------------------------------------------------

ABSENT = object()  # the key ``open_interest`` is not in the health body at all
EXPECTED_AFTER_MS = 500_000
HORIZON_MS = 2_505_600_000  # 29 days
OI_RULE = OiRule(1, EXPECTED_AFTER_MS, 60_000)
OI_HEALTH = OiHealth(OI_RULE)
COUNT_KEYS = (
    "conflicted_slots",
    "not_yet_expected_slots",
    "no_longer_recoverable_slots",
    "due_slots",
    "stale_slots",
    "missing_slots",
)
STATES = ("present", "conflicted", "not_yet_expected", "no_longer_recoverable", "due", "stale", "missing")


def health_object(*, as_of_ms: int = T0, rule: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``health.open_interest`` as contract 6.5.8 shows it (the fields the tool does not read are included)."""
    return {
        "as_of_ms": as_of_ms,
        "rule": dict(rule)
        if rule is not None
        else {"cadence": 1, "expected_after_ms": EXPECTED_AFTER_MS, "lag_threshold_ms": 60_000},
        "scope": "stored_series",
        "status": "current",
        "series_count": 1,
        "current": 1,
        "due": 0,
        "stale": 0,
        "unjudged_series": 0,
        "conflicted_series": 0,
        "behind": [],
        "truncated": False,
    }


def health_with(open_interest: object = ABSENT, *, as_of_ms: int = T0, rule: Any = RULE) -> bytes:
    doc = health_doc(as_of_ms, rule)
    if open_interest is not ABSENT:
        doc["open_interest"] = open_interest
    return json.dumps(doc).encode()


def _body(
    *,
    window: Window,
    symbol: str,
    as_of_ms: int,
    horizon: int,
    oldest: int,
    newest: int,
    counts: Mapping[str, int],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    slots = (window.end - window.start) // TF_MS
    return {
        "venue": VENUE,
        "symbol": symbol,
        "period": TIMEFRAME,
        "time_semantics": "period_end",
        "start": window.start,
        "end": window.end,
        "as_of_ms": as_of_ms,
        "rule": {
            "cadence": 1,
            "expected_after_ms": EXPECTED_AFTER_MS,
            "lag_threshold_ms": 60_000,
            "retrieval_horizon_ms": horizon,
        },
        "series": {"oldest_time_ms": oldest, "newest_time_ms": newest},
        "coverage": {
            "slots": slots,
            "present_slots": counts["present"],
            "conflicted_slots": counts["conflicted"],
            "not_yet_expected_slots": counts["not_yet_expected"],
            "no_longer_recoverable_slots": counts["no_longer_recoverable"],
            "due_slots": counts["due"],
            "stale_slots": counts["stale"],
            "missing_slots": counts["missing"],
            "gap_free": counts["present"] == slots,
        },
        "runs": runs,
    }


def api_doc(
    *,
    rows: Collection[int],
    conflicts: Collection[int] = (),
    as_of_ms: int = T0 + 1000,
    horizon: int = HORIZON_MS,
    window: Window = OI_WINDOW,
    symbol: str = SYMBOL,
) -> dict[str, Any]:
    """The body the API produces for a series with these ``rows``: computed by the API's own pure function."""
    rule = OpenInterestRule(
        cadence=1,
        expected_after_ms=EXPECTED_AFTER_MS,
        lag_threshold_ms=60_000,
        period=Timeframe("5m"),
        retrieval_horizon_ms=horizon,
    )
    newest, oldest = max(rows), min(rows)
    result = classify_open_interest_runs(
        start=window.start,
        end=window.end,
        stored={t for t in rows if window.start <= t < window.end},
        conflicts={t for t in conflicts if window.start <= t < window.end},
        newest_time_ms=newest,
        as_of_ms=as_of_ms,
        rule=rule,
    )
    c = result.coverage
    counts = {
        "present": c.present,
        "conflicted": c.conflicted,
        "not_yet_expected": c.not_yet_expected,
        "no_longer_recoverable": c.no_longer_recoverable,
        "due": c.due,
        "stale": c.stale,
        "missing": c.missing,
    }
    runs = [
        {"state": r.state.value, "start": r.start_ms, "end": r.end_ms, "slots": (r.end_ms - r.start_ms) // TF_MS}
        for r in result.runs
    ]
    return _body(
        window=window,
        symbol=symbol,
        as_of_ms=as_of_ms,
        horizon=horizon,
        oldest=oldest,
        newest=newest,
        counts=counts,
        runs=runs,
    )


def doc_from_states(
    *,
    oldest: int,
    newest: int,
    states: Mapping[int, str] | None = None,
    as_of_ms: int = T0 + 1000,
    horizon: int = HORIZON_MS,
    window: Window = OI_WINDOW,
    symbol: str = SYMBOL,
) -> dict[str, Any]:
    """A self-consistent body built slot by slot, so a test can state exactly one contradiction.

    Slots before ``oldest`` are ``missing``, slots in ``[oldest, newest]`` are ``present``, slots after ``newest``
    are ``not_yet_expected``; ``states`` overrides single slots, even into a state the API would never produce.
    Counts and maximal runs are derived (touching slots of one state form one run).
    """
    overrides = states or {}
    counts = dict.fromkeys(STATES, 0)
    runs: list[dict[str, Any]] = []
    for t in range(window.start, window.end, TF_MS):
        if t in overrides:
            state = overrides[t]
        elif t < oldest:
            state = "missing"
        elif t <= newest:
            state = "present"
        else:
            state = "not_yet_expected"
        counts[state] += 1
        if state == "present":
            continue
        if runs and runs[-1]["state"] == state and runs[-1]["end"] == t:
            runs[-1]["end"] = t + TF_MS
            runs[-1]["slots"] += 1
        else:
            runs.append({"state": state, "start": t, "end": t + TF_MS, "slots": 1})
    return _body(
        window=window,
        symbol=symbol,
        as_of_ms=as_of_ms,
        horizon=horizon,
        oldest=oldest,
        newest=newest,
        counts=counts,
        runs=runs,
    )


# --- a fake API for the refresh tests --------------------------------------------------------------------------------

Reply = FakeReply | TransportError
OiRoute = Callable[[dict[str, str]], Reply]


def json_reply(doc: object) -> FakeReply:
    return FakeReply(200, json.dumps(doc).encode())


def error_reply(status: int, code: str) -> FakeReply:
    return FakeReply(status, json.dumps({"error": {"code": code, "message": "x", "details": {}}}).encode())


def default_open_interest_doc(query: dict[str, str]) -> dict[str, Any]:
    """A healthy answer for the requested window: rows from its first slot up to near its end."""
    window = Window(int(query["start"]), int(query["end"]))
    return doc_from_states(oldest=window.start, newest=window.end - 10 * TF_MS, symbol=query["symbol"], window=window)


def stub_routes(
    rows: Sequence[InstrumentRow],
    *,
    health_open_interest: object = ABSENT,
    candle: Mapping[str, Reply] | None = None,
    open_interest: Mapping[str, Reply | Callable[[dict[str, str]], Reply]] | None = None,
) -> dict[str, OiRoute]:
    """Consistent routes: both coverage answers are built from the request, so the echo always matches."""
    by_symbol = {r.symbol: r for r in rows}

    def coverage(query: dict[str, str]) -> Reply:
        symbol = query["symbol"]
        if candle and symbol in candle:
            return candle[symbol]
        window = Window(int(query["start"]), int(query["end"]))
        newest = by_symbol[symbol].newest_open_time_ms
        assert newest is not None
        return json_reply(coverage_doc(window, oldest=window.start, newest=newest, symbol=symbol))

    def oi(query: dict[str, str]) -> Reply:
        symbol = query["symbol"]
        override = (open_interest or {}).get(symbol)
        if override is not None:
            return override(query) if callable(override) else override
        if by_symbol[symbol].newest_open_time_ms is None:
            return error_reply(404, "unknown_series")
        return json_reply(default_open_interest_doc(query))

    return {
        "/health/ingest": lambda query: FakeReply(200, health_with(health_open_interest)),
        "/v1/instruments": lambda query: json_reply(
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
        ),
        "/v1/coverage": coverage,
        "/v1/open-interest/coverage": oi,
    }


class StubFetch:
    """A ``Fetch`` without sockets: canned answers by path, every call recorded, a hook that may move a fake clock."""

    def __init__(self, routes: Mapping[str, OiRoute], *, on_call: Callable[[str], None] | None = None) -> None:
        self.routes = routes
        self.on_call = on_call
        self.paths: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, path_and_query: str) -> RawResponse:
        with self._lock:
            self.paths.append(path_and_query)
        if self.on_call is not None:
            self.on_call(path_and_query)
        split = urlsplit(path_and_query)
        route = self.routes.get(split.path)
        reply: Reply = route(dict(parse_qsl(split.query, keep_blank_values=True))) if route else FakeReply(404, b"{}")
        if isinstance(reply, TransportError):
            raise reply
        return RawResponse(reply.status, reply.content_type, reply.body)

    def count(self, path: str) -> int:
        return sum(1 for p in self.paths if urlsplit(p).path == path)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now
