"""W1: the seed weight left at the first emitted point is at most e^-9 for every allowed value (spec 5.2)."""

import math

from quasar_market_data.domain.indicators import parse_indicators

LIMIT = -9.0


def _rsi_atr(period: int, warmup: int) -> float:
    return (warmup - period) * math.log1p(-1 / period)


def _ema(period: int, warmup: int) -> float:
    return (warmup - period + 1) * math.log1p(-2 / (period + 1))


def _adx(period: int, warmup: int) -> float:
    r = 1 - 1 / period
    n = warmup - 2 * period + 1
    return math.log(r**n + (n / period) * r ** (warmup - period + 1))


def _macd(slow: int, signal: int, warmup: int) -> float:
    alpha_g = 2 / (signal + 1)
    r_g = 1 - alpha_g
    r_s = 1 - 2 / (slow + 1)
    total = r_s ** (warmup - slow + 1) + r_g ** (warmup - slow - signal + 2)
    for j in range(slow + signal - 1, warmup + 1):
        total += alpha_g * r_g ** (warmup - j) * r_s ** (j - slow + 1)
    return math.log(total)


def test_w1_seed_weight_is_at_most_e_minus_9_for_every_allowed_value() -> None:
    for period in range(2, 101):
        for name in ("rsi", "atr"):
            (spec,) = parse_indicators(f"{name}:period={period}")
            assert _rsi_atr(period, spec.warmup_slots) <= LIMIT, (name, period)
        (spec,) = parse_indicators(f"adx:period={period}")
        assert _adx(period, spec.warmup_slots) <= LIMIT, ("adx", period)
    for period in range(2, 201):
        (spec,) = parse_indicators(f"ema:period={period}")
        assert _ema(period, spec.warmup_slots) <= LIMIT, ("ema", period)
    for slow in range(3, 201):
        for signal in range(2, 51):
            (spec,) = parse_indicators(f"macd:fast=2,slow={slow},signal={signal}")
            assert _macd(slow, signal, spec.warmup_slots) <= LIMIT, ("macd", slow, signal)


def test_w1_detects_a_shorter_formula() -> None:
    assert any(_adx(period, 12 * period) > LIMIT for period in range(2, 101))
