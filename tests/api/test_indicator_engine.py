import ast
import json
import math
from collections.abc import Sequence
from pathlib import Path

import pytest

from quasar_market_data.api.float_text import float_text
from quasar_market_data.api.indicator_engine import compute_function
from quasar_market_data.domain.indicator_series import SegmentPrices
from quasar_market_data.domain.indicators import IndicatorSpec
from tests.api.indicator_reference import adx_ref, atr_ref, bbands_ref, ema_ref, macd_ref
from tests.indicator_data import generated_bars, generated_closes

ENGINE_PATH = Path(__file__).resolve().parents[2] / "src" / "quasar_market_data" / "api" / "indicator_engine.py"
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "indicators" / "rsi_golden.json"
S19_GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "indicators" / "s19_golden.json"

# OBSERVED on the locked build (ta-lib python 0.8.2, C library 0.8.1, numpy 2.5.4): constant closes (zero average
# gain and loss) give exactly 0.0, not 50 and not NaN. The TA-Lib C source (ta_RSI.c) was not consulted; R5 pins the
# observed behaviour, and a TA-Lib upgrade that changes it fails test_r5_constant_closes on purpose.
CONSTANT_VALUE = 0.0


def rsi_engine(closes: Sequence[float], period: int) -> list[float]:
    compute = compute_function(IndicatorSpec("rsi", (("period", period),)))
    prices = tuple(closes)
    return list(compute(SegmentPrices(prices, prices, prices))[0])


def wilder_rsi_reference(closes: Sequence[float], period: int) -> list[float]:
    """Independent Wilder RSI in plain Python floats; NaN before index ``period``."""
    out = [math.nan] * len(closes)
    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for j in range(1, len(closes)):
        change = closes[j] - closes[j - 1]
        gains[j] = max(change, 0.0)
        losses[j] = max(-change, 0.0)
    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    for j in range(period, len(closes)):
        if j > period:
            avg_gain = (avg_gain * (period - 1) + gains[j]) / period
            avg_loss = (avg_loss * (period - 1) + losses[j]) / period
        if avg_loss == 0.0 and avg_gain > 0.0:
            out[j] = 100.0
        elif avg_gain == 0.0 and avg_loss > 0.0:
            out[j] = 0.0
        elif avg_gain == 0.0 and avg_loss == 0.0:
            out[j] = CONSTANT_VALUE
        else:
            out[j] = 100.0 * avg_gain / (avg_gain + avg_loss)
    return out


def _series() -> dict[str, list[float]]:
    moving = [float(c) for c in generated_closes(200, seed=2)]
    return {
        "generated": [float(c) for c in generated_closes(600, seed=1)],
        "rising": [100.0 + i for i in range(300)],
        "falling": [1000.0 - i for i in range(300)],
        "constant": [42.5] * 300,
        "moving_then_constant": moving + [moving[-1]] * 300,
        "alternating": [100.0 + (i % 2) * 1.5 for i in range(300)],
    }


SERIES = _series()
PERIODS = [2, 14, 100]


@pytest.mark.parametrize("period", PERIODS)
@pytest.mark.parametrize("name", list(SERIES))
def test_r4_matches_a_pure_python_wilder_rsi(name: str, period: int) -> None:
    closes = SERIES[name]
    engine = rsi_engine(closes, period)
    reference = wilder_rsi_reference(closes, period)
    for j in range(period, len(closes)):
        assert math.isfinite(engine[j]) and math.isfinite(reference[j]), (name, period, j)
        if reference[j] in (0.0, 100.0):
            assert engine[j] == reference[j], (name, period, j, engine[j])
        else:
            assert abs(engine[j] - reference[j]) < 1e-9, (name, period, j, engine[j], reference[j])


