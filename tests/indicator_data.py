"""Deterministic close series for indicator tests."""

import random
from decimal import Decimal
from typing import Literal

from quasar_market_data.domain.indicator_series import SegmentPrices


def generated_closes(count: int, seed: int) -> list[str]:
    """A seeded random walk of positive closes as canonical decimal text (at most 2 decimals, no trailing zero)."""
    rng = random.Random(seed)
    price = Decimal(100)
    closes: list[str] = []
    for _ in range(count):
        price = max(Decimal(1), price + Decimal(rng.randint(-150, 150)) / Decimal(100))
        text = format(price.quantize(Decimal("0.01")), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        closes.append(text)
    return closes


def generated_bars(count: int, seed: int) -> list[tuple[str, str, str, str]]:
    """Seeded OHLC bars as canonical decimal text: ``high >= max(open, close)``, ``0.01 <= low <= min(open, close)``.

    Closes equal ``generated_closes(count, seed)``; open is the previous close (the first opens at 100); wicks come from
    a second ``random.Random`` seeded from ``seed`` so that the closes keep their values.
    """
    closes = generated_closes(count, seed)
    rng = random.Random(f"wicks-{seed}")
    bars: list[tuple[str, str, str, str]] = []
    previous = Decimal(100)
    for text in closes:
        close = Decimal(text)
        top = max(previous, close) + Decimal(rng.randint(0, 80)) / Decimal(100)
        bottom = max(Decimal("0.01"), min(previous, close) - Decimal(rng.randint(0, 80)) / Decimal(100))
        bars.append((_canonical(previous), _canonical(top), _canonical(bottom), text))
        previous = close
    return bars


def _canonical(value: Decimal) -> str:
    text = format(value.quantize(Decimal("0.01")), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


type Shape = Literal["walk", "tiny_tr", "trend_reversal", "volatility_collapse", "flat_after_move"]


def shaped_bars(shape: Shape, count: int, seed: int) -> SegmentPrices:
    """Seeded float high/low/close for the data shapes of spec 5.2.1 (``low <= close <= high`` throughout).

    ``walk``: random walk at a 60 000 level. ``tiny_tr``: price 1e-5, moves and wicks of up to about 1e-12.
    ``trend_reversal``: a constant up-drift for the first half, then a constant down-drift. ``volatility_collapse``:
    the walk with step size and wick divided by 1000 from the midpoint. ``flat_after_move``: the walk until one
    third, then ``high = low = close`` constant.
    """
    rng = random.Random(f"shape-{shape}-{seed}")
    level = 1e-5 if shape == "tiny_tr" else 60000.0
    unit = 1e-14 if shape == "tiny_tr" else 0.01
    close = level
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for index in range(count):
        step = rng.randint(-150, 150) * unit
        wick_up = rng.randint(0, 80) * unit
        wick_down = rng.randint(0, 80) * unit
        if shape == "trend_reversal":
            step = (rng.randint(-100, 100) + (50 if index < count // 2 else -50)) * unit
        elif shape == "volatility_collapse" and index >= count // 2:
            step, wick_up, wick_down = step / 1000, wick_up / 1000, wick_down / 1000
        elif shape == "flat_after_move" and index >= count // 3:
            step, wick_up, wick_down = 0.0, 0.0, 0.0
        previous = close
        close = previous + step
        highs.append(max(previous, close) + wick_up)
        lows.append(min(previous, close) - wick_down)
        closes.append(close)
    return SegmentPrices(tuple(highs), tuple(lows), tuple(closes))
