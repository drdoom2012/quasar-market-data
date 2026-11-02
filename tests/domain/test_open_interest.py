import pytest

from quasar_market_data.domain.candle import CandleKey
from quasar_market_data.domain.decimal_text import InvalidDecimalError
from quasar_market_data.domain.open_interest import OpenInterest, OpenInterestKey, period_end_count
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS
from tests.typing_helpers import untyped

T = 1_793_620_800_000  # 2026-11-02T12:00:00Z, aligned to 5m and 1h
P5 = Timeframe("5m")
KEY = OpenInterestKey("binance-usdm", "BTC/USDT:USDT", P5, T)


def make_row(open_interest: str = "95677.779", open_interest_value: str = "7446123150.675") -> OpenInterest:
    return OpenInterest(KEY, open_interest, open_interest_value)


def test_values_are_canonicalized() -> None:
    row = OpenInterest(KEY, "95677.77900000", "7446123150.67500000")
    assert row.open_interest == "95677.779"
    assert row.open_interest_value == "7446123150.675"
    zero = OpenInterest(KEY, "0.00000000", "0.00000000")
    assert (zero.open_interest, zero.open_interest_value) == ("0", "0")


def test_exponent_form_is_canonicalized() -> None:
    assert make_row(open_interest="1e-5").open_interest == "0.00001"


@pytest.mark.parametrize("slot", ["open_interest", "open_interest_value"])
@pytest.mark.parametrize("text", ["-1", "-0", "-0.0", "+5"])
def test_signed_values_are_rejected(slot: str, text: str) -> None:
    with pytest.raises(InvalidDecimalError, match="sign"):
        make_row(**{slot: text})


@pytest.mark.parametrize("slot", ["open_interest", "open_interest_value"])
@pytest.mark.parametrize("text", [" 1.5", "1.5 ", "", "abc", "1,5"])
def test_malformed_values_are_rejected(slot: str, text: str) -> None:
    with pytest.raises(InvalidDecimalError):
        make_row(**{slot: text})


@pytest.mark.parametrize("slot", ["open_interest", "open_interest_value"])
@pytest.mark.parametrize("value", [1.5, 1, None])
def test_non_str_values_are_rejected(slot: str, value: object) -> None:
    with pytest.raises(TypeError):
        make_row(**{slot: untyped(value)})


def test_row_key_must_be_an_open_interest_key() -> None:
    with pytest.raises(TypeError):
        OpenInterest(untyped(CandleKey("binance-usdm", "BTC/USDT:USDT", P5, T)), "1", "1")


def test_key_rejects_unaligned_time() -> None:
    with pytest.raises(ValueError, match="aligned"):
        OpenInterestKey("binance-usdm", "BTC/USDT:USDT", P5, T + 1)
    with pytest.raises(ValueError, match="aligned"):
        OpenInterestKey("binance-usdm", "BTC/USDT:USDT", Timeframe("1h"), T + 300_000)


def test_key_type_checks() -> None:
    for bad_time in (True, 1.0):
        with pytest.raises(TypeError):
            OpenInterestKey("binance-usdm", "BTC/USDT:USDT", P5, untyped(bad_time))
    with pytest.raises(TypeError):
        OpenInterestKey("binance-usdm", "BTC/USDT:USDT", untyped("5m"), T)
    with pytest.raises(ValueError):
        OpenInterestKey("binance-usdm", "BTC/USDT:USDT", P5, MAX_TIMESTAMP_MS + 300_000)


@pytest.mark.parametrize(("venue", "symbol"), [("Binance", "BTC/USDT:USDT"), ("binance-usdm", "")])
def test_key_identity_messages_match_candle_key(venue: str, symbol: str) -> None:
    with pytest.raises(ValueError) as candle_error:
        CandleKey(venue, symbol, P5, T)
    with pytest.raises(ValueError) as oi_error:
        OpenInterestKey(venue, symbol, P5, T)
    assert str(oi_error.value) == str(candle_error.value)


def test_key_and_value_equality() -> None:
    assert make_row() == make_row()
    assert hash(make_row()) == hash(make_row())
    assert OpenInterestKey("binance-usdm", "BTC/USDT:USDT", P5, T) == KEY


def test_period_end_count() -> None:
    assert period_end_count(1_793_620_200_000, 1_793_621_100_000, P5) == 4
    assert period_end_count(1_793_620_500_001, 1_793_621_100_000, P5) == 2
    assert period_end_count(1_793_620_800_001, 1_793_621_099_999, P5) == 0
    assert period_end_count(T, T, P5) == 1


def test_period_end_count_argument_checks() -> None:
    with pytest.raises(ValueError):
        period_end_count(T + 1, T, P5)
    with pytest.raises(TypeError):
        period_end_count(untyped(True), T, P5)
    with pytest.raises(TypeError):
        period_end_count(T, T, untyped("5m"))