def test_r4_tolerance_is_measured() -> None:
    """Maximum |engine - reference| over all R4 inputs and periods: 9.95e-14 (bound asserted: 1e-11)."""
    worst = 0.0
    for closes in SERIES.values():
        for period in PERIODS:
            engine = rsi_engine(closes, period)
            reference = wilder_rsi_reference(closes, period)
            for j in range(period, len(closes)):
                worst = max(worst, abs(engine[j] - reference[j]))
    assert worst <= 1e-11, worst


def _golden_values() -> dict[str, list[str]]:
    closes = [float(c) for c in generated_closes(400, seed=7)]
    return {str(period): [float_text(v) for v in rsi_engine(closes, period)[10 * period :]] for period in (14, 2)}


def test_r3_golden_strings() -> None:
    """A TA-Lib or numpy upgrade that changes a digit fails here on purpose; regenerate the file deliberately."""
    expected = json.loads(GOLDEN_PATH.read_text())
    assert _golden_values() == expected


def test_r5_constant_closes() -> None:
    values = rsi_engine([42.5] * 300, 14)
    assert all(v == CONSTANT_VALUE for v in values[140:])
    assert float_text(values[140]) == "0"


def test_short_input_gives_no_finite_value_before_period() -> None:
    values = rsi_engine([float(c) for c in generated_closes(10, seed=5)], 14)
    assert len(values) == 10
    assert all(math.isnan(v) for v in values)


def test_c1_warmup_approximation() -> None:
    """Observation, not a guarantee: RSI over the last 10 x period + 1 closes approximates the full-history RSI.

    Measured maximum difference: 8.97e-4 (periods 2, 14, 100).
    """
    closes = [float(c) for c in generated_closes(5000, seed=3)]
    for period in PERIODS:
        full = rsi_engine(closes, period)[-1]
        window = rsi_engine(closes[-(10 * period + 1) :], period)[-1]
        assert abs(full - window) < 1e-2, (period, full, window)


