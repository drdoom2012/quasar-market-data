import pytest

from quasar_market_data.domain.decimal_text import InvalidDecimalError
from quasar_market_data.domain.funding_rate import (
    DEFAULT_FUNDING_RATE_PUBLICATION_DELAY_MS,
    FundingRate,
    FundingRateKey,
    RateType,
    count_off_hour_rows,
    forward_end_bound_ms,
    is_funding_rate_eligible,
    is_off_hour,
    select_eligible_funding_rates,
)
from quasar_market_data.domain.server_time import TimeRejection, TrustedTime, UntrustedTime
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


# --- eligibility (ADR-0004 P-4) ---

DELAY = 30_000


def test_default_publication_delay() -> None:
    assert DEFAULT_FUNDING_RATE_PUBLICATION_DELAY_MS == 30_000


def test_boundary_is_eligible_and_one_millisecond_earlier_is_not() -> None:
    row = make_row(offset=0)
    assert is_funding_rate_eligible(T, DELAY, T + DELAY) is True
    assert is_funding_rate_eligible(T, DELAY, T + DELAY - 1) is False
    assert select_eligible_funding_rates([row], DELAY, TrustedTime(T + DELAY)).eligible == (row,)
    assert select_eligible_funding_rates([row], DELAY, TrustedTime(T + DELAY - 1)).pending == (row,)
    assert is_funding_rate_eligible(T, 0, T) is True
    assert is_funding_rate_eligible(T, 0, T - 1) is False


def test_forward_end_bound_makes_every_row_up_to_it_eligible() -> None:
    lower_bound = T + 123_456
    bound = forward_end_bound_ms(DELAY, lower_bound)
    assert bound == lower_bound - DELAY
    assert is_funding_rate_eligible(bound, DELAY, lower_bound) is True
    assert is_funding_rate_eligible(bound + 1, DELAY, lower_bound) is False


def test_mixed_rows_are_split_in_order() -> None:
    early, on_time, late = make_row(offset=-3_600_000), make_row(offset=0), make_row(offset=3_600_000)
    selection = select_eligible_funding_rates([early, on_time, late], DELAY, TrustedTime(T + DELAY))
    assert selection.eligible == (early, on_time)
    assert selection.pending == (late,)
    assert selection.untrusted_reason is None


@pytest.mark.parametrize("reason", list(TimeRejection))
def test_untrusted_time_selects_nothing(reason: TimeRejection) -> None:
    rows = [make_row(offset=-3_600_000), make_row(offset=0)]
    selection = select_eligible_funding_rates(rows, DELAY, UntrustedTime(reason))
    assert selection.eligible == ()
    assert selection.pending == tuple(rows)
    assert selection.untrusted_reason == reason


def test_empty_rows() -> None:
    trusted = select_eligible_funding_rates([], DELAY, TrustedTime(T))
    untrusted = select_eligible_funding_rates([], DELAY, UntrustedTime(TimeRejection.NO_OBSERVATION))
    assert (trusted.eligible, trusted.pending) == ((), ())
    assert (untrusted.eligible, untrusted.pending) == ((), ())


def test_delay_is_validated_before_the_trust_branch() -> None:
    with pytest.raises(ValueError):
        select_eligible_funding_rates([], -1, UntrustedTime(TimeRejection.NO_OBSERVATION))
    with pytest.raises(ValueError):
        is_funding_rate_eligible(T, -1, T)
    with pytest.raises(ValueError):
        forward_end_bound_ms(-1, T)


def test_eligibility_argument_type_checks() -> None:
    for bad in (True, 1.5):
        with pytest.raises(TypeError):
            is_funding_rate_eligible(T, untyped(bad), T)
        with pytest.raises(TypeError):
            is_funding_rate_eligible(T, DELAY, untyped(bad))
        with pytest.raises(TypeError):
            forward_end_bound_ms(untyped(bad), T)
        with pytest.raises(TypeError):
            forward_end_bound_ms(DELAY, untyped(bad))
    with pytest.raises(ValueError):
        is_funding_rate_eligible(MAX_TIMESTAMP_MS + 1, DELAY, T)
    with pytest.raises(TypeError):
        select_eligible_funding_rates([], DELAY, untyped(object()))
