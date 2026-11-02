"""Strict, all-or-nothing parser for raw Binance USD-M open interest history (ADR-0003 P-1, P-2, P-5).

One response of ``GET /futures/data/openInterestHist`` (CCXT ``fapiDataGetOpenInterestHist``) is a list
of objects with ``symbol``, ``sumOpenInterest``, ``sumOpenInterestValue``, ``CMCCirculatingSupply`` and
``timestamp`` (the period end). The two open interest values are decimal strings and are never
converted from JSON numbers. ``CMCCirculatingSupply`` and any other key are not interpreted.

Any problem raises ``OpenInterestParseError`` and no row is returned: a row outside the requested
``[start_time_ms, end_time_ms]`` (which also catches the row Binance adds for a start inside a period),
an unaligned, duplicated or non-increasing ``timestamp``, a wrong ``symbol``, a value that is not an
unsigned decimal string, more rows than period ends in the range, or a response that is not a list.
Missing rows are not an error.

Binance returns the *newest* ``limit`` rows of an over-long range (P-5). The request side (OI-3) never
asks for more than ``limit`` period ends; this parser only detects a response with more rows than the
range can hold. It does not read a clock or decide eligibility.
"""

from collections.abc import Mapping
from typing import Final

from quasar_market_data.collector.binance_klines import VENUE_BINANCE_USDM
from quasar_market_data.domain.candle import ensure_symbol
from quasar_market_data.domain.open_interest import OpenInterest, OpenInterestKey, period_end_count
from quasar_market_data.domain.timeframe import Timeframe

_REQUIRED_KEYS: Final = ("symbol", "timestamp", "sumOpenInterest", "sumOpenInterestValue")


class OpenInterestParseError(ValueError):
    """A raw open interest response was rejected; ``row_index`` names the offending row when there is one."""

    def __init__(self, message: str, row_index: int | None = None) -> None:
        self.row_index = row_index
        super().__init__(message if row_index is None else f"row {row_index}: {message}")


def _text_slot(row: Mapping[object, object], name: str) -> str:
    value = row[name]
    if type(value) is not str:
        raise TypeError(f"{name} must be a decimal string, got {type(value).__name__}")
    return value


def _parse_row(
    row: object, market_id: str, symbol: str, period: Timeframe, start: int, end: int, previous: int | None
) -> OpenInterest:
    if type(row) is not dict:
        raise TypeError(f"row must be a dict, got {type(row).__name__}")
    for key in _REQUIRED_KEYS:
        if key not in row:
            raise ValueError(f"missing key {key!r}")
    if row["symbol"] != market_id:
        raise ValueError(f"symbol {row['symbol']!r} does not match market_id {market_id!r}")
    timestamp = row["timestamp"]
    if type(timestamp) is not int:
        raise TypeError(f"timestamp must be a JSON integer, got {type(timestamp).__name__}")
    if timestamp < start:
        raise ValueError(f"timestamp {timestamp} is before start_time_ms {start}")
    if timestamp > end:
        raise ValueError(f"timestamp {timestamp} is after end_time_ms {end}")
    if previous is not None and timestamp <= previous:
        raise ValueError(f"timestamp {timestamp} is not increasing after previous {previous}")
    return OpenInterest(
        key=OpenInterestKey(VENUE_BINANCE_USDM, symbol, period, timestamp),
        open_interest=_text_slot(row, "sumOpenInterest"),
        open_interest_value=_text_slot(row, "sumOpenInterestValue"),
    )


def parse_open_interest_hist(
    response: object, *, market_id: str, symbol: str, period: Timeframe, start_time_ms: int, end_time_ms: int
) -> tuple[OpenInterest, ...]:
    """Parse one response for the range ``[start_time_ms, end_time_ms]``, or raise with no partial result.

    Argument errors are caller bugs and raise plain ``TypeError``/``ValueError`` before the response is read.
    """
    if type(market_id) is not str:
        raise TypeError(f"market_id must be str, got {type(market_id).__name__}")
    if not market_id:
        raise ValueError("market_id must not be empty")
    ensure_symbol(symbol)
    slots = period_end_count(start_time_ms, end_time_ms, period)

    if type(response) is not list:
        raise OpenInterestParseError(f"response must be a list, got {type(response).__name__}")
    if len(response) > slots:
        raise OpenInterestParseError(f"{len(response)} rows for {slots} period ends in the requested range")

    rows: list[OpenInterest] = []
    for index, row in enumerate(response):
        previous = rows[-1].key.time_ms if rows else None
        try:
            rows.append(_parse_row(row, market_id, symbol, period, start_time_ms, end_time_ms, previous))
        except (TypeError, ValueError) as error:
            raise OpenInterestParseError(str(error), index) from error
    return tuple(rows)
