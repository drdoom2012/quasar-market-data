"""Positional shortest round-trip text of a finite float64 (contract section 6.3, decision I5)."""

import math
from decimal import Decimal


def float_text(value: float) -> str:
    """The shortest decimal that parses back to ``value``, without exponent, trailing zero or ``-0``."""
    if not math.isfinite(value):
        raise ValueError(f"cannot write a non-finite value: {value!r}")
    text = format(Decimal(repr(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text
