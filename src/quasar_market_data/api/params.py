"""Raw query string -> request objects (contract sections 1, 5.1, 6.1.1, 6.3.1 and 6.5.2), strict and free of I/O."""

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, unquote

from quasar_market_data.api.errors import RECEIVED_MAX_CHARS, ApiError, cut_received, invalid_parameter
from quasar_market_data.domain.candle import MAX_SYMBOL_LENGTH
from quasar_market_data.domain.indicators import IndicatorSpec, IndicatorSyntaxError, parse_indicators
from quasar_market_data.domain.schedule import newest_expected_open_ms
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, MIN_TIMESTAMP_MS

SERVED_VENUE: Final = "binance-usdm"
SERVED_TIMEFRAME: Final = Timeframe("5m")
MAX_LIMIT: Final = 1000
DEFAULT_LIMIT: Final = 1000
MAX_WINDOW_SLOTS: Final = 10_000
MAX_INDICATOR_WINDOW_SLOTS: Final = 2016

# An explicit ASCII class and no flags: `\d` would match other Unicode digits, and `fullmatch` (not `$`)
# rejects a trailing newline. See the contract: integer parameters match ^[0-9]{1,16}$.
INTEGER_PATTERN: Final = re.compile(r"[0-9]{1,16}")

_NAMES: Final = ("venue", "symbol", "timeframe", "start", "end", "limit")
_REQUIRED: Final = ("venue", "symbol", "timeframe", "start")
_COVERAGE_NAMES: Final = ("venue", "symbol", "timeframe", "start", "end")
_COVERAGE_REQUIRED: Final = ("venue", "symbol", "timeframe", "start", "end")
_INDICATORS_NAMES: Final = ("venue", "symbol", "timeframe", "start", "end", "indicators")
_INDICATORS_REQUIRED: Final = ("venue", "symbol", "timeframe", "start", "indicators")
_OPEN_INTEREST_NAMES: Final = ("venue", "symbol", "period", "start", "end", "limit")
_OPEN_INTEREST_REQUIRED: Final = ("venue", "symbol", "period", "start")
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


@dataclass(frozen=True)
class IndicatorsRequest:
    venue: str
    symbol: str
    timeframe: Timeframe
    start: int
    end: int | None
    indicators: tuple[IndicatorSpec, ...]


@dataclass(frozen=True)
class OpenInterestRequest:
    venue: str
    symbol: str
    period: Timeframe
    start: int
    end: int | None
    limit: int


def parse_candles_request(raw_query: bytes) -> CandlesRequest:
    values = _parse(raw_query, _NAMES, _REQUIRED)
    venue, symbol, start = _series_and_start(values)
    end = _end(values)
    limit = _limit(values["limit"]) if "limit" in values else DEFAULT_LIMIT
    if end is not None:
        _check_window(start, end)

    return CandlesRequest(venue, symbol, SERVED_TIMEFRAME, start, end, limit)


def parse_open_interest_request(raw_query: bytes) -> OpenInterestRequest:
    values = _parse(raw_query, _OPEN_INTEREST_NAMES, _OPEN_INTEREST_REQUIRED)
    venue, symbol, start = _series_and_start(values, "period")
    end = _end(values)
    limit = _limit(values["limit"]) if "limit" in values else DEFAULT_LIMIT
    if end is not None:
        _check_window(start, end)

    return OpenInterestRequest(venue, symbol, SERVED_TIMEFRAME, start, end, limit)


def parse_coverage_request(raw_query: bytes) -> CoverageRequest:
    values = _parse(raw_query, _COVERAGE_NAMES, _COVERAGE_REQUIRED)
    venue, symbol, start = _series_and_start(values)
    end = _end_timestamp(values["end"])
    _check_window(start, end)

    return CoverageRequest(venue, symbol, SERVED_TIMEFRAME, start, end)


def parse_indicators_request(raw_query: bytes) -> IndicatorsRequest:
    values = _parse(raw_query, _INDICATORS_NAMES, _INDICATORS_REQUIRED)
    venue, symbol, start = _series_and_start(values)
    end = _end(values)
    if end is not None:
        _check_window(start, end, MAX_INDICATOR_WINDOW_SLOTS)
    text = values["indicators"]
    try:
        indicators = parse_indicators(text)
    except IndicatorSyntaxError as error:
        raise invalid_parameter(str(error), parameter="indicators", received=text) from None

    return IndicatorsRequest(venue, symbol, SERVED_TIMEFRAME, start, end, indicators)


