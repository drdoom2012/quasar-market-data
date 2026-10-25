import pytest

from quasar_market_data.api.errors import ApiError
from quasar_market_data.api.params import CandlesRequest, parse_candles_request
from quasar_market_data.domain.timeframe import Timeframe

TF = 300_000
START = 1792643400000
BASE = f"venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start={START}"


def parse(query: str) -> CandlesRequest:
    return parse_candles_request(query.encode("ascii"))


def error_of(query: bytes) -> ApiError:
    with pytest.raises(ApiError) as caught:
        parse_candles_request(query)
    assert caught.value.status == 400
    assert caught.value.code == "invalid_parameter"
    return caught.value


def replace(**changes: str | None) -> str:
    """BASE with the given parameters replaced (None removes it)."""
    pairs = dict(pair.split("=", 1) for pair in BASE.split("&"))
    for name, value in changes.items():
        if value is None:
            del pairs[name]
        else:
            pairs[name] = value
    return "&".join(f"{name}={value}" for name, value in pairs.items())


def test_minimal_valid_request() -> None:
    assert parse(BASE) == CandlesRequest("binance-usdm", "BTC/USDT:USDT", Timeframe("5m"), START, None, 1000)


def test_all_parameters_and_boundaries_are_accepted() -> None:
    request = parse(f"{BASE}&end={START + 10_000 * TF}&limit=1")
    assert (request.end, request.limit) == (START + 10_000 * TF, 1)
    assert parse(f"{BASE}&limit=1000").limit == 1000
    assert parse(replace(start="946684800000")).start == 946684800000
    assert parse(replace(start="4102444800000")).start == 4102444800000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BTC/USDT:USDT", "BTC/USDT:USDT"),
        ("BTC%2FUSDT%3AUSDT", "BTC/USDT:USDT"),
        ("%E6%B5%8B%E8%AF%95/USDT:USDT", "测试/USDT:USDT"),
        ("A%2BB", "A+B"),
        ("e%CC%81", "é"),
        ("%C3%A9", "é"),
        ("A" * 64, "A" * 64),
    ],
)
def test_symbol_decoding_is_exact(raw: str, expected: str) -> None:
    assert parse(replace(symbol=raw)).symbol == expected


def test_composed_and_decomposed_symbols_stay_different() -> None:
    assert parse(replace(symbol="e%CC%81")).symbol != parse(replace(symbol="%C3%A9")).symbol


@pytest.mark.parametrize(
    ("query", "parameter", "received", "maximum"),
    [
        (replace(venue=None), "venue", None, None),
        (replace(symbol=None), "symbol", None, None),
        (replace(timeframe=None), "timeframe", None, None),
        (replace(start=None), "start", None, None),
        (f"{BASE}&limt=10", "limt", "10", None),
        (f"{BASE}&Limit=5", "Limit", "5", None),
        (f"{BASE}&limit=1&limit=2", "limit", "2", None),
        (f"{BASE}&limit=", "limit", "", None),
        (f"{BASE}&end=", "end", "", None),
        (replace(symbol=""), "symbol", "", None),
        (f"{BASE}&limit=0", "limit", "0", 1000),
        (f"{BASE}&limit=1001", "limit", "1001", 1000),
        (replace(start=str(START + 1)), "start", str(START + 1), None),
        (replace(start="946684799999"), "start", "946684799999", None),
        (replace(start="4102444800001"), "start", "4102444800001", None),
        (f"{BASE}&end={START + TF + 1}", "end", str(START + TF + 1), None),
        (f"{BASE}&end={START}", "end", str(START), None),
        (f"{BASE}&end={START - TF}", "end", str(START - TF), None),
        (f"{BASE}&end={START + 10_001 * TF}", "end", str(START + 10_001 * TF), 10_000),
        (replace(venue="binance-spot"), "venue", "binance-spot", None),
        (replace(timeframe="1h"), "timeframe", "1h", None),
        (replace(timeframe="5M"), "timeframe", "5M", None),
        (replace(symbol="A" * 65), "symbol", "A" * 64, None),
        (replace(symbol="BTC%20USDT"), "symbol", "BTC USDT", None),
        (replace(symbol="BTC+USDT"), "symbol", "BTC USDT", None),
        (replace(symbol="BTC%09USDT"), "symbol", "BTC\tUSDT", None),
        (replace(symbol="BTC%00USDT"), "symbol", "BTC\x00USDT", None),
        (replace(symbol="BTC%0D%0AUSDT"), "symbol", "BTC\r\nUSDT", None),
    ],
)
def test_invalid_requests_are_400_with_details(
    query: str, parameter: str, received: str | None, maximum: int | None
) -> None:
    error = error_of(query.encode("ascii"))

    assert error.details.get("parameter") == parameter
    assert error.details.get("received") == received
    assert error.details.get("maximum") == maximum
    assert ("received" in error.details) == (received is not None)


