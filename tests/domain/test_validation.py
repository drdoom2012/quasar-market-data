import pytest

from quasar_market_data.domain.candle import Candle
from quasar_market_data.domain.validation import (
    CandleValidationError,
    Violation,
    ViolationCode,
    require_valid,
    validate_candle,
)
from tests.domain.test_candle import make_candle

V = ViolationCode


def codes(candle: Candle) -> list[tuple[ViolationCode, str]]:
    return [(v.code, v.field) for v in validate_candle(candle)]


def test_typical_candle_is_valid() -> None:
    assert validate_candle(make_candle()) == ()


def test_flat_zero_volume_candle_is_valid() -> None:
    candle = make_candle(open="100", high="100", low="100", close="100", volume="0", turnover="0")
    assert validate_candle(candle) == ()


def test_missing_turnover_is_valid() -> None:
    assert validate_candle(make_candle(turnover=None)) == ()


def test_equality_between_high_low_open_close_is_valid() -> None:
    candle = make_candle(open="99", high="101", low="99", close="101")
    assert validate_candle(candle) == ()


def test_zero_open_and_low() -> None:
    assert codes(make_candle(open="0", high="101", low="0", close="100")) == [
        (V.NON_POSITIVE_PRICE, "open"),
        (V.NON_POSITIVE_PRICE, "low"),
    ]


def test_non_positive_open_reported_with_relation_violations() -> None:
    # open=0 with low=99: low above open.
    assert codes(make_candle(open="0", high="101", low="99", close="100")) == [
        (V.NON_POSITIVE_PRICE, "open"),
        (V.LOW_ABOVE_OPEN, "low"),
    ]


def test_negative_close_is_non_positive_price() -> None:
    assert (V.NON_POSITIVE_PRICE, "close") in codes(make_candle(close="-1"))


def test_negative_close_exact_list() -> None:
    assert codes(make_candle(close="-1")) == [
        (V.NON_POSITIVE_PRICE, "close"),
        (V.LOW_ABOVE_CLOSE, "low"),
    ]


def test_non_positive_prices_reported_in_field_order() -> None:
    candle = make_candle(open="0", high="0", low="0", close="0")
    assert codes(candle) == [
        (V.NON_POSITIVE_PRICE, "open"),
        (V.NON_POSITIVE_PRICE, "high"),
        (V.NON_POSITIVE_PRICE, "low"),
        (V.NON_POSITIVE_PRICE, "close"),
    ]


def test_negative_high_and_low_reported() -> None:
    candle = make_candle(open="-5", high="-1", low="-9", close="-3")
    assert codes(candle) == [
        (V.NON_POSITIVE_PRICE, "open"),
        (V.NON_POSITIVE_PRICE, "high"),
        (V.NON_POSITIVE_PRICE, "low"),
        (V.NON_POSITIVE_PRICE, "close"),
    ]


def test_high_below_open_close_and_low() -> None:
    candle = make_candle(open="100", high="90", low="95", close="100")
    assert codes(candle) == [
        (V.HIGH_BELOW_OPEN, "high"),
        (V.HIGH_BELOW_CLOSE, "high"),
        (V.HIGH_BELOW_LOW, "high"),
    ]


def test_high_below_open_only() -> None:
    candle = make_candle(open="102", high="101", low="99", close="100")
    assert codes(candle) == [(V.HIGH_BELOW_OPEN, "high")]


def test_high_below_close_only() -> None:
    candle = make_candle(open="100", high="101", low="99", close="102")
    assert codes(candle) == [(V.HIGH_BELOW_CLOSE, "high")]


def test_inverted_high_low_reports_all_overlapping_rules() -> None:
    candle = make_candle(open="100", high="100", low="101", close="100")
    assert codes(candle) == [
        (V.HIGH_BELOW_LOW, "high"),
        (V.LOW_ABOVE_OPEN, "low"),
        (V.LOW_ABOVE_CLOSE, "low"),
    ]


def test_low_above_open_and_close() -> None:
    candle = make_candle(open="100", high="110", low="105", close="102")
    assert codes(candle) == [(V.LOW_ABOVE_OPEN, "low"), (V.LOW_ABOVE_CLOSE, "low")]