def indicators_window_end(request: IndicatorsRequest, *, as_of_ms: int, publication_delay_ms: int) -> int:
    """The window end: an explicit ``end`` unchanged, else one slot past the newest expected slot (contract 6.3.5).

    The clock is an argument; nothing here reads it. ``as_of_ms`` must be in the valid time range: callers validate
    it first (``ensure_timestamp_ms``), since with a defaulted end a clock below it raises ``ValueError`` here.
    """
    if request.end is not None:
        return request.end
    timeframe_ms = request.timeframe.milliseconds
    newest = newest_expected_open_ms(as_of_ms, request.timeframe, publication_delay_ms)
    if newest is None or newest + timeframe_ms <= request.start:
        return request.start + timeframe_ms
    end = newest + timeframe_ms
    if (end - request.start) // timeframe_ms > MAX_INDICATOR_WINDOW_SLOTS:
        raise invalid_parameter(
            f"the window up to the newest expected slot exceeds {MAX_INDICATOR_WINDOW_SLOTS} slots;"
            " pass an explicit end",
            parameter="start",
            received=str(request.start),
            maximum=MAX_INDICATOR_WINDOW_SLOTS,
        )
    return end


def require_no_query(raw_query: bytes) -> None:
    """The health endpoints take no parameters: any name is a ``400``, malformed escapes included (contract 6.2)."""
    for name, value in _decode(raw_query):
        raise invalid_parameter("health endpoints take no parameters", parameter=cut_received(name), received=value)


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


def _series_and_start(values: dict[str, str], label_name: str = "timeframe") -> tuple[str, str, int]:
    """Validate venue, symbol, the timeframe (``period`` for open interest) and start, in that order.

    The timeframe label is not returned (always served); ``label_name`` is the parameter's name in the request.
    """
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

    if values[label_name] != SERVED_TIMEFRAME.label:
        raise invalid_parameter(
            f"{label_name} must be {SERVED_TIMEFRAME.label}", parameter=label_name, received=values[label_name]
        )

    return venue, symbol, _timestamp("start", values["start"])


def _end(values: dict[str, str]) -> int | None:
    return _end_timestamp(values["end"]) if "end" in values else None


def _check_window(start: int, end: int, maximum: int = MAX_WINDOW_SLOTS) -> None:
    if end <= start:
        raise invalid_parameter("end must be greater than start", parameter="end", received=str(end))
    if (end - start) // SERVED_TIMEFRAME.milliseconds > maximum:
        raise invalid_parameter(
            f"the window must not exceed {maximum} slots",
            parameter="end",
            received=str(end),
            maximum=maximum,
        )


def _decode(raw_query: bytes) -> list[tuple[str, str]]:
    """Decode the query string; every failure is a 400 without ``parameter`` (the name may be the broken part)."""
    try:
        text = raw_query.decode("ascii")
    except UnicodeDecodeError as error:
        raise _undecodable(
            raw_query, error.start, "the query string must be ASCII; percent-encode other characters"
        ) from None
    escape = _MALFORMED_ESCAPE.search(text)
    if escape is not None:
        raise _undecodable(raw_query, escape.start(), "the query string contains a malformed percent-escape")
    try:
        return parse_qsl(text, keep_blank_values=True, strict_parsing=False, errors="strict")
    except UnicodeDecodeError:
        raise _undecodable(
            raw_query, _invalid_utf8_position(text), "the query string is not valid UTF-8 after percent-decoding"
        ) from None


def _invalid_utf8_position(text: str) -> int:
    """Where the first ``&``-separated piece that does not percent-decode to UTF-8 begins."""
    position = 0
    for piece in text.split("&"):
        try:
            unquote(piece, errors="strict")
        except UnicodeDecodeError:
            return position
        position += len(piece) + 1
    return 0  # unreachable in practice: parse_qsl decoded the same pieces


def _undecodable(raw_query: bytes, position: int, message: str) -> ApiError:
    """``received`` is the stretch of the query around the first offending byte, not its (usually valid) start."""
    text = raw_query.decode("ascii", errors="replace")
    start = max(0, min(position, len(text)) - RECEIVED_MAX_CHARS // 2)
    return invalid_parameter(message, received=text[start : start + RECEIVED_MAX_CHARS])


def _integer(name: str, text: str) -> int:
    if INTEGER_PATTERN.fullmatch(text) is None:
        raise invalid_parameter(f"{name} must be an integer of 1-16 ASCII digits", parameter=name, received=text)
    return int(text)


def _end_timestamp(text: str) -> int:
    """``end`` is exclusive, so it may be one timeframe past the last valid open time (the slot at the maximum)."""
    return _timestamp("end", text, maximum=MAX_TIMESTAMP_MS + SERVED_TIMEFRAME.milliseconds)


def _timestamp(name: str, text: str, *, maximum: int = MAX_TIMESTAMP_MS) -> int:
    value = _integer(name, text)
    if not MIN_TIMESTAMP_MS <= value <= maximum:
        raise invalid_parameter(f"{name} must be within [{MIN_TIMESTAMP_MS}, {maximum}]", parameter=name, received=text)
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
