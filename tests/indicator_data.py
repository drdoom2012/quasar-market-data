"""Deterministic close series for indicator tests."""

import random
from decimal import Decimal


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
