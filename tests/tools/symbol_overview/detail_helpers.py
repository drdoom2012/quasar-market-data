"""Builders of the API answers the symbol detail reads (candles, open interest rows, funding rows) and routes for
``FakeApi`` that serve them with the paging rules of the contract (C§3, C§5.1, C§6.5.2, C§6.7.3)."""

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from tests.tools.symbol_overview.fake_api import FakeReply
from tests.tools.symbol_overview.helpers import SYMBOL
from tools.symbol_overview.model import TF_MS, TIMEFRAME, VENUE

Route = Callable[[dict[str, str]], FakeReply]


def reply(doc: object, status: int = 200) -> FakeReply:
    return FakeReply(status, json.dumps(doc).encode())


def body(doc: object) -> bytes:
    return json.dumps(doc).encode()


def api_error(status: int, code: str) -> FakeReply:
    return FakeReply(status, json.dumps({"error": {"code": code, "message": "x", "details": {}}}).encode())


def candle_item(t: int, close: str = "100", *, conflicted: bool = False, **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "open_time_ms": t,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": "1",
        "turnover": None,
        "conflicted": conflicted,
    }
    item.update(fields)
    return item


def candles_doc(
    rows: Sequence[dict[str, Any]],
    *,
    start: int,
    end: int,
    next_: int | None = None,
    symbol: str = SYMBOL,
    oldest: int | None = None,
    newest: int | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    times = [r["open_time_ms"] for r in rows]
    doc: dict[str, Any] = {
        "venue": VENUE,
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "start": start,
        "end": end,
        "series": {
            "oldest_open_time_ms": oldest if oldest is not None else (times[0] if times else start),
            "newest_open_time_ms": newest if newest is not None else (times[-1] if times else start),
        },
        "candles": list(rows),
        "next": next_,
        "coverage": {"slots": (end - start) // TF_MS},
    }
    doc.update(overrides)
    return doc


def oi_item(t: int, value: str = "100", *, conflicted: bool = False) -> dict[str, Any]:
    return {"time_ms": t, "open_interest": "1", "open_interest_value": value, "conflicted": conflicted}


def oi_page_doc(
    rows: Sequence[dict[str, Any]],
    *,
    start: int,
    end: int,
    next_: int | None = None,
    symbol: str = SYMBOL,
    oldest: int | None = None,
    newest: int | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    times = [r["time_ms"] for r in rows]
    doc: dict[str, Any] = {
        "venue": VENUE,
        "symbol": symbol,
        "period": TIMEFRAME,
        "time_semantics": "period_end",
        "start": start,
        "end": end,
        "series": {
            "oldest_time_ms": oldest if oldest is not None else (times[0] if times else start),
            "newest_time_ms": newest if newest is not None else (times[-1] if times else start),
        },
        "rows": list(rows),
        "next": next_,
        "coverage": {"slots": (end - start) // TF_MS},
    }
    doc.update(overrides)
    return doc


def funding_item(
    t: int, rate: str = "0.0001", kind: str = "Regular", mark: str | None = "100", *, conflicted: bool = False
) -> dict[str, Any]:
    return {"funding_time_ms": t, "rate_type": kind, "funding_rate": rate, "mark_price": mark, "conflicted": conflicted}


def funding_page_doc(
    rows: Sequence[dict[str, Any]],
    *,
    start: int,
    end: int,
    next_: int | None = None,
    symbol: str = SYMBOL,
    oldest: int | None = None,
    newest: int | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    times = [r["funding_time_ms"] for r in rows]
    doc: dict[str, Any] = {
        "venue": VENUE,
        "symbol": symbol,
        "time_semantics": "settlement",
        "start": start,
        "end": end,
        "series": {
            "oldest_funding_time_ms": oldest if oldest is not None else (times[0] if times else None),
            "newest_funding_time_ms": newest if newest is not None else (times[-1] if times else None),
        },
        "rows": list(rows),
        "next": next_,
        "coverage": {
            "as_of_ms": end,
            "verified_from_ms": None,
            "verified_through_ms": None,
            "verified_at_ms": None,
            "window_state": "unverified",
            "events_in_window": len(rows),
            "conflicted_events": 0,
            "complete": False,
        },
    }
    doc.update(overrides)
    return doc


# --- routes that serve stored data with the contract's paging ---------------------------------------------------------


def _page(times: Sequence[int], start: int, end: int, limit: int, *, closed: bool) -> tuple[list[int], int | None]:
    inside = [t for t in times if start <= t and (t <= end if closed else t < end)]
    page = inside[:limit]
    return page, (inside[limit] if len(inside) > limit else None)


def candles_route(
    times: Collection[int], *, close: Callable[[int], str] = lambda t: "100", conflicted: Collection[int] = ()
) -> Route:
    stored = sorted(times)

    def route(query: dict[str, str]) -> FakeReply:
        start, end, limit = int(query["start"]), int(query["end"]), int(query["limit"])
        page, next_ = _page(stored, start, end, limit, closed=False)
        rows = [candle_item(t, close(t), conflicted=t in conflicted) for t in page]
        return reply(candles_doc(rows, start=start, end=end, next_=next_, oldest=stored[0], newest=stored[-1]))

    return route


def oi_route(times: Collection[int], *, value: Callable[[int], str] = lambda t: "100") -> Route:
    stored = sorted(times)

    def route(query: dict[str, str]) -> FakeReply:
        start, end, limit = int(query["start"]), int(query["end"]), int(query["limit"])
        page, next_ = _page(stored, start, end, limit, closed=False)
        rows = [oi_item(t, value(t)) for t in page]
        return reply(oi_page_doc(rows, start=start, end=end, next_=next_, oldest=stored[0], newest=stored[-1]))

    return route


def funding_route(items: Mapping[int, str] | Collection[int]) -> Route:
    stored = sorted(items)

    def route(query: dict[str, str]) -> FakeReply:
        start, end, limit = int(query["start"]), int(query["end"]), int(query["limit"])
        page, next_ = _page(stored, start, end, limit, closed=True)
        rows = [funding_item(t) for t in page]
        return reply(funding_page_doc(rows, start=start, end=end, next_=next_, oldest=stored[0], newest=stored[-1]))

    return route
