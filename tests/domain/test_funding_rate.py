import pytest

from quasar_market_data.domain.decimal_text import InvalidDecimalError
from quasar_market_data.domain.funding_rate import (
    FundingRate,
    FundingRateKey,
    RateType,
    count_off_hour_rows,
    is_off_hour,
)
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS
from tests.typing_helpers import untyped

T = 1_793_736_000_000  # 2026-11-04T20:00:00Z, a whole hour
KEY = FundingRateKey("binance-usdm", "BTC/USDT:USDT", T + 5, RateType.REGULAR)


def make_row(funding_rate: str = "0.0001", mark_price: str | None = "67123.45", offset: int = 5) -> FundingRate:
    return FundingRate(
        FundingRateKey("binance-usdm", "BTC/USDT:USDT", T + offset, RateType.REGULAR), funding_rate, mark_price
    )


def test_rate_is_canonicalized_and_signed() -> None:
    assert FundingRate(KEY, "0.00010000", None).funding_rate == "0.0001"
    assert FundingRate(KEY, "-0.00010000", None).funding_rate == "-0.0001"
    assert FundingRate(KEY, "0.00000000", None).funding_rate == "0"
    assert FundingRate(KEY, "-0.00000000", None).funding_rate == "0"
    assert FundingRate(KEY, "+0.0001", None).funding_rate == "0.0001"


def test_mark_price_is_canonicalized_or_none() -> None:
    assert FundingRate(KEY, "0.0001", "67123.45000000").mark_price == "67123.45"
    assert FundingRate(KEY, "0.0001", None).mark_price is None
    assert FundingRate(KEY, "0.0001", "0.00000000").mark_price == "0"


@pytest.mark.parametrize("text", ["-1", "-0", "+1", "", " 1", "abc"])
def test_mark_price_rejects_sign_and_empty(text: str) -> None:
    with pytest.raises(InvalidDecimalError):
        FundingRate(KEY, "0.0001", text)


@pytest.mark.parametrize("text", ["", " 0.1", "0.1 ", "abc", "1,5"])
def test_rate_rejects_malformed_text(text: str) -> None:
    with pytest.raises(InvalidDecimalError):
        FundingRate(KEY, text, None)


@pytest.mark.parametrize("value", [0.0001, 1, None])
def test_rate_rejects_non_string(value: object) -> None:
    with pytest.raises(TypeError):
        FundingRate(KEY, untyped(value), None)


@pytest.mark.parametrize("value", [1.5, 1, True])
def test_mark_price_rejects_non_string(value: object) -> None:
    with pytest.raises(TypeError):
        FundingRate(KEY, "0.0001", untyped(value))


def test_key_type_checks() -> None:
    for bad_time in (True, 1.0, "1"):
        with pytest.raises(TypeError):
            FundingRateKey("binance-usdm", "BTC/USDT:USDT", untyped(bad_time), RateType.REGULAR)
    with pytest.raises(ValueError):
        FundingRateKey("binance-usdm", "BTC/USDT:USDT", MAX_TIMESTAMP_MS + 1, RateType.REGULAR)
    with pytest.raises(TypeError):
        FundingRateKey("binance-usdm", "BTC/USDT:USDT", T, untyped("Regular"))
    with pytest.raises(ValueError):
        FundingRateKey("Binance", "BTC/USDT:USDT", T, RateType.REGULAR)
    with pytest.raises(ValueError):
        FundingRateKey("binance-usdm", "", T, RateType.REGULAR)


def test_key_must_be_a_funding_rate_key() -> None:
    with pytest.raises(TypeError):
        FundingRate(untyped("key"), "0.0001", None)


def test_regular_and_special_of_one_hour_are_different_keys() -> None:
    regular = FundingRateKey("binance-usdm", "BTC/USDT:USDT", T + 1, RateType.REGULAR)
    special_later = FundingRateKey("binance-usdm", "BTC/USDT:USDT", T + 1_002, RateType.SPECIAL)
    special_same_time = FundingRateKey("binance-usdm", "BTC/USDT:USDT", T + 1, RateType.SPECIAL)
    assert regular != special_later
    assert regular != special_same_time
    assert regular == FundingRateKey("binance-usdm", "BTC/USDT:USDT", T + 1, RateType.REGULAR)
    assert hash(regular) == hash(FundingRateKey("binance-usdm", "BTC/USDT:USDT", T + 1, RateType.REGULAR))
    assert len({regular, special_later, special_same_time}) == 3


def test_funding_time_is_stored_as_reported() -> None:
    assert make_row(offset=17).key.funding_time_ms == T + 17


@pytest.mark.parametrize(
    ("offset", "expected"), [(0, False), (26, False), (1_003, False), (5_000, False), (5_001, True), (1_800_000, True)]
)
def test_is_off_hour(offset: int, expected: bool) -> None:
    assert is_off_hour(T + offset) is expected


def test_is_off_hour_rejects_non_int() -> None:
    with pytest.raises(TypeError):
        is_off_hour(untyped(True))


def test_count_off_hour_rows() -> None:
    rows = [make_row(offset=0), make_row(offset=26), make_row(offset=5_001), make_row(offset=1_800_000)]
    assert count_off_hour_rows(rows) == 2
    assert count_off_hour_rows([]) == 0
