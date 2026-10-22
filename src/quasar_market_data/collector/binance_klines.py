"""Strict, all-or-nothing parser for raw Binance USD-M kline rows (ADR-0001 P-1, P-2).

A raw row is a 12-item list: ``[openTime, open, high, low, close, volume, closeTime, quoteVolume,
trades, takerBaseVolume, takerQuoteVolume, ignore]``. Times are JSON integers and every price or volume
is a decimal string; a JSON number in a text slot is an error and is never converted. The last four
items are not interpreted.

Any problem raises ``KlineParseError`` and no candle is returned. The parser checks shape and
consistency only: it does not validate market content (``validate_candle`` does), read a clock, or
decide whether a candle is closed.
"""

from collections.abc import Sequence
from typing import Final

from quasar_market_data.domain.candle import Candle, CandleKey
from quasar_market_data.domain.timeframe import Timeframe

VENUE_BINANCE_USDM: Final = "binance-usdm"

_ROW_LENGTH: Final = 12
_OPEN_TIME, _OPEN, _HIGH, _LOW, _CLOSE, _VOLUME, _CLOSE_TIME, _TURNOVER = range(8)


class KlineParseError(ValueError):
    """A raw kline batch was rejected; ``row_index`` names the offending row when there is one."""

    def __init__(self, message: str, row_index: int | None = None) -> None:
        self.row_index = row_index
        super().__init__(message if row_index is None else f"row {row_index}: {message}")


def _int_slot(row: Sequence[object], slot: int, name: str) -> int:
    value = row[slot]
    if type(value) is not int:
        raise TypeError(f"{name} must be a JSON integer, got {type(value).__name__}")
    return value


def _text_slot(row: Sequence[object], slot: int, name: str) -> str:
    value = row[slot]
    if type(value) is not str:
        raise TypeError(f"{name} must be a decimal string, got {type(value).__name__}")
    return value


def _parse_row(row: object, symbol: str, timeframe: Timeframe) -> Candle:
    if not isinstance(row, list | tuple):
        raise TypeError(f"row must be a list or tuple, got {type(row).__name__}")
    if len(row) != _ROW_LENGTH:
        raise ValueError(f"row must have {_ROW_LENGTH} items, got {len(row)}")
    open_time = _int_slot(row, _OPEN_TIME, "openTime")
    close_time = _int_slot(row, _CLOSE_TIME, "closeTime")
    expected_close = open_time + timeframe.milliseconds - 1
    if close_time != expected_close:
        raise ValueError(f"closeTime {close_time} != openTime + interval - 1 ({expected_close})")
    return Candle(
        key=CandleKey(VENUE_BINANCE_USDM, symbol, timeframe, open_time),
        open=_text_slot(row, _OPEN, "open"),
        high=_text_slot(row, _HIGH, "high"),
        low=_text_slot(row, _LOW, "low"),
        close=_text_slot(row, _CLOSE, "close"),
        volume=_text_slot(row, _VOLUME, "volume"),
        turnover=_text_slot(row, _TURNOVER, "quoteVolume"),
    )


def parse_klines(rows: Sequence[object], symbol: str, timeframe: Timeframe) -> tuple[Candle, ...]:
    """Parse ``rows`` into candles in input order, or raise ``KlineParseError`` with no partial result.

    ``openTime`` must be strictly increasing across the batch; gaps are allowed (coverage is not a
    parsing concern).
    """
    candles: list[Candle] = []
    for index, row in enumerate(rows):
        try:
            candle = _parse_row(row, symbol, timeframe)
        except (TypeError, ValueError) as error:
            raise KlineParseError(str(error), index) from error
        if candles and candle.key.open_time_ms <= candles[-1].key.open_time_ms:
            raise KlineParseError(
                f"openTime {candle.key.open_time_ms} is not after previous {candles[-1].key.open_time_ms}", index
            )
        candles.append(candle)
    return tuple(candles)
