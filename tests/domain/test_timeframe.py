import pytest

from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, MIN_TIMESTAMP_MS
from tests.typing_helpers import untyped


class _IntSubclass(int):
    pass


SUPPORTED_LABELS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]


@pytest.mark.parametrize(("label", "expected"), [("5m", 300_000), ("1h", 3_600_000), ("1d", 86_400_000)])
def test_milliseconds(label: str, expected: int) -> None:
    assert Timeframe(label).milliseconds == expected


@pytest.mark.parametrize("label", SUPPORTED_LABELS)
def test_every_supported_timeframe_divides_one_utc_day(label: str) -> None:
    assert 86_400_000 % Timeframe(label).milliseconds == 0


def test_align_down_is_epoch_aligned() -> None:
    assert Timeframe("5m").align_down(1_700_000_123_456) == 1_700_000_100_000


def test_align_down_at_minimum() -> None:
    tf = Timeframe("5m")
    assert tf.align_down(MIN_TIMESTAMP_MS) == MIN_TIMESTAMP_MS
    assert tf.align_down(MIN_TIMESTAMP_MS + 299_999) == MIN_TIMESTAMP_MS


@pytest.mark.parametrize("label", SUPPORTED_LABELS)
def test_align_down_stays_in_range_at_bounds(label: str) -> None:
    tf = Timeframe(label)
    assert tf.align_down(MIN_TIMESTAMP_MS) == MIN_TIMESTAMP_MS
    assert tf.align_down(MAX_TIMESTAMP_MS) == MAX_TIMESTAMP_MS


def test_is_aligned() -> None:
    tf = Timeframe("5m")
    assert tf.is_aligned(1_700_000_100_000)
    assert not tf.is_aligned(1_700_000_100_001)


def test_end_exclusive_ms() -> None:
    assert Timeframe("5m").end_exclusive_ms(1_700_000_100_000) == 1_700_000_400_000


def test_end_exclusive_ms_may_equal_max() -> None:
    assert Timeframe("5m").end_exclusive_ms(MAX_TIMESTAMP_MS - 300_000) == MAX_TIMESTAMP_MS


def test_end_exclusive_ms_overflow_raises() -> None:
    with pytest.raises(ValueError, match="end_exclusive_ms"):
        Timeframe("5m").end_exclusive_ms(MAX_TIMESTAMP_MS)
    with pytest.raises(ValueError, match="end_exclusive_ms"):
        Timeframe("1d").end_exclusive_ms(MAX_TIMESTAMP_MS)


@pytest.mark.parametrize("label", ["0m", "-5m", "5", "7m", "3d", "", " 5m", "5M"])
def test_invalid_labels_rejected_on_direct_construction(label: str) -> None:
    with pytest.raises(ValueError, match="timeframe"):
        Timeframe(label)


def test_non_str_label_raises_type_error() -> None:
    with pytest.raises(TypeError):
        Timeframe(untyped(5))


def test_equality_and_hash() -> None:
    assert Timeframe("5m") == Timeframe("5m")
    assert hash(Timeframe("5m")) == hash(Timeframe("5m"))
    assert Timeframe("5m") != Timeframe("1h")


@pytest.mark.parametrize("bad", [True, 1.0, "1", None, _IntSubclass(1_700_000_100_000)])
def test_methods_reject_non_plain_int(bad: object) -> None:
    tf = Timeframe("5m")
    with pytest.raises(TypeError):
        tf.align_down(untyped(bad))
    with pytest.raises(TypeError):
        tf.is_aligned(untyped(bad))
    with pytest.raises(TypeError):
        tf.end_exclusive_ms(untyped(bad))


@pytest.mark.parametrize("bad", [MIN_TIMESTAMP_MS - 1, MAX_TIMESTAMP_MS + 1])
def test_methods_reject_out_of_range(bad: int) -> None:
    tf = Timeframe("5m")
    with pytest.raises(ValueError, match="timestamp_ms"):
        tf.align_down(bad)
    with pytest.raises(ValueError, match="timestamp_ms"):
        tf.is_aligned(bad)
    with pytest.raises(ValueError, match="open_time_ms"):
        tf.end_exclusive_ms(bad)
