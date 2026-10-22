from decimal import Decimal

import pytest

from quasar_market_data.domain.decimal_text import (
    MAX_CANONICAL_DIGITS,
    MAX_EXPONENT,
    MAX_INPUT_LENGTH,
    MAX_MANTISSA_DIGITS,
    InvalidDecimalError,
    canonical_decimal,
    to_decimal,
)
from tests.typing_helpers import untyped

CANONICAL_CASES: list[tuple[str, str]] = [
    ("1000", "1000"),
    ("1000.00", "1000"),
    ("10", "10"),
    ("0.00100", "0.001"),
    ("100.50", "100.5"),
    ("0.00001000", "0.00001"),
    ("1e3", "1000"),
    ("1E+3", "1000"),
    ("1.5e-3", "0.0015"),
    ("1e-8", "0.00000001"),
    ("0.00000001", "0.00000001"),
    ("+5", "5"),
    ("007", "7"),
    ("0", "0"),
    ("0.000", "0"),
    ("-0", "0"),
    ("-1.50", "-1.5"),
    ("1e32", "1" + "0" * 32),
]

LONG_40 = "1234567890123456789012345678901234567890.123456789"

WIDE_CASES: list[tuple[str, str]] = [
    (LONG_40, LONG_40),
    ("1.00000000000000000000000000000001", "1.00000000000000000000000000000001"),
    ("9" * 96, "9" * 96),
]

D = "9" * 64
F = "0." + "9" * 63

BOUNDARY_ACCEPTED: list[tuple[str, str]] = [
    (D + "e32", D + "0" * 32),
    (D + "e-32", "9" * 32 + "." + "9" * 32),
    (F + "e-32", "0." + "0" * 32 + "9" * 63),
    (F + "e32", "9" * 32 + "." + "9" * 31),
    ("9" * 96 + "e-1", "9" * 95 + "." + "9"),
    ("0." + "9" * 95, "0." + "9" * 95),
    ("1e32", "1" + "0" * 32),
]

BOUNDARY_REJECTED: list[str] = [
    "9" * 96 + "e1",
    "0." + "9" * 95 + "e-1",
    "9" * 97,
    "1e33",
    "1e-33",
]

ACCEPTED_SAMPLES = CANONICAL_CASES + WIDE_CASES + BOUNDARY_ACCEPTED

REJECTED_TEXT: list[str] = [
    "",
    " ",
    " 1",
    "1 ",
    "1\n",
    "1_000",
    "١٢٣",
    "１２３",
    ".5",
    "5.",
    "1e",
    "1e+",
    "--1",
    "0x10",
    "NaN",
    "Infinity",
    "-Infinity",
    "1,5",
    "abc",
    "1e999999999",
    "1" * (MAX_INPUT_LENGTH + 1),
]


def test_limit_constants() -> None:
    assert MAX_INPUT_LENGTH == 128
    assert MAX_MANTISSA_DIGITS == 96
    assert MAX_EXPONENT == 32
    assert MAX_CANONICAL_DIGITS == 96


@pytest.mark.parametrize(("raw", "expected"), CANONICAL_CASES)
def test_canonical_forms(raw: str, expected: str) -> None:
    assert canonical_decimal(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), WIDE_CASES)
def test_more_than_28_significant_digits_are_kept_exactly(raw: str, expected: str) -> None:
    assert canonical_decimal(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), BOUNDARY_ACCEPTED)
def test_boundary_combinations_accepted(raw: str, expected: str) -> None:
    assert canonical_decimal(raw) == expected


@pytest.mark.parametrize("raw", BOUNDARY_REJECTED)
def test_boundary_combinations_rejected(raw: str) -> None:
    with pytest.raises(InvalidDecimalError):
        canonical_decimal(raw)


def test_boundary_expected_digit_counts() -> None:
    assert len(canonical_decimal(D + "e32")) == 96
    assert canonical_decimal(F + "e-32").count("9") == 63
    assert len(canonical_decimal("0." + "9" * 95).replace(".", "")) == 96


@pytest.mark.parametrize("raw", REJECTED_TEXT)
def test_rejected_text(raw: str) -> None:
    with pytest.raises(InvalidDecimalError):
        canonical_decimal(raw)


def test_invalid_decimal_error_is_value_error() -> None:
    assert issubclass(InvalidDecimalError, ValueError)


@pytest.mark.parametrize("value", [untyped(1.5), untyped(1), untyped(Decimal(1)), untyped(None)])
def test_non_str_input_raises_type_error(value: object) -> None:
    with pytest.raises(TypeError):
        canonical_decimal(untyped(value))


def _check_closed(raw: str) -> str:
    c = canonical_decimal(raw)
    assert canonical_decimal(c) == c
    assert len(c) <= 98
    assert sum(ch.isdigit() for ch in c) <= MAX_CANONICAL_DIGITS
    d = to_decimal(c)
    if c != "0":
        assert format(d, "f") == c
    return c


@pytest.mark.parametrize(("raw", "expected"), ACCEPTED_SAMPLES)
def test_accepted_inputs_are_closed_under_canonicalization(raw: str, expected: str) -> None:
    assert _check_closed(raw) == expected


MANTISSA_LENGTHS = [1, 63, 64, 65, 96, 97]
EXPONENTS = [-33, -32, -1, 0, 1, 32, 33]


def _mantissa(pattern: str, n: int) -> str:
    if pattern == "int":
        return "9" * n
    if pattern == "frac":
        return "0." + "9" * (n - 1) if n > 1 else "0"
    return "0" * (n // 2) + "9" * (n - n // 2)


GRID = [
    _mantissa(pattern, n) + (f"e{e}" if e else "")
    for pattern in ("int", "frac", "leading_zero")
    for n in MANTISSA_LENGTHS
    for e in EXPONENTS
]


@pytest.mark.parametrize("raw", GRID)
def test_grid_is_rejected_or_closed(raw: str) -> None:
    try:
        canonical_decimal(raw)
    except InvalidDecimalError:
        return
    _check_closed(raw)


def test_grid_has_both_outcomes() -> None:
    accepted = 0
    for raw in GRID:
        try:
            canonical_decimal(raw)
        except InvalidDecimalError:
            continue
        accepted += 1
    assert 0 < accepted < len(GRID)


def test_to_decimal_accepts_canonical_text() -> None:
    assert to_decimal("100.5") == Decimal("100.5")


@pytest.mark.parametrize("raw", ["100.50", "-0", "1e3", "+5"])
def test_to_decimal_rejects_non_canonical_text(raw: str) -> None:
    with pytest.raises(InvalidDecimalError):
        to_decimal(raw)


def test_to_decimal_is_exact_beyond_default_context() -> None:
    assert format(to_decimal(LONG_40), "f") == LONG_40
