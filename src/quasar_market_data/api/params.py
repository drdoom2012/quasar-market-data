"""Raw query string -> ``CandlesRequest`` / ``CoverageRequest`` (contract sections 1, 5.1 and 6.1.1), strict and free of I/O."""

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl

from quasar_market_data.api.errors import ApiError, cut_received, invalid_parameter
from quasar_market_data.domain.candle import MAX_SYMBOL_LENGTH
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, MIN_TIMESTAMP_MS

SERVED_VENUE: Final = "binance-usdm"
SERVED_TIMEFRAME: Final = Timeframe("5m")
MAX_LIMIT: Final = 1000
DEFAULT_LIMIT: Final = 1000
MAX_WINDOW_SLOTS: Final = 10_000

# An explicit ASCII class and no flags: `\d` would match other Unicode digits, and `fullmatch` (not `$`)
# rejects a trailing newline. See the contract: integer parameters match ^[0-9]{1,16}$.
INTEGER_PATTERN: Final = re.compile(r"[0-9]{1,16}")

_NAMES: Final = ("venue", "symbol", "timeframe", "start", "end", "limit")
_REQUIRED: Final = ("venue", "symbol", "timeframe", "start")
_COVERAGE_NAMES: Final = ("venue", "symbol", "timeframe", "start", "end")
_COVERAGE_REQUIRED: Final = ("venue", "symbol", "timeframe", "start", "end")
_MALFORMED_ESCAPE: Final = re.compile(r"%(?![0-9A-Fa-f]{2})")


@dataclass(frozen=True)
class CandlesRequest:
    venue: str
    symbol: str
    timeframe: Timeframe
    start: int
    end: int | None
    limit: int


@dataclass(frozen=True)
class CoverageRequest:
    venue: str
    symbol: str
    timeframe: Timeframe
    start: int
    end: int


def parse_candles_request(raw_query: bytes) -> CandlesRequest:
    values = _parse(raw_query, _NAMES, _REQUIRED)
    venue, symbol, start = _series_and_start(values)
    end = _end(values)
    limit = _limit(values["limit"]) if "limit" in values else DEFAULT_LIMIT
    if end is not None:
        _check_window(start, end)

    return CandlesRequest(venue, symbol, SERVED_TIMEFRAME, start, end, limit)


def parse_coverage_request(raw_query: bytes) -> CoverageRequest:
    values = _parse(raw_query, _COVERAGE_NAMES, _COVERAGE_REQUIRED)
    venue, symbol, start = _series_and_start(values)
    end = _timestamp("end", values["end"])
    _check_window(start, end)

    return CoverageRequest(venue, symbol, SERVED_TIMEFRAME, start, end)


def _parse(raw_query: bytes, names: tuple[str, ...], required: tuple[str, ...]) -> dict[str, str]:
    """Decode the query and apply the shared rules: unknown, repeated, empty, then missing."""
    values: dict[str, str] = {}
    for name, value in _decode(raw_query):
        if name not in names:
            raise invalid_parameter("unknown parameter", parameter=cut_received(name), received=value)
        if name in values:
            raise invalid_parameter(f"{name} must not be repeated", parameter=name, received=value)
        if value == "":
            raise invalid_parameter(f"{name} must not be empty", parameter=name, received="")
        values[name] = value

    for name in required:
        if name not in values:
            raise invalid_parameter(f"{name} is required", parameter=name)
    return values


def _series_and_start(values: dict[str, str]) -> tuple[str, str, int]:
    """Validate venue, symbol, timeframe and start, in that order; the timeframe is not returned (always served)."""
    venue = values["venue"]
    if venue != SERVED_VENUE:
        raise invalid_parameter(f"venue must be {SERVED_VENUE}", parameter="venue", received=venue)

    symbol = values["symbol"]
    if not 1 <= len(symbol) <= MAX_SYMBOL_LENGTH:
        raise invalid_parameter(f"symbol must be 1-{MAX_SYMBOL_LENGTH} characters", parameter="symbol", received=symbol)
    # The same rule as CandleKey, so the 400/404 boundary equals what the writer can store.
    if any(ch.isspace() or not ch.isprintable() for ch in symbol):
        raise invalid_parameter(
            "symbol must not contain whitespace or control characters", parameter="symbol", received=symbol
        )

    if values["timeframe"] != SERVED_TIMEFRAME.label:
        raise invalid_parameter(
            f"timeframe must be {SERVED_TIMEFRAME.label}", parameter="timeframe", received=values["timeframe"]
        )

    return venue, symbol, _timestamp("start", values["start"])


def _end(values: dict[str, str]) -> int | None:
    return _timestamp("end", values["end"]) if "end" in values else None


def _check_window(start: int, end: int) -> None:
    if end <= start:
        raise invalid_parameter("end must be greater than start", parameter="end", received=str(end))
    if (end - start) // SERVED_TIMEFRAME.milliseconds > MAX_WINDOW_SLOTS:
        raise invalid_parameter(
            f"the window must not exceed {MAX_WINDOW_SLOTS} slots",
            parameter="end",
            received=str(end),
            maximum=MAX_WINDOW_SLOTS,
        )


def _decode(raw_query: bytes) -> list[tuple[str, str]]:
    """Decode the query string; every failure is a 400 without ``parameter`` (the name may be the broken part)."""
    try:
        text = raw_query.decode("ascii")
    except UnicodeDecodeError:
        raise _undecodable(raw_query, "the query string must be ASCII; percent-encode other characters") from None
    if _MALFORMED_ESCAPE.search(text):
        raise _undecodable(raw_query, "the query string contains a malformed percent-escape")
    try:
        return parse_qsl(text, keep_blank_values=True, strict_parsing=False, errors="strict")
    except UnicodeDecodeError:
        raise _undecodable(raw_query, "the query string is not valid UTF-8 after percent-decoding") from None


def _undecodable(raw_query: bytes, message: str) -> ApiError:
    return invalid_parameter(message, received=raw_query.decode("ascii", errors="replace"))


def _integer(name: str, text: str) -> int:
    if INTEGER_PATTERN.fullmatch(text) is None:
        raise invalid_parameter(f"{name} must be an integer of 1-16 ASCII digits", parameter=name, received=text)
    return int(text)


def _timestamp(name: str, text: str) -> int:
    value = _integer(name, text)
    if not MIN_TIMESTAMP_MS <= value <= MAX_TIMESTAMP_MS:
        raise invalid_parameter(
            f"{name} must be within [{MIN_TIMESTAMP_MS}, {MAX_TIMESTAMP_MS}]", parameter=name, received=text
        )
    if value % SERVED_TIMEFRAME.milliseconds != 0:
        raise invalid_parameter(f"{name} must be aligned to the timeframe", parameter=name, received=text)
    return value


def _limit(text: str) -> int:
    value = _integer("limit", text)
    if not 1 <= value <= MAX_LIMIT:
        raise invalid_parameter(
            f"limit must be between 1 and {MAX_LIMIT}", parameter="limit", received=text, maximum=MAX_LIMIT
        )
    return value
