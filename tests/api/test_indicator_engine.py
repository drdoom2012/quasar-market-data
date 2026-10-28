import ast
import json
import math
from collections.abc import Sequence
from pathlib import Path

import pytest

from quasar_market_data.api.float_text import float_text
from quasar_market_data.api.indicator_engine import compute_function
from quasar_market_data.domain.indicators import IndicatorSpec
from tests.indicator_data import generated_closes

ENGINE_PATH = Path(__file__).resolve().parents[2] / "src" / "quasar_market_data" / "api" / "indicator_engine.py"
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "indicators" / "rsi_golden.json"

# OBSERVED on the locked build (ta-lib python 0.8.2, C library 0.8.1, numpy 2.5.4): constant closes (zero average
# gain and loss) give exactly 0.0, not 50 and not NaN. The TA-Lib C source (ta_RSI.c) was not consulted; R5 pins the
# observed behaviour, and a TA-Lib upgrade that changes it fails test_r5_constant_closes on purpose.
CONSTANT_VALUE = 0.0


def rsi_engine(closes: Sequence[float], period: int) -> list[float]:
    compute = compute_function(IndicatorSpec("rsi", (("period", period),)))
    return list(compute(closes)[0])


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
