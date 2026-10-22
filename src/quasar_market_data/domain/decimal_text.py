"""Canonical decimal text: exact, bounded, string-only normalization of decimal values.

Canonicalization uses string arithmetic only: no ``Decimal`` arithmetic, no rounding, no context.
``Decimal`` is constructed solely in ``to_decimal``.
"""

import re
from decimal import Decimal
from typing import Final

MAX_INPUT_LENGTH: Final = 128
MAX_MANTISSA_DIGITS: Final = 96
MAX_EXPONENT: Final = 32
MAX_CANONICAL_DIGITS: Final = 96

_GRAMMAR: Final = re.compile(
    r"(?P<sign>[+-])?(?P<int>[0-9]+)(?:\.(?P<frac>[0-9]+))?(?:[eE](?P<exp>[+-]?[0-9]+))?",
    re.ASCII,
)


class InvalidDecimalError(ValueError):
    """Raised when text is not an acceptable decimal value."""


def canonical_decimal(value: str) -> str:
    """Return the canonical text of ``value``.

    Accepted grammar (ASCII only): ``[+-]?[0-9]+(\\.[0-9]+)?([eE][+-]?[0-9]+)?``. Limits are checked
    before any expansion. Trailing fractional zeros are stripped, integer digits are preserved, and
    nothing is rounded or truncated. Raises ``TypeError`` for non-``str`` input and
    ``InvalidDecimalError`` for text that is malformed or exceeds the limits.
    """
    if type(value) is not str:
        raise TypeError(f"value must be str, got {type(value).__name__}")
    if len(value) > MAX_INPUT_LENGTH:
        raise InvalidDecimalError(f"decimal text longer than {MAX_INPUT_LENGTH} characters")
    match = _GRAMMAR.fullmatch(value)
    if match is None:
        raise InvalidDecimalError("decimal text does not match the accepted grammar")

    int_digits = match.group("int")
    frac_digits = match.group("frac") or ""
    if len(int_digits) + len(frac_digits) > MAX_MANTISSA_DIGITS:
        raise InvalidDecimalError(f"more than {MAX_MANTISSA_DIGITS} mantissa digits")
    exponent = int(match.group("exp") or "0")
    if abs(exponent) > MAX_EXPONENT:
        raise InvalidDecimalError(f"exponent magnitude exceeds {MAX_EXPONENT}")

    digits = int_digits + frac_digits
    if not digits.strip("0"):
        return "0"

    point = len(int_digits) + exponent
    if point <= 0:
        integer, fraction = "0", "0" * -point + digits
    elif point >= len(digits):
        integer, fraction = digits + "0" * (point - len(digits)), ""
    else:
        integer, fraction = digits[:point], digits[point:]

    integer = integer.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    if len(integer) + len(fraction) > MAX_CANONICAL_DIGITS:
        raise InvalidDecimalError(f"canonical form has more than {MAX_CANONICAL_DIGITS} digits")

    sign = "-" if match.group("sign") == "-" else ""
    return f"{sign}{integer}.{fraction}" if fraction else f"{sign}{integer}"


def to_decimal(canonical: str) -> Decimal:
    """Return the exact ``Decimal`` for already-canonical text.

    Raises ``InvalidDecimalError`` if ``canonical`` is not exactly the output of ``canonical_decimal``.
    """
    if canonical_decimal(canonical) != canonical:
        raise InvalidDecimalError("decimal text is not in canonical form")
    return Decimal(canonical)