def test_received_of_a_long_value_is_cut_to_64() -> None:
    error = error_of(replace(venue="v" * 100).encode("ascii"))

    assert error.details["received"] == "v" * 64


@pytest.mark.parametrize("parameter", ["start", "end", "limit"])
@pytest.mark.parametrize(
    ("raw", "decoded"),
    [
        ("1_000", "1_000"),
        ("%205", " 5"),
        ("5%20", "5 "),
        ("+5", " 5"),
        ("%2B5", "+5"),
        ("-5", "-5"),
        ("1.0", "1.0"),
        ("5%0A", "5\n"),
        ("%0A5", "\n5"),
        ("0x10", "0x10"),
        ("%D9%A1%D9%A2", "١٢"),  # Arabic-Indic digits
        ("%DB%B1%DB%B2", "۱۲"),  # Extended Arabic-Indic digits
        ("%E0%A5%A7", "१"),  # Devanagari digit
        ("%EF%BC%95", "５"),  # full-width digit
        ("%C2%B2", "²"),  # superscript two: str.isdigit() accepts it, \d does not
        ("12345678901234567", "12345678901234567"),  # 17 digits
    ],
)
def test_integer_lookalikes_are_rejected(parameter: str, raw: str, decoded: str) -> None:
    query = f"{BASE}&{parameter}={raw}" if parameter != "start" else replace(start=raw)

    error = error_of(query.encode("ascii"))

    assert error.details["parameter"] == parameter
    assert error.details["received"] == decoded
    assert "align" not in error.message


def test_integer_accepts_only_ascii_digits_up_to_16() -> None:
    assert parse(f"{BASE}&limit=0010").limit == 10
    assert parse(f"{BASE}&limit=0000000000000100").limit == 100
    assert parse(replace(start="4102444800000")).start == 4102444800000


@pytest.mark.parametrize(
    "query",
    [
        f"{BASE}&a=%zz",
        f"{BASE}&a=%",
        replace(symbol="a%zz"),
        replace(symbol="a%FF"),
        f"{BASE}&a=%C3",
    ],
)
def test_undecodable_query_is_400_without_parameter(query: str) -> None:
    error = error_of(query.encode("ascii"))

    assert "parameter" not in error.details
    assert isinstance(error.details["received"], str)
    assert len(str(error.details["received"])) <= 64


def test_a_raw_non_ascii_byte_is_400_without_parameter() -> None:
    error = error_of(BASE.encode("ascii") + b"&limit=\xc3")

    assert "parameter" not in error.details
    assert isinstance(error.details["received"], str)


def test_query_string_decoding() -> None:
    assert parse(f"{BASE}&&limit=5").limit == 5
    assert parse(f"{BASE}&limit=5&").limit == 5
    assert parse(replace(symbol="A;B")).symbol == "A;B"
    error = error_of(BASE.encode("ascii") + b"&limit")  # a pair without "=" is an empty value
    assert error.details["parameter"] == "limit"
    assert error.details["received"] == ""


def test_first_error_follows_the_documented_order() -> None:
    # an unknown parameter beats a missing parameter
    assert error_of(replace(venue=None).encode() + b"&bogus=1").details["parameter"] == "bogus"
    # a missing parameter beats a bad limit
    assert error_of(replace(symbol=None).encode() + b"&limit=0").details["parameter"] == "symbol"
    # a bad venue beats a bad limit
    assert error_of(replace(venue="x").encode() + b"&limit=0").details["parameter"] == "venue"
    # values are checked in order: a bad end beats a bad limit
    assert error_of(f"{BASE}&end=1&limit=0".encode()).details["parameter"] == "end"
