"""Observations of warm-up convergence (spec 5.2.1, 5.6 C1) and of flat runs (spec 5.5, follow-up F-20).

These tests observe the locked build (ta-lib python 0.8.2, C library 0.8.1); they are not guarantees of the contract.
A tolerance detects a regression (an anchor shorter than ``warmup_slots``); it is the measured maximum times 2,
rounded up to one significant digit.

Comparing with spec 5.2.1: the absolute figures of that table for ``ema``, ``macd`` and ``atr`` depend on the step size
of the designer's probe, which the spec does not state (the errors scale with the step, not with the price level), so
only the scale-free columns (RSI, ADX, DI, in points) and the relative figures are comparable. Measured here with
``shaped_bars``: RSI(100) 0.54 and ADX(100) 0.15 points after a 1000x volatility collapse (spec 0.5 and 0.13), ATR(100)
3.76 % of the ATR value after it (spec 3.6 %), and the tiny-TR absolute errors of the price-level indicators agree.
"""

import math
import random
from collections.abc import Sequence

import pytest

from quasar_market_data.api.indicator_engine import compute_function
from quasar_market_data.domain.indicator_series import SegmentPrices
from quasar_market_data.domain.indicators import IndicatorSpec
from tests.indicator_data import Shape, shaped_bars

BARS = 6000
POINTS = range(2600, BARS, 37)
SHAPES: tuple[Shape, ...] = ("walk", "tiny_tr", "trend_reversal", "volatility_collapse")
SEED = 1

# (minimum parameters, maximum parameters) per indicator.
PARAMETERS: dict[str, tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...]]] = {
    "rsi": ((("period", 2),), (("period", 100),)),
    "ema": ((("period", 2),), (("period", 200),)),
    "macd": ((("fast", 2), ("slow", 3), ("signal", 2)), (("fast", 100), ("slow", 200), ("signal", 50))),
    "atr": ((("period", 2),), (("period", 100),)),
    "adx": ((("period", 2),), (("period", 100),)),
}

# Tolerance (measured maximum x 2, rounded up to one digit) as (minimum parameters, maximum parameters); the measured
# maximum is in the comment. Points for rsi and adx (adx also covers plus_di and minus_di), relative to the close for
# ema, macd (all three outputs) and atr. Measured on the locked build over POINTS, seed 1.
TOLERANCE: dict[tuple[str, Shape], tuple[float, float]] = {
    # measured: 2.77e-4 / 9.45e-4
    ("rsi", "walk"): (6e-4, 2e-3),
    # measured: 5.17e-4 / 8.57e-4
    ("rsi", "tiny_tr"): (2e-3, 2e-3),
    # measured: 2.82e-4 / 3.32e-3
    ("rsi", "trend_reversal"): (6e-4, 7e-3),
    # measured: 1.32e-3 / 0.54 (spec 5.2.1: 0.009 / 0.5)
    ("rsi", "volatility_collapse"): (3e-3, 2.0),
    # measured: 4.43e-11 / 2.35e-9
    ("ema", "walk"): (9e-11, 5e-9),
    # measured: 2.14e-13 / 1.63e-11
    ("ema", "tiny_tr"): (5e-13, 4e-11),
    # measured: 2.56e-11 / 1.22e-8
    ("ema", "trend_reversal"): (6e-11, 3e-8),
    # measured: 2.88e-11 / 2.63e-9
    ("ema", "volatility_collapse"): (6e-11, 6e-9),
    # measured: 6.23e-14 / 1.59e-10
    ("macd", "walk"): (2e-13, 4e-10),
    # measured: 5.65e-16 / 1.09e-12
    ("macd", "tiny_tr"): (2e-15, 3e-12),
    # measured: 4.43e-14 / 8.03e-10
    ("macd", "trend_reversal"): (9e-14, 2e-9),
    # measured: 4.36e-14 / 1.70e-10
    ("macd", "volatility_collapse"): (9e-14, 4e-10),
    # measured: 2.75e-11 / 1.43e-10
    ("atr", "walk"): (6e-11, 3e-10),
    # measured: 1.38e-13 / 8.56e-13
    ("atr", "tiny_tr"): (3e-13, 2e-12),
    # measured: 2.16e-11 / 1.55e-10
    ("atr", "trend_reversal"): (5e-11, 4e-10),
    # measured: 1.54e-11 / 1.02e-9 (3.76 % of the ATR value for ATR(100))
    ("atr", "volatility_collapse"): (4e-11, 3e-9),
    # measured: 4.91e-5 / 8.57e-4
    ("adx", "walk"): (1e-4, 2e-3),
    # measured: 4.59e-5 / 9.58e-4
    ("adx", "tiny_tr"): (1e-4, 2e-3),
    # measured: 2.35e-5 / 4.58e-3
    ("adx", "trend_reversal"): (5e-5, 1e-2),
    # measured: 1.90e-4 / 0.15 (spec 5.2.1: ADX(100) 0.13)
    ("adx", "volatility_collapse"): (4e-4, 0.3),
}

RELATIVE_TO_CLOSE = {"ema", "macd", "atr"}


def _slice(prices: SegmentPrices, start: int, stop: int) -> SegmentPrices:
    return SegmentPrices(prices.high[start:stop], prices.low[start:stop], prices.close[start:stop])


def _worst_difference(name: str, shape: Shape, params: tuple[tuple[str, int], ...]) -> float:
    spec = IndicatorSpec(name, params)
    compute = compute_function(spec)
    warmup = spec.warmup_slots
    bars = shaped_bars(shape, BARS, SEED)
    whole = compute(bars)
    worst = 0.0
    for t in POINTS:
        anchored = compute(_slice(bars, t - warmup, t + 1))
        for output, reference in zip(anchored, whole, strict=True):
            a, b = output[-1], reference[t]
            assert math.isfinite(a)
            assert math.isfinite(b)
            difference = abs(a - b)
            if name in RELATIVE_TO_CLOSE:
                difference /= bars.close[t]
            worst = max(worst, difference)
    return worst


