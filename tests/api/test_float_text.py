import pytest

from quasar_market_data.api.float_text import float_text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "0"),
        (-0.0, "0"),
        (100.0, "100"),
        (1e-07, "0.0000001"),
        (123456789.125, "123456789.125"),
        (0.1, "0.1"),
        (-2.5, "-2.5"),
    ],
)
def test_r2_float_text_pinned(value: float, expected: str) -> None:
    assert float_text(value) == expected


@pytest.mark.parametrize("value", [0.0, -0.0, 100.0, 1e-07, 123456789.125, 0.1, -2.5, 5e-324, 1 / 3, 2 / 3 * 100])
def test_r2_float_text_round_trips_without_exponent(value: float) -> None:
    text = float_text(value)
    assert "e" not in text.lower()
    if "." in text:
        assert not text.endswith("0")
        assert not text.endswith(".")
    assert float(text) == value


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_r2_non_finite_raises(value: float) -> None:
    with pytest.raises(ValueError):
        float_text(value)
