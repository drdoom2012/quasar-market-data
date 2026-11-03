"""Strict, all-or-nothing parser for raw Binance USD-M funding rate history (ADR-0004 P-1, P-2, P-3, P-5).

One response of ``GET /fapi/v1/fundingRate`` (CCXT ``fapiPublicGetFundingRate``) is a list of objects with
``symbol``, ``fundingTime`` (integer milliseconds), ``fundingRate`` (decimal string), ``markPrice``
(decimal string, or ``""`` for records settled before 2023-10-31) and ``rateType`` (``Regular`` or
``Special``). Values are never converted from JSON numbers; any other key is ignored.

Any problem raises ``FundingRateParseError`` and no row is returned: a response that is not a list (this
includes the HTTP-200 error object Binance sends for an invalid request), more rows than ``limit``, a row
outside the requested ``[start_time_ms, end_time_ms]``, a ``fundingTime`` that decreases, a repeated
``(fundingTime, rateType)`` key, a wrong ``symbol``, an unknown ``rateType``, or a malformed rate or mark
price. A Regular and a Special row of one ``fundingTime`` are accepted in either order. An empty list is a
valid answer. Hour alignment of ``fundingTime`` is a heuristic that is only counted, never a rejection
(P-3); the range check already catches a unit error. The parser does not read a clock or decide
eligibility, and it does not check that the request followed the request rules (FR-3).
"""

from collections.abc import Mapping
from typing import Final

from quasar_market_data.collector.binance_klines import VENUE_BINANCE_USDM
from quasar_market_data.domain.candle import ensure_symbol
from quasar_market_data.domain.funding_rate import (
    FUNDING_RATE_PAGE_LIMIT,
    FundingRate,
    FundingRateKey,
    RateType,
)
from quasar_market_data.domain.timestamps import ensure_timestamp_ms

_REQUIRED_KEYS: Final = ("symbol", "fundingTime", "fundingRate", "markPrice", "rateType")


class FundingRateParseError(ValueError):
    """A raw funding rate response was rejected; ``row_index`` names the offending row when there is one."""

    def __init__(self, message: str, row_index: int | None = None) -> None:
        self.row_index = row_index
        super().__init__(message if row_index is None else f"row {row_index}: {message}")


def _decimal_slot(row: Mapping[object, object], name: str) -> str:
    value = row[name]
    if type(value) is not str:
        raise TypeError(f"{name} must be a decimal string, got {type(value).__name__}")
    return value


def _parse_row(
    row: object,
    market_id: str,
    symbol: str,
    start: int,
    end: int,
    previous_time: int | None,
    seen_at_previous: frozenset[RateType],
) -> FundingRate:
    if type(row) is not dict:
        raise TypeError(f"row must be a dict, got {type(row).__name__}")
    for key in _REQUIRED_KEYS:
        if key not in row:
            raise ValueError(f"missing key {key!r}")
    if row["symbol"] != market_id:
        raise ValueError(f"symbol {row['symbol']!r} does not match market_id {market_id!r}")
    funding_time = row["fundingTime"]
    if type(funding_time) is not int:
        raise TypeError(f"fundingTime must be a JSON integer, got {type(funding_time).__name__}")
    if funding_time < start:
        raise ValueError(f"fundingTime {funding_time} is before start_time_ms {start}")
    if funding_time > end:
        raise ValueError(f"fundingTime {funding_time} is after end_time_ms {end}")
    raw_type = row["rateType"]
    if type(raw_type) is not str or raw_type not in (RateType.REGULAR.value, RateType.SPECIAL.value):
        raise ValueError(f"rateType must be Regular or Special, got {raw_type!r}")
    rate_type = RateType(raw_type)
    if previous_time is not None:
        if funding_time < previous_time:
            raise ValueError(
                f"fundingTime {funding_time} is below the previous {previous_time}; rows must be ascending"
            )
        if funding_time == previous_time and rate_type in seen_at_previous:
            raise ValueError(f"key ({funding_time}, {raw_type}) is repeated")
    funding_rate = _decimal_slot(row, "fundingRate")
    mark_price = _decimal_slot(row, "markPrice")
    return FundingRate(
        key=FundingRateKey(VENUE_BINANCE_USDM, symbol, funding_time, rate_type),
        funding_rate=funding_rate,
        mark_price=None if mark_price == "" else mark_price,
    )


def parse_funding_rate_history(
    response: object,
    *,
    market_id: str,
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
    limit: int = FUNDING_RATE_PAGE_LIMIT,
) -> tuple[FundingRate, ...]:
    """Parse one response for the range ``[start_time_ms, end_time_ms]``, or raise with no partial result.

    Argument errors are caller bugs and raise plain ``TypeError``/``ValueError`` before the response is read.
    """
    if type(market_id) is not str:
        raise TypeError(f"market_id must be str, got {type(market_id).__name__}")
    if not market_id:
        raise ValueError("market_id must not be empty")
    ensure_symbol(symbol)
    start = ensure_timestamp_ms(start_time_ms, "start_time_ms")
    end = ensure_timestamp_ms(end_time_ms, "end_time_ms")
    if start > end:
        raise ValueError(f"start_time_ms {start} is after end_time_ms {end}")
    if type(limit) is not int:
        raise TypeError(f"limit must be int, got {type(limit).__name__}")
    if not 1 <= limit <= FUNDING_RATE_PAGE_LIMIT:
        raise ValueError(f"limit must be within [1, {FUNDING_RATE_PAGE_LIMIT}], got {limit}")

    if type(response) is not list:
        raise FundingRateParseError(f"response must be a list, got {type(response).__name__}")
    if len(response) > limit:
        raise FundingRateParseError(f"{len(response)} rows is more than limit {limit}")

    rows: list[FundingRate] = []
    previous_time: int | None = None
    seen_at_previous: frozenset[RateType] = frozenset()
    for index, raw_row in enumerate(response):
        try:
            parsed = _parse_row(raw_row, market_id, symbol, start, end, previous_time, seen_at_previous)
        except (TypeError, ValueError) as error:
            raise FundingRateParseError(str(error), index) from error
        time = parsed.key.funding_time_ms
        seen_at_previous = (
            (seen_at_previous | {parsed.key.rate_type}) if time == previous_time else frozenset({parsed.key.rate_type})
        )
        previous_time = time
        rows.append(parsed)
    return tuple(rows)
