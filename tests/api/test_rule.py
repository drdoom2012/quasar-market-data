import pytest

from quasar_market_data.api.rule import (
    MAX_LAG_THRESHOLD_MS,
    MAX_PUBLICATION_DELAY_MS,
    check_lag_threshold,
    check_publication_delay,
    checked_rule,
)
from quasar_market_data.domain.timed_coverage import ExpectationRule


def test_the_bounds_themselves_are_accepted() -> None:
    assert checked_rule(15_000, 60_000) == ExpectationRule(15_000, 60_000)
    assert MAX_PUBLICATION_DELAY_MS == 300_000
    assert MAX_LAG_THRESHOLD_MS == 900_000
    assert check_publication_delay(0) == 0
    assert check_lag_threshold(0) == 0
    assert check_publication_delay(300_000) == 300_000
    assert check_lag_threshold(900_000) == 900_000
    rule = checked_rule(300_000, 900_000)
    assert (rule.publication_delay_ms, rule.lag_threshold_ms) == (300_000, 900_000)


def test_one_above_a_bound_is_refused() -> None:
    with pytest.raises(ValueError, match=r"publication_delay_ms.*300000"):
        check_publication_delay(300_001)
    with pytest.raises(ValueError, match=r"lag_threshold_ms.*900000"):
        check_lag_threshold(900_001)
    with pytest.raises(ValueError, match=r"publication_delay_ms.*300000"):
        checked_rule(300_001, 60_000)
    with pytest.raises(ValueError, match=r"lag_threshold_ms.*900000"):
        checked_rule(15_000, 900_001)


@pytest.mark.parametrize("value", [True, 1.5, "60"])
def test_wrong_types_are_refused(value: object) -> None:
    with pytest.raises(TypeError):
        check_publication_delay(value)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        check_lag_threshold(value)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        checked_rule(value, 60_000)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        checked_rule(15_000, value)  # type: ignore[arg-type]


def test_a_wrong_type_is_refused_before_the_bound_is_checked() -> None:
    with pytest.raises(TypeError):
        check_publication_delay(float(MAX_PUBLICATION_DELAY_MS + 1))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        check_lag_threshold(float(MAX_LAG_THRESHOLD_MS + 1))  # type: ignore[arg-type]


def test_negative_values_are_refused() -> None:
    with pytest.raises(ValueError):
        check_publication_delay(-1)
    with pytest.raises(ValueError):
        check_lag_threshold(-1)
    with pytest.raises(ValueError):
        checked_rule(-1, 60_000)
    with pytest.raises(ValueError):
        checked_rule(15_000, -1)
