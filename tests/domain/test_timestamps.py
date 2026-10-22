import pytest

from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, MIN_TIMESTAMP_MS, ensure_timestamp_ms
from tests.typing_helpers import untyped


class _IntSubclass(int):
    pass


def test_bounds_are_2000_and_2100_utc() -> None:
    assert MIN_TIMESTAMP_MS == 946_684_800_000
    assert MAX_TIMESTAMP_MS == 4_102_444_800_000


@pytest.mark.parametrize("value", [MIN_TIMESTAMP_MS, 1_700_000_100_000, MAX_TIMESTAMP_MS])
def test_in_range_values_are_returned_unchanged(value: int) -> None:
    result = ensure_timestamp_ms(value)
    assert result == value
    assert type(result) is int


@pytest.mark.parametrize("value", [MIN_TIMESTAMP_MS - 1, MAX_TIMESTAMP_MS + 1, 0, -1])
def test_out_of_range_raises_value_error(value: int) -> None:
    with pytest.raises(ValueError, match="timestamp_ms"):
        ensure_timestamp_ms(value)


@pytest.mark.parametrize("value", [True, 1.0, "1", None, _IntSubclass(1_700_000_100_000)])
def test_non_plain_int_raises_type_error(value: object) -> None:
    with pytest.raises(TypeError):
        ensure_timestamp_ms(untyped(value))


def test_error_message_uses_given_name() -> None:
    with pytest.raises(ValueError, match="open_time_ms"):
        ensure_timestamp_ms(0, "open_time_ms")
    with pytest.raises(TypeError, match="open_time_ms"):
        ensure_timestamp_ms(untyped(1.5), "open_time_ms")
