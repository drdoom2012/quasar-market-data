"""TA-Lib adapter: the only module that imports talib and numpy (design S18 section 5.6).

TA-Lib global state (unstable period, compatibility) is never touched; defaults apply.
"""

from collections.abc import Sequence

import numpy
import talib

from quasar_market_data.domain.indicator_series import Compute, SegmentPrices
from quasar_market_data.domain.indicators import IndicatorSpec


def _floats(values: Sequence[float]) -> numpy.ndarray:
    return numpy.asarray(values, dtype=numpy.float64)


def compute_function(spec: IndicatorSpec) -> Compute:
    """The float64 compute function of ``spec``; one output sequence per indicator output."""
    name = spec.name
    if name == "rsi":
        period = spec.param("period")

        def rsi(segment: SegmentPrices) -> Sequence[Sequence[float]]:
            return (talib.RSI(_floats(segment.close), timeperiod=period).tolist(),)

        return rsi
    if name == "ema":
        period = spec.param("period")

        def ema(segment: SegmentPrices) -> Sequence[Sequence[float]]:
            return (talib.EMA(_floats(segment.close), timeperiod=period).tolist(),)

        return ema
    if name == "macd":
        fast, slow, signal = spec.param("fast"), spec.param("slow"), spec.param("signal")

        def macd(segment: SegmentPrices) -> Sequence[Sequence[float]]:
            line, signal_line, histogram = talib.MACD(
                _floats(segment.close), fastperiod=fast, slowperiod=slow, signalperiod=signal
            )
            return (line.tolist(), signal_line.tolist(), histogram.tolist())

        return macd
    if name == "atr":
        period = spec.param("period")

        def atr(segment: SegmentPrices) -> Sequence[Sequence[float]]:
            result = talib.ATR(_floats(segment.high), _floats(segment.low), _floats(segment.close), timeperiod=period)
            return (result.tolist(),)

        return atr
    if name == "adx":
        period = spec.param("period")

        def adx(segment: SegmentPrices) -> Sequence[Sequence[float]]:
            high, low, close = _floats(segment.high), _floats(segment.low), _floats(segment.close)
            return (
                talib.ADX(high, low, close, timeperiod=period).tolist(),
                talib.PLUS_DI(high, low, close, timeperiod=period).tolist(),
                talib.MINUS_DI(high, low, close, timeperiod=period).tolist(),
            )

        return adx
    if name == "bbands":
        period, stddev = spec.param("period"), spec.param("stddev")

        def bbands(segment: SegmentPrices) -> Sequence[Sequence[float]]:
            # talib.MA_Type exists at runtime but is missing from the stubs.
            sma = talib.MA_Type.SMA  # type: ignore[attr-defined]
            upper, middle, lower = talib.BBANDS(
                _floats(segment.close), timeperiod=period, nbdevup=stddev, nbdevdn=stddev, matype=sma
            )
            return (upper.tolist(), middle.tolist(), lower.tolist())

        return bbands
    raise ValueError(f"no engine function for indicator {name!r}")
