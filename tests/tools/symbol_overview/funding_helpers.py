"""Shared constants and builders for the funding tests of the symbol overview tool (FR-6)."""

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from tests.tools.symbol_overview.fake_api import FakeReply
from tests.tools.symbol_overview.helpers import RULE, SYMBOL, T0, health_doc, instrument
from tests.tools.symbol_overview.oi_helpers import (
    ABSENT,
    OiRoute,
    Reply,
    StubFetch,
    error_reply,
    json_reply,
    stub_routes,
)
from tools.symbol_overview.model import (
    DAY_MS,
    TIMEFRAME,
    VENUE,
    FundingHealth,
    FundingRule,
    FundingWindow,
    InstrumentRow,
)
from tools.symbol_overview.window import funding_window, window_for

PUBLICATION_DELAY_MS = 30_000
BEHIND_AFTER_MS = PUBLICATION_DELAY_MS + 7_200_000
QUIET_AFTER_MS = 172_800_000
BACKFILL_DAYS = 30
RULE_DOC = {
    "publication_delay_ms": PUBLICATION_DELAY_MS,
    "behind_after_ms": BEHIND_AFTER_MS,
    "quiet_after_ms": QUIET_AFTER_MS,
    "backfill_days": BACKFILL_DAYS,
}
FUNDING_RULE = FundingRule(PUBLICATION_DELAY_MS, BEHIND_AFTER_MS, QUIET_AFTER_MS, BACKFILL_DAYS)
WINDOW = window_for(T0)
FWINDOW = funding_window(WINDOW, FUNDING_RULE, T0)  # [S, T0 - behind_after_ms + 1]
START, END = FWINDOW.start, FWINDOW.end
HEALTHY_FROM = START - 10 * DAY_MS  # a span that began before the window
FRESH_THROUGH = T0 - 3_600_000  # a span that ends an hour ago: not behind
NEWEST = T0 - 3_600_000 - 5  # the newest settlement, a few ms after an hour mark


def health_object(*, as_of_ms: int = T0, rule: Mapping[str, Any] | None = None, **counts: Any) -> dict[str, Any]:
    """``health.funding_rate`` as contract 6.7.8 shows it."""
    body: dict[str, Any] = {
        "as_of_ms": as_of_ms,
        "rule": dict(rule) if rule is not None else dict(RULE_DOC),
        "scope": "stored_series",
        "status": "current",
        "series_count": 1,
        "current": 1,
        "behind": 0,
        "catching_up": 0,
        "conflicted_series": 0,
        "quiet_series": 0,
    }
    body.update(counts)
    return body


def health_body(funding_rate: object = ABSENT, *, open_interest: object = ABSENT, as_of_ms: int = T0) -> bytes:
    """A ``/health/ingest`` body; the keys that are ABSENT are not in it at all."""
    doc = health_doc(as_of_ms, RULE)
    if open_interest is not ABSENT:
        doc["open_interest"] = open_interest
    if funding_rate is not ABSENT:
        doc["funding_rate"] = funding_rate
    return json.dumps(doc).encode()


def parsed_health(funding_rate: object = ABSENT) -> FundingHealth:
    return FundingHealth(FUNDING_RULE, "current", 1, 1, 0, 0, 0, 0)


def derived_state(window: FundingWindow, span: tuple[int, int] | None) -> str:
    if span is None or window.end < span[0] or window.start > span[1]:
        return "unverified"
    return "verified" if span[0] <= window.start and window.end <= span[1] else "partly_verified"


def funding_doc(
    *,
    window: FundingWindow = FWINDOW,
    symbol: str = SYMBOL,
    span: tuple[int, int] | None = (HEALTHY_FROM, FRESH_THROUGH),
    verified_at: int | None = T0 - 60_000,
    oldest: int | None = HEALTHY_FROM + 1,
    newest: int | None = NEWEST,
    events: int | None = None,
    conflicted: int = 0,
    as_of_ms: int = T0 + 1000,
    rows: Sequence[Any] = (),
    **overrides: Any,
) -> dict[str, Any]:
    """A self-consistent ``GET /v1/funding-rates`` body; ``overrides`` replace top-level or ``coverage`` keys.

    ``window_state`` and ``complete`` are derived as the contract defines them, so a test states one contradiction
    by overriding exactly one value (``coverage={...}`` merges into the coverage block).
    """
    state = derived_state(window, span)
    block: dict[str, Any] = {
        "as_of_ms": as_of_ms,
        "verified_from_ms": span[0] if span else None,
        "verified_through_ms": span[1] if span else None,
        "verified_at_ms": verified_at if span else None,
        "window_state": state,
        "events_in_window": (3 if newest is not None else 0) if events is None else events,
        "conflicted_events": conflicted,
        "complete": state == "verified" and conflicted == 0,
    }
    block.update(overrides.pop("coverage", {}))
    doc: dict[str, Any] = {
        "venue": VENUE,
        "symbol": symbol,
        "time_semantics": "settlement",
        "start": window.start,
        "end": window.end,
        "series": {"oldest_funding_time_ms": oldest, "newest_funding_time_ms": newest},
        "rows": list(rows),
        "next": None,
        "coverage": block,
    }
    doc.update(overrides)
    return doc


def body_of(doc: object) -> bytes:
    return json.dumps(doc).encode()


# --- a stub API for the refresh tests ---------------------------------------------------------------------------------

FundingRoute = Callable[[dict[str, str]], Reply]


def default_funding_doc(query: dict[str, str]) -> dict[str, Any]:
    """A healthy answer for the requested window."""
    return funding_doc(window=FundingWindow(int(query["start"]), int(query["end"])), symbol=query["symbol"])


def funding_stub_routes(
    rows: Sequence[InstrumentRow],
    *,
    health_funding: object = ABSENT,
    health_open_interest: object = ABSENT,
    funding: Mapping[str, Reply | FundingRoute] | None = None,
    **kwargs: Any,
) -> dict[str, OiRoute]:
    """The routes of ``stub_routes`` with a health body that carries the funding key and a funding route."""
    routes = stub_routes(rows, health_open_interest=health_open_interest, **kwargs)
    by_symbol = {r.symbol: r for r in rows}

    def fund(query: dict[str, str]) -> Reply:
        symbol = query["symbol"]
        override = (funding or {}).get(symbol)
        if override is not None:
            return override(query) if callable(override) else override
        if by_symbol[symbol].newest_open_time_ms is None:
            return error_reply(404, "unknown_series")
        return json_reply(default_funding_doc(query))

    routes["/health/ingest"] = lambda query: FakeReply(
        200, health_body(health_funding, open_interest=health_open_interest)
    )
    routes["/v1/funding-rates"] = fund
    return routes


__all__ = [
    "ABSENT",
    "BEHIND_AFTER_MS",
    "FRESH_THROUGH",
    "FUNDING_RULE",
    "FWINDOW",
    "SYMBOL",
    "TIMEFRAME",
    "StubFetch",
    "instrument",
]
