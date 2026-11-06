"""Validation of one page of candles, open interest rows or funding rows for the symbol detail (pure; design 5.2).

The same rule as ``validate.py``: any failure is an ``Invalid`` and the caller makes the whole part an error; nothing
here repairs, converts or guesses. Decimals are checked for the contract's canonical form (C§1) and stay text.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tools.symbol_overview.model import (
    FUNDING_WINDOW_STATES,
    PAGE_LIMIT,
    TF_MS,
    CandleRow,
    FundingRow,
    InstrumentRow,
    Invalid,
    OiRow,
)
from tools.symbol_overview.validate import (
    _aligned_ts,
    _all_or_none_ts,
    _bad,
    _decode,
    _non_negative,
    _ts,
)

_DIGITS = re.compile(r"[0-9]+(\.[0-9]+)?")
_RATE_TYPES = ("Regular", "Special")  # the order in a page (C§6.7.3)


@dataclass(frozen=True, slots=True)
class CandlePage:
    rows: tuple[CandleRow, ...]
    next: int | None
    oldest_ms: int
    newest_ms: int


@dataclass(frozen=True, slots=True)
class OiPage:
    rows: tuple[OiRow, ...]
    next: int | None
    oldest_ms: int
    newest_ms: int


@dataclass(frozen=True, slots=True)
class FundingPage:
    rows: tuple[FundingRow, ...]
    next: int | None
    oldest_ms: int | None
    newest_ms: int | None


def canonical_decimal(value: object, *, signed: bool = False) -> bool:
    """The contract's canonical decimal text (C§1): ASCII digits and at most one ``.``, no exponent, no leading zero
    except ``0.x``, no trailing fractional zero; a leading ``-`` only where ``signed`` (never ``-0``)."""
    if not isinstance(value, str):
        return False
    body = value[1:] if signed and value.startswith("-") else value
    if not _DIGITS.fullmatch(body):
        return False
    integer, _, fraction = body.partition(".")
    if (len(integer) > 1 and integer.startswith("0")) or fraction.endswith("0"):
        return False
    return not (value != body and body == "0")


def _int(value: object) -> bool:
    return type(value) is int


def _head(doc: dict[str, Any], rows_key: str) -> tuple[list[Any], dict[str, Any], object] | Invalid:
    rows, series = doc.get(rows_key), doc.get("series")
    if not (isinstance(rows, list) and isinstance(series, dict) and "next" in doc) or len(rows) > PAGE_LIMIT:
        return _bad("schema")
    return rows, series, doc["next"]


def candle_page_result(
    status: int, content_type: str | None, body: bytes, *, instrument: InstrumentRow, start: int, end: int
) -> CandlePage | Invalid:
    doc = _decode(status, content_type, body)
    if isinstance(doc, Invalid):
        return doc
    head = _head(doc, "candles")
    if isinstance(head, Invalid):
        return head
    raw, series, next_ = head
    oldest, newest = series.get("oldest_open_time_ms"), series.get("newest_open_time_ms")
    strings = (doc.get("venue"), doc.get("symbol"), doc.get("timeframe"))
    if (
        not all(isinstance(s, str) for s in strings)
        or not (_int(doc.get("start")) and _int(doc.get("end")))
        or not (_aligned_ts(oldest) and _aligned_ts(newest))
        or not isinstance(oldest, int)
        or not isinstance(newest, int)
        or oldest > newest
    ):
        return _bad("schema")
    if (*strings, doc["start"], doc["end"]) != (instrument.venue, instrument.symbol, instrument.timeframe, start, end):
        return _bad("echo")

    rows: list[CandleRow] = []
    previous = start - TF_MS
    for item in raw:
        if not isinstance(item, dict):
            return _bad("schema")
        t, conflicted, turnover = item.get("open_time_ms"), item.get("conflicted"), item.get("turnover")
        values = [item.get(key) for key in ("open", "high", "low", "close", "volume")]
        if (
            not _aligned_ts(t)
            or not isinstance(t, int)
            or not start <= t < end
            or t <= previous
            or type(conflicted) is not bool
            or not all(canonical_decimal(v) for v in values)
            or not (turnover is None or canonical_decimal(turnover))
        ):
            return _bad("schema")
        o, h, low, c, v = (str(x) for x in values)
        if Decimal(low) > min(Decimal(o), Decimal(c)) or Decimal(h) < max(Decimal(o), Decimal(c)):
            return _bad("schema", "ohlc")  # an inconsistent candle is not drawn; the whole part says so
        rows.append(CandleRow(t, o, h, low, c, v, turnover, conflicted))
        previous = t
    if rows and not oldest <= rows[0].t <= rows[-1].t <= newest:
        return _bad("schema")
    if next_ is not None and not (_aligned_ts(next_) and rows and isinstance(next_, int) and rows[-1].t < next_ < end):
        return _bad("schema")  # a next without rows, or one that does not move forward, never loops the caller
    return CandlePage(tuple(rows), next_, oldest, newest)


def open_interest_page_result(
    status: int, content_type: str | None, body: bytes, *, instrument: InstrumentRow, start: int, end: int
) -> OiPage | Invalid:
    doc = _decode(status, content_type, body)
    if isinstance(doc, Invalid):
        return doc
    head = _head(doc, "rows")
    if isinstance(head, Invalid):
        return head
    raw, series, next_ = head
    oldest, newest = series.get("oldest_time_ms"), series.get("newest_time_ms")
    strings = (doc.get("venue"), doc.get("symbol"), doc.get("period"))
    if (
        not all(isinstance(s, str) for s in strings)
        or not (_int(doc.get("start")) and _int(doc.get("end")))
        or not (_aligned_ts(oldest) and _aligned_ts(newest))
        or not isinstance(oldest, int)
        or not isinstance(newest, int)
        or oldest > newest
    ):
        return _bad("schema")
    if (*strings, doc["start"], doc["end"], doc.get("time_semantics")) != (
        instrument.venue,
        instrument.symbol,
        instrument.timeframe,
        start,
        end,
        "period_end",
    ):
        return _bad("echo")

    rows: list[OiRow] = []
    previous = start - TF_MS
    for item in raw:
        if not isinstance(item, dict):
            return _bad("schema")
        t, conflicted = item.get("time_ms"), item.get("conflicted")
        oi, value = item.get("open_interest"), item.get("open_interest_value")
        if (
            not _aligned_ts(t)
            or not isinstance(t, int)
            or not start <= t < end
            or t <= previous
            or type(conflicted) is not bool
            or not (canonical_decimal(oi) and canonical_decimal(value))
        ):
            return _bad("schema")
        rows.append(OiRow(t, str(oi), str(value), conflicted))
        previous = t
    if rows and not oldest <= rows[0].t <= rows[-1].t <= newest:
        return _bad("schema")
    if next_ is not None and not (_aligned_ts(next_) and rows and isinstance(next_, int) and rows[-1].t < next_ < end):
        return _bad("schema")
    return OiPage(tuple(rows), next_, oldest, newest)


def funding_page_result(
    status: int, content_type: str | None, body: bytes, *, instrument: InstrumentRow, start: int, end: int
) -> FundingPage | Invalid:
    doc = _decode(status, content_type, body)
    if isinstance(doc, Invalid):
        return doc
    head = _head(doc, "rows")
    if isinstance(head, Invalid):
        return head
    raw, series, next_ = head
    oldest, newest = series.get("oldest_funding_time_ms"), series.get("newest_funding_time_ms")
    block = doc.get("coverage")
    strings = (doc.get("venue"), doc.get("symbol"))
    if (
        not all(isinstance(s, str) for s in strings)
        or not (_int(doc.get("start")) and _int(doc.get("end")))
        or not _all_or_none_ts((oldest, newest))
        or not isinstance(block, dict)
        or block.get("window_state") not in FUNDING_WINDOW_STATES
        or not _non_negative(block.get("conflicted_events"))
        or type(block.get("complete")) is not bool
    ):
        return _bad("schema")
    if (*strings, doc["start"], doc["end"], doc.get("time_semantics")) != (
        instrument.venue,
        instrument.symbol,
        start,
        end,
        "settlement",
    ):
        return _bad("echo")
    oldest_ms = oldest if isinstance(oldest, int) else None
    newest_ms = newest if isinstance(newest, int) else None
    if oldest_ms is not None and newest_ms is not None and oldest_ms > newest_ms:
        return _bad("schema")

    rows: list[FundingRow] = []
    previous: tuple[int, int] = (start - 1, 0)
    for item in raw:
        if not isinstance(item, dict):
            return _bad("schema")
        t, kind, conflicted = item.get("funding_time_ms"), item.get("rate_type"), item.get("conflicted")
        rate, mark = item.get("funding_rate"), item.get("mark_price")
        if (
            not _ts(t)
            or not isinstance(t, int)
            or not start <= t <= end
            or kind not in _RATE_TYPES
            or type(conflicted) is not bool
            or not canonical_decimal(rate, signed=True)
            or not (mark is None or canonical_decimal(mark))
        ):
            return _bad("schema")
        key = (t, _RATE_TYPES.index(str(kind)))
        if key <= previous:
            return _bad("schema")  # ordered and unique by (funding_time_ms, rate_type), Regular first
        rows.append(FundingRow(t, str(kind), str(rate), None if mark is None else str(mark), conflicted))
        previous = key
    if rows and (oldest_ms is None or newest_ms is None or not oldest_ms <= rows[0].t <= rows[-1].t <= newest_ms):
        return _bad("schema")
    if next_ is not None and not (_ts(next_) and rows and isinstance(next_, int) and rows[-1].t < next_ <= end):
        return _bad("schema")  # a page never splits one funding_time_ms, so next is strictly after the last row
    return FundingPage(tuple(rows), next_, oldest_ms, newest_ms)