def _check(name: str) -> None:
    for shape in SHAPES:
        minimum, maximum = TOLERANCE[(name, shape)]
        low_params, high_params = PARAMETERS[name]
        assert _worst_difference(name, shape, low_params) <= minimum, (name, shape, "minimum parameters")
        assert _worst_difference(name, shape, high_params) <= maximum, (name, shape, "maximum parameters")


def test_c1_rsi_anchored_matches_whole_history() -> None:
    """Anchored at ``t - warmup_slots`` against slot 0, in points; measured maxima in TOLERANCE comments."""
    _check("rsi")


def test_c1_ema_anchored_matches_whole_history() -> None:
    """Relative to the close; measured maxima in TOLERANCE comments."""
    _check("ema")


def test_c1_macd_anchored_matches_whole_history() -> None:
    """Relative to the close, worst of macd, signal and histogram; maxima in TOLERANCE comments."""
    _check("macd")


def test_c1_atr_anchored_matches_whole_history() -> None:
    """Relative to the close; after a 1000x volatility collapse ATR(100) is off by 3.76 % of its own value (measured)."""
    _check("atr")


def test_c1_adx_anchored_matches_whole_history() -> None:
    """In points, worst of adx, plus_di and minus_di; maxima in TOLERANCE comments."""
    _check("adx")


@pytest.mark.parametrize("shape", SHAPES)
def test_c1_bbands_depends_only_on_the_last_period_closes(shape: Shape) -> None:
    bars = shaped_bars(shape, BARS, SEED)
    for period, stddev in ((2, 1), (200, 5)):
        spec = IndicatorSpec("bbands", (("period", period), ("stddev", stddev)))
        compute = compute_function(spec)
        whole = compute(bars)
        for t in POINTS:
            anchored = compute(_slice(bars, t - spec.warmup_slots, t + 1))
            for output, reference in zip(anchored, whole, strict=True):
                assert output[-1] == pytest.approx(reference[t], rel=1e-9)


FLAT_FROM = 100  # shaped_bars("flat_after_move", 300, 1): moves up to slot 99, high = low = close from slot 100.


def _flat_after_move() -> SegmentPrices:
    return shaped_bars("flat_after_move", 300, SEED)


def _last(name: str, params: Sequence[tuple[str, int]], start: int) -> list[float]:
    compute = compute_function(IndicatorSpec(name, tuple(params)))
    return [output[-1] for output in compute(_slice(_flat_after_move(), start, 300))]


# OBSERVED on the locked build (ta-lib 0.8.2, C 0.8.1) for adx:period=14 at slot 299, anchored before the flat run.
FROZEN_DI = {
    0: (16.155923064287546, 16.62742423252769, 23.035303611014808),
    50: (15.996042316084532, 16.633906285157522, 22.968765882583206),
}


def test_flat_run_adx_and_di_read_zero_only_when_the_whole_range_is_flat() -> None:
    """Characterization, follow-up F-20: ADX and both DI read 0 only if the range from the anchor is flat.

    Anchored inside the flat run (or at its last moving slot, whose true range is not used) all three are exactly 0.
    Anchored before it they keep a frozen non-zero value (the smoothed DM and TR shrink at the same rate); update
    together with the user's F-20 decision.
    """
    for start in (FLAT_FROM - 1, FLAT_FROM, 120, 200):
        assert _last("adx", (("period", 14),), start) == [0.0, 0.0, 0.0], start
    for start, expected in FROZEN_DI.items():
        observed = _last("adx", (("period", 14),), start)
        assert observed == pytest.approx(expected, rel=1e-9), start
        assert all(value > 0.0 for value in observed)


def test_flat_run_atr_is_zero_and_bbands_collapse() -> None:
    for start in (FLAT_FROM - 1, FLAT_FROM, 120, 200):
        assert _last("atr", (("period", 14),), start) == [0.0], start
    # OBSERVED on the locked build: ATR anchored before the flat run is still non-zero (about 4.8e-7 at start 0).
    assert _last("atr", (("period", 14),), 0)[0] > 0.0
    for start in (0, FLAT_FROM, 200):
        upper, middle, lower = _last("bbands", (("period", 20), ("stddev", 2)), start)
        assert upper == middle == lower, start


@pytest.mark.parametrize("true_range", [1e-13, 1e-15, 1e-17])
def test_tiny_true_ranges_give_ordinary_values(true_range: float) -> None:
    """True ranges far below the price give finite values (no zero clamp, no NaN); ADX stays inside (0, 100)."""
    rng = random.Random(f"tiny-{true_range}")
    close = 1e-5
    highs, lows, closes = [], [], []
    for _ in range(300):
        close += rng.choice((-1, 1)) * true_range / 2
        highs.append(close + true_range / 2)
        lows.append(close - true_range / 2)
        closes.append(close)
    prices = SegmentPrices(tuple(highs), tuple(lows), tuple(closes))
    for name, params in (
        ("adx", (("period", 14),)),
        ("atr", (("period", 14),)),
        ("rsi", (("period", 14),)),
    ):
        outputs = compute_function(IndicatorSpec(name, params))(prices)
        for output in outputs:
            assert all(math.isfinite(value) for value in output[-100:]), (name, true_range)
        if name == "adx":
            assert 0.0 < outputs[0][-1] < 100.0
        if name == "atr":
            assert outputs[0][-1] > 0.0
