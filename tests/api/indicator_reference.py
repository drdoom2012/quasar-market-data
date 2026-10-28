"""Independent pure-Python references for the S19 indicators (plain floats, no TA-Lib, no numpy).

Each function returns lists as long as the input, NaN before the TA-Lib lookback of that output.
"""

import math
from collections.abc import Sequence


def ema_ref(close: Sequence[float], period: int) -> list[float]:
    """EMA with ``k = 2 / (period + 1)`` seeded with the SMA of the first ``period`` closes (first value at period-1)."""
    out = [math.nan] * len(close)
    if len(close) < period:
        return out
    k = 2.0 / (period + 1)
    value = sum(close[:period]) / period
    out[period - 1] = value
    for j in range(period, len(close)):
        value = (close[j] - value) * k + value
        out[j] = value
    return out


def _ema_from(series: Sequence[float], seed_start: int, first: int, period: int) -> list[float]:
    """EMA over ``series`` seeded with the SMA of ``series[seed_start : seed_start + period]``; first value at ``first``."""
    out = [math.nan] * len(series)
    k = 2.0 / (period + 1)
    value = sum(series[seed_start : seed_start + period]) / period
    out[first] = value
    for j in range(first + 1, len(series)):
        value = (series[j] - value) * k + value
        out[j] = value
    return out


def macd_ref(close: Sequence[float], fast: int, slow: int, signal: int) -> tuple[list[float], list[float], list[float]]:
    """MACD, signal and histogram with the seeding observed on the locked build (see the S19 plan, Facts checked).

    Slow EMA seeded with the SMA of close[0..slow-1]; fast EMA seeded late with the SMA of close[slow-fast..slow-1];
    both first valid at slow-1; the signal EMA is seeded with the SMA of macd[slow-1..slow+signal-2]; all three
    outputs are first emitted at slow+signal-2.
    """
    n = len(close)
    nan = [math.nan] * n
    lookback = slow + signal - 2
    if n <= lookback:
        return list(nan), list(nan), list(nan)
    slow_ema = _ema_from(close, 0, slow - 1, slow)
    fast_ema = _ema_from(close, slow - fast, slow - 1, fast)
    line = [math.nan] * n
    for j in range(slow - 1, n):
        line[j] = fast_ema[j] - slow_ema[j]
    sig = _ema_from(line, slow - 1, lookback, signal)
    macd = [math.nan] * n
    hist = [math.nan] * n
    for j in range(lookback, n):
        macd[j] = line[j]
        hist[j] = line[j] - sig[j]
    return macd, sig, hist


def _true_range(high: Sequence[float], low: Sequence[float], close: Sequence[float], j: int) -> float:
    return max(high[j] - low[j], abs(high[j] - close[j - 1]), abs(low[j] - close[j - 1]))


def atr_ref(high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int) -> list[float]:
    """Wilder ATR: first value at index ``period`` is the mean of TR[1..period]; then (prev*(p-1)+TR)/p."""
    n = len(close)
    out = [math.nan] * n
    if n <= period:
        return out
    value = sum(_true_range(high, low, close, j) for j in range(1, period + 1)) / period
    out[period] = value
    for j in range(period + 1, n):
        value = (value * (period - 1) + _true_range(high, low, close, j)) / period
        out[j] = value
    return out


def adx_ref(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int
) -> tuple[list[float], list[float], list[float]]:
    """Wilder +DM/-DM/TR sums (sum of the first period values at index period, then s - s/p + x), DI = 100*DM/TR,
    DX = 100*|+DI - -DI|/(+DI + -DI), ADX seeded with the mean of DX[period..2*period-1] (index 2*period-1), then
    (prev*(p-1)+DX)/p. DI and ADX are NaN before index period and 2*period-1 respectively.
    """
    n = len(close)
    adx = [math.nan] * n
    plus_di = [math.nan] * n
    minus_di = [math.nan] * n
    if n <= 2 * period - 1:
        return adx, plus_di, minus_di

    def movement(j: int) -> tuple[float, float]:
        up = high[j] - high[j - 1]
        down = low[j - 1] - low[j]
        plus = up if up > down and up > 0 else 0.0
        minus = down if down > up and down > 0 else 0.0
        return plus, minus

    sum_plus = sum_minus = sum_tr = 0.0
    for j in range(1, period):
        plus, minus = movement(j)
        sum_plus += plus
        sum_minus += minus
        sum_tr += _true_range(high, low, close, j)
    dx_values: list[float] = []
    previous_adx = math.nan
    for j in range(period, n):
        plus, minus = movement(j)
        sum_plus = sum_plus - sum_plus / period + plus
        sum_minus = sum_minus - sum_minus / period + minus
        sum_tr = sum_tr - sum_tr / period + _true_range(high, low, close, j)
        p_di = 100.0 * sum_plus / sum_tr if sum_tr != 0.0 else 0.0
        m_di = 100.0 * sum_minus / sum_tr if sum_tr != 0.0 else 0.0
        plus_di[j] = p_di
        minus_di[j] = m_di
        total = p_di + m_di
        dx = 100.0 * abs(m_di - p_di) / total if total != 0.0 else 0.0
        if j < 2 * period - 1:
            dx_values.append(dx)
        elif j == 2 * period - 1:
            dx_values.append(dx)
            previous_adx = sum(dx_values) / period
            adx[j] = previous_adx
        else:
            previous_adx = (previous_adx * (period - 1) + dx) / period
            adx[j] = previous_adx
    return adx, plus_di, minus_di


def bbands_ref(close: Sequence[float], period: int, stddev: int) -> tuple[list[float], list[float], list[float]]:
    """SMA of the last ``period`` closes plus/minus ``stddev`` population standard deviations."""
    n = len(close)
    upper = [math.nan] * n
    middle = [math.nan] * n
    lower = [math.nan] * n
    for j in range(period - 1, n):
        window = close[j - period + 1 : j + 1]
        mean = sum(window) / period
        deviation = math.sqrt(max(sum((c - mean) ** 2 for c in window) / period, 0.0))
        middle[j] = mean
        upper[j] = mean + stddev * deviation
        lower[j] = mean - stddev * deviation
    return upper, middle, lower