def test_never_changes_talib_global_state() -> None:
    tree = ast.parse(ENGINE_PATH.read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not names & {"set_unstable_period", "set_compatibility"}


# ---------------------------------------------------------------- S19 indicators

type Bars = tuple[list[float], list[float], list[float]]  # high, low, close
R4_RELATIVE = 1e-9  # one tolerance for every indicator: |engine - reference| <= R4_RELATIVE * max(1, |reference|)


def _bars_from_text(count: int, seed: int) -> Bars:
    rows = generated_bars(count, seed)
    return [float(r[1]) for r in rows], [float(r[2]) for r in rows], [float(r[3]) for r in rows]


def _shape_bars(close: list[float]) -> Bars:
    return [c + 0.5 for c in close], [c - 0.5 for c in close], close


def _bar_sets() -> dict[str, Bars]:
    sets = {"generated": _bars_from_text(2000, seed=11)}
    for name in ("rising", "falling", "alternating"):
        sets[name] = _shape_bars(SERIES[name])
    return sets


BAR_SETS = _bar_sets()

TYPICAL: dict[str, tuple[tuple[str, int], ...]] = {
    "ema": (("period", 21),),
    "macd": (("fast", 12), ("slow", 26), ("signal", 9)),
    "atr": (("period", 14),),
    "adx": (("period", 14),),
    "bbands": (("period", 20), ("stddev", 2)),
}
MINIMUM: dict[str, tuple[tuple[str, int], ...]] = {
    "ema": (("period", 2),),
    "macd": (("fast", 2), ("slow", 3), ("signal", 2)),
    "atr": (("period", 2),),
    "adx": (("period", 2),),
    "bbands": (("period", 2), ("stddev", 1)),
}
MAXIMUM: dict[str, tuple[tuple[str, int], ...]] = {
    "ema": (("period", 200),),
    "macd": (("fast", 99), ("slow", 200), ("signal", 50)),
    "atr": (("period", 100),),
    "adx": (("period", 100),),
    "bbands": (("period", 200), ("stddev", 5)),
}
PARAM_SETS: dict[str, list[tuple[tuple[str, int], ...]]] = {
    name: [MINIMUM[name], TYPICAL[name], MAXIMUM[name]] for name in TYPICAL
}
PARAM_SETS["macd"] += [(("fast", 5), ("slow", 35), ("signal", 5)), (("fast", 99), ("slow", 100), ("signal", 50))]


def engine_outputs(name: str, params: tuple[tuple[str, int], ...], bars: Bars) -> list[list[float]]:
    high, low, close = bars
    compute = compute_function(IndicatorSpec(name, params))
    return [list(o) for o in compute(SegmentPrices(tuple(high), tuple(low), tuple(close)))]


def reference_outputs(name: str, params: tuple[tuple[str, int], ...], bars: Bars) -> list[list[float]]:
    high, low, close = bars
    p = dict(params)
    if name == "ema":
        return [ema_ref(close, p["period"])]
    if name == "macd":
        return [list(o) for o in macd_ref(close, p["fast"], p["slow"], p["signal"])]
    if name == "atr":
        return [atr_ref(high, low, close, p["period"])]
    if name == "adx":
        return [list(o) for o in adx_ref(high, low, close, p["period"])]
    if name == "bbands":
        return [list(o) for o in bbands_ref(close, p["period"], p["stddev"])]
    raise AssertionError(name)


def deviations(name: str, params: tuple[tuple[str, int], ...], bars: Bars) -> list[float]:
    """Per-index scaled deviation per output past that output's lookback; asserts the NaN patterns are equal."""
    engine = engine_outputs(name, params, bars)
    reference = reference_outputs(name, params, bars)
    assert len(engine) == len(reference)
    result: list[float] = []
    for out_index, (e, r) in enumerate(zip(engine, reference, strict=True)):
        assert len(e) == len(r)
        for j in range(len(r)):
            assert math.isnan(e[j]) == math.isnan(r[j]), (name, params, out_index, j, e[j], r[j])
            if not math.isnan(r[j]):
                result.append(abs(e[j] - r[j]) / max(1.0, abs(r[j])))
    return result


def _assert_r4(name: str) -> None:
    for params in PARAM_SETS[name]:
        for set_name, bars in BAR_SETS.items():
            for index, value in enumerate(deviations(name, params, bars)):
                assert value <= R4_RELATIVE, (name, params, set_name, index, value)


def test_r4_ema_matches_the_reference() -> None:
    _assert_r4("ema")


def test_r4_macd_matches_the_reference() -> None:
    _assert_r4("macd")


def test_r4_atr_matches_the_reference() -> None:
    _assert_r4("atr")


def test_r4_adx_matches_the_reference() -> None:
    _assert_r4("adx")


def test_r4_bbands_matches_the_reference() -> None:
    _assert_r4("bbands")


def test_r4_new_indicators_tolerance_is_measured() -> None:
    """Maximum scaled deviation |engine - reference| / max(1, |reference|) over all R4 inputs, bounded by MEASURED_MAX."""
    for name in TYPICAL:
        worst = 0.0
        for params in PARAM_SETS[name]:
            for bars in BAR_SETS.values():
                worst = max(worst, *deviations(name, params, bars))
        assert worst <= MEASURED_MAX[name], (name, worst)


# Measured on the locked build (maximum scaled deviation; bound = measured value rounded up to one significant digit,
# ema 2.7e-16, macd 4.7e-14, atr 2.1e-15, adx 1.7e-15, bbands 4.1e-14). The R4 tolerance itself stays 1e-9.
MEASURED_MAX = {"ema": 3e-16, "macd": 5e-14, "atr": 3e-15, "adx": 2e-15, "bbands": 5e-14}


def test_r4_argument_order_is_detected() -> None:
    high, low, close = BAR_SETS["generated"]
    engine = engine_outputs("atr", (("period", 14),), (high, low, close))[0]
    swapped = atr_ref(low, high, close, 14)
    assert max(abs(engine[j] - swapped[j]) for j in range(14, len(close))) > 1e-6


def test_r4_macd_lookback() -> None:
    close = BAR_SETS["generated"][2]
    for fast, slow, signal in ((2, 3, 2), (12, 26, 9), (5, 35, 5), (99, 100, 50), (99, 200, 50)):
        engine = engine_outputs("macd", (("fast", fast), ("slow", slow), ("signal", signal)), BAR_SETS["generated"])
        reference = macd_ref(close, fast, slow, signal)
        expected = slow + signal - 2
        for output in (*engine, *reference):
            first = next(j for j, v in enumerate(output) if not math.isnan(v))
            assert first == expected, (fast, slow, signal, first)


def test_r4_macd_rejects_independent_emas() -> None:
    close = BAR_SETS["generated"][2]
    fast, slow, signal = 12, 26, 9
    independent = [f - s for f, s in zip(ema_ref(close, fast), ema_ref(close, slow), strict=True)]
    engine = engine_outputs("macd", (("fast", fast), ("slow", slow), ("signal", signal)), BAR_SETS["generated"])[0]
    first = slow + signal - 2
    assert abs(engine[first] - independent[first]) > 1e-3


def _s19_golden_values() -> dict[str, list[str]]:
    bars = _bars_from_text(2000, seed=7)
    golden: dict[str, list[str]] = {}
    for name, typical in TYPICAL.items():
        for params in (typical, MAXIMUM[name]):
            spec = IndicatorSpec(name, params)
            outputs = engine_outputs(name, params, bars)
            warmup = spec.warmup_slots
            for output_name, values in zip(spec.outputs, outputs, strict=True):
                golden[f"{spec.id}/{output_name}"] = [float_text(v) for v in values[warmup:]]
    return golden


def test_r3_s19_golden_strings() -> None:
    """A TA-Lib or numpy upgrade that changes a digit fails here on purpose; regenerate the file deliberately."""
    assert _s19_golden_values() == json.loads(S19_GOLDEN_PATH.read_text())


EXTREMES: list[tuple[str, tuple[tuple[str, int], ...]]] = [
    ("adx", (("period", 2),)),
    ("macd", (("fast", 2), ("slow", 3), ("signal", 2))),
    ("macd", (("fast", 99), ("slow", 100), ("signal", 50))),
    ("bbands", (("period", 200), ("stddev", 5))),
    ("bbands", (("period", 2), ("stddev", 1))),
    ("ema", (("period", 2),)),
    ("ema", (("period", 200),)),
    ("atr", (("period", 2),)),
    ("atr", (("period", 100),)),
    ("adx", (("period", 100),)),
]


@pytest.mark.parametrize(("name", "params"), EXTREMES)
def test_no_nan_after_warmup_at_extreme_parameters(name: str, params: tuple[tuple[str, int], ...]) -> None:
    spec = IndicatorSpec(name, params)
    bars = _bars_from_text(spec.warmup_slots + 1, seed=4)
    outputs = engine_outputs(name, params, bars)
    assert len(outputs) == len(spec.outputs)
    for output in outputs:
        assert len(output) == spec.warmup_slots + 1
        assert math.isfinite(output[-1]), (name, params, output[-1])


def test_macd_histogram_is_talibs_third_output() -> None:
    macd, signal, histogram = engine_outputs("macd", TYPICAL["macd"], BAR_SETS["generated"])
    for j in range(33, len(macd)):
        assert abs(histogram[j] - (macd[j] - signal[j])) <= 1e-9 * max(1.0, abs(histogram[j]))


def test_bbands_bands_are_symmetric_around_the_middle() -> None:
    upper, middle, lower = engine_outputs("bbands", TYPICAL["bbands"], BAR_SETS["generated"])
    for j in range(19, len(middle)):
        assert abs((upper[j] - middle[j]) - (middle[j] - lower[j])) <= 1e-9 * max(1.0, abs(middle[j]))