def test_low_above_open_only() -> None:
    candle = make_candle(open="98", high="110", low="99", close="100")
    assert codes(candle) == [(V.LOW_ABOVE_OPEN, "low")]


def test_low_above_close_only() -> None:
    candle = make_candle(open="100", high="110", low="99", close="98")
    assert codes(candle) == [(V.LOW_ABOVE_CLOSE, "low")]


def test_negative_volume() -> None:
    assert codes(make_candle(volume="-1")) == [(V.NEGATIVE_VOLUME, "volume")]


def test_negative_turnover() -> None:
    assert codes(make_candle(turnover="-1")) == [(V.NEGATIVE_TURNOVER, "turnover")]


def test_independent_volume_and_turnover_defects_both_reported_in_rule_order() -> None:
    assert codes(make_candle(volume="-1", turnover="-2")) == [
        (V.NEGATIVE_VOLUME, "volume"),
        (V.NEGATIVE_TURNOVER, "turnover"),
    ]


def test_all_rules_fire_in_deterministic_order() -> None:
    # Every rule violated: all prices non-positive, high below open/close/low, low above open/close,
    # negative volume and turnover.
    candle = make_candle(open="-5", high="-10", low="-1", close="-3", volume="-1", turnover="-1")
    assert codes(candle) == [
        (V.NON_POSITIVE_PRICE, "open"),
        (V.NON_POSITIVE_PRICE, "high"),
        (V.NON_POSITIVE_PRICE, "low"),
        (V.NON_POSITIVE_PRICE, "close"),
        (V.HIGH_BELOW_OPEN, "high"),
        (V.HIGH_BELOW_CLOSE, "high"),
        (V.HIGH_BELOW_LOW, "high"),
        (V.LOW_ABOVE_OPEN, "low"),
        (V.LOW_ABOVE_CLOSE, "low"),
        (V.NEGATIVE_VOLUME, "volume"),
        (V.NEGATIVE_TURNOVER, "turnover"),
    ]


def test_every_violation_code_is_covered_by_the_exhaustive_case() -> None:
    candle = make_candle(open="-5", high="-10", low="-1", close="-3", volume="-1", turnover="-1")
    assert {v.code for v in validate_candle(candle)} == set(ViolationCode)


def test_violation_has_non_empty_detail() -> None:
    violations = validate_candle(make_candle(volume="-1"))
    assert violations == (Violation(code=V.NEGATIVE_VOLUME, field="volume", detail=violations[0].detail),)
    assert violations[0].detail


def test_comparison_is_exact_beyond_default_precision() -> None:
    candle = make_candle(
        open="1.00000000000000000000000000000000002",
        high="1.00000000000000000000000000000000001",
        low="1.00000000000000000000000000000000002",
        close="1.00000000000000000000000000000000002",
    )
    assert (V.HIGH_BELOW_LOW, "high") in codes(candle)


def test_exact_equal_at_high_precision_is_valid() -> None:
    value = "1.00000000000000000000000000000000002"
    candle = make_candle(open=value, high=value, low=value, close=value)
    assert validate_candle(candle) == ()


def test_require_valid_returns_same_object() -> None:
    candle = make_candle()
    assert require_valid(candle) is candle


def test_require_valid_raises_with_violations() -> None:
    candle = make_candle(open="100", high="90", low="95", close="100", volume="-1")
    with pytest.raises(CandleValidationError) as info:
        require_valid(candle)
    assert info.value.violations == validate_candle(candle)
    assert isinstance(info.value, ValueError)


def test_violation_codes_are_stable_strings() -> None:
    assert [c.value for c in ViolationCode] == [
        "NON_POSITIVE_PRICE",
        "HIGH_BELOW_OPEN",
        "HIGH_BELOW_CLOSE",
        "HIGH_BELOW_LOW",
        "LOW_ABOVE_OPEN",
        "LOW_ABOVE_CLOSE",
        "NEGATIVE_VOLUME",
        "NEGATIVE_TURNOVER",
    ]
