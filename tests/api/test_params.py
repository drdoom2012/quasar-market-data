from collections.abc import Callable

import pytest

from quasar_market_data.api.errors import ApiError
from quasar_market_data.api.params import (
    CandlesRequest,
    CoverageRequest,
    IndicatorsRequest,
    indicators_window_end,
    parse_candles_request,
    parse_coverage_request,
    parse_indicators_request,
)
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


def test_end_may_be_one_timeframe_past_the_maximum_time() -> None:
    maximum = 4102444800000
    assert parse(replace(start=str(maximum)) + f"&end={maximum + TF}").end == maximum + TF
    error = error_of(f"{replace(start=str(maximum))}&end={maximum + 2 * TF}".encode("ascii"))
    assert error.details["parameter"] == "end"
    assert error.message == f"end must be within [946684800000, {maximum + TF}]"


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


def test_a_bad_limit_wins_over_an_empty_window() -> None:
    error = error_of(f"{BASE}&limit=0&end={START - TF}".encode())

    assert error.details["parameter"] == "limit"


END = START + 12 * TF
COVERAGE = f"{BASE}&end={END}"


def parse_coverage(query: str) -> CoverageRequest:
    return parse_coverage_request(query.encode("ascii"))


def coverage_error_of(query: str) -> ApiError:
    with pytest.raises(ApiError) as caught:
        parse_coverage_request(query.encode("ascii"))
    assert caught.value.status == 400
    assert caught.value.code == "invalid_parameter"
    return caught.value


def test_coverage_request_parses_the_required_fields() -> None:
    assert parse_coverage(COVERAGE) == CoverageRequest("binance-usdm", "BTC/USDT:USDT", Timeframe("5m"), START, END)


def test_coverage_requires_end() -> None:
    error = coverage_error_of(BASE)

    assert error.details == {"parameter": "end"}


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": "limt=10"},
        {"extra": "Limit=5"},
        {"extra": "venue=binance-usdm"},
        {"extra": "end=" + str(END)},
        {"extra": "symbol="},
        {"venue": ""},
        {"end": ""},
        {"start": str(START + 1)},
        {"end": str(END + 1)},
        {"end": str(START)},
        {"end": str(START - TF)},
        {"venue": "x"},
        {"symbol": "A" * 65},
        {"symbol": "BTC%20USDT"},
        {"start": "1_000"},
        {"start": "946684799999"},
    ],
)
def test_coverage_rejects_what_candles_rejects(changes: dict[str, str]) -> None:
    extra = changes.get("extra")
    overrides = {k: v for k, v in changes.items() if k != "extra"}
    query = replace(**{"end": str(END), **overrides})
    if extra is not None:
        query = f"{query}&{extra}"

    coverage = coverage_error_of(query)
    candles = error_of(query.encode("ascii"))

    assert coverage.message == candles.message
    assert coverage.details == candles.details


def test_coverage_cuts_received_to_64_characters() -> None:
    query = replace(end=str(END), venue="v" * 100)
    error = coverage_error_of(query)

    assert error.details["received"] == "v" * 64
    assert error.details == error_of(query.encode("ascii")).details


@pytest.mark.parametrize("parameter", ["limit", "as_of"])
def test_coverage_rejects_limit_and_as_of(parameter: str) -> None:
    error = coverage_error_of(f"{COVERAGE}&{parameter}=1")

    assert error.status == 400
    assert error.details["parameter"] == parameter


def test_coverage_window_may_be_exactly_10000_slots_and_not_more() -> None:
    assert parse_coverage(replace(end=str(START + 10_000 * TF))).end == START + 10_000 * TF

    error = coverage_error_of(replace(end=str(START + 10_001 * TF)))

    assert error.details["parameter"] == "end"
    assert error.details["maximum"] == 10_000


@pytest.mark.parametrize("parse_fn", [parse_candles_request, parse_coverage_request])
def test_both_parsers_validate_the_timeframe(parse_fn: Callable[[bytes], CandlesRequest | CoverageRequest]) -> None:
    end = f"&end={END}"

    def run(query: str) -> ApiError:
        with pytest.raises(ApiError) as caught:
            parse_fn((query + end).encode("ascii"))
        assert caught.value.status == 400
        return caught.value

    assert parse_fn((BASE + end).encode("ascii")).timeframe == Timeframe("5m")
    for value in ("1m", ""):
        error = run(replace(timeframe=value))
        assert error.details["parameter"] == "timeframe"
        assert error.details["received"] == value
    missing = run(replace(timeframe=None))
    assert missing.details == {"parameter": "timeframe"}


@pytest.mark.parametrize(
    ("bad", "shown"),
    [(b"%zz", "%zz"), (b"\xc3", "\ufffd"), (b"x=%E6%B5", "%E6%B5"), (b"x=%FF", "%FF")],
    ids=["malformed-escape", "raw-non-ascii", "truncated-utf-8", "invalid-utf-8"],
)
def test_received_of_an_undecodable_query_shows_the_bad_part_not_the_valid_prefix(bad: bytes, shown: str) -> None:
    prefix = b"a=" + b"v" * 200 + b"&"

    received = str(error_of(prefix + bad + b"&b=1").details["received"])

    assert len(received) <= 64
    assert shown in received


# --- indicators request (contract 6.3.1, 6.3.5) ---

IND_BASE = f"{BASE}&indicators=rsi:period=14"


def parse_ind(query: str) -> IndicatorsRequest:
    return parse_indicators_request(query.encode("ascii"))


def ind_error(query: str) -> ApiError:
    with pytest.raises(ApiError) as caught:
        parse_indicators_request(query.encode("ascii"))
    assert caught.value.status == 400
    assert caught.value.code == "invalid_parameter"
    return caught.value


def test_valid_indicators_request_parses() -> None:
    request = parse_ind(IND_BASE)
    assert (request.venue, request.symbol, request.timeframe, request.start, request.end) == (
        "binance-usdm",
        "BTC/USDT:USDT",
        Timeframe("5m"),
        START,
        None,
    )
    assert [spec.id for spec in request.indicators] == ["rsi:period=14"]


@pytest.mark.parametrize("name", ["venue", "symbol", "timeframe", "start", "indicators"])
def test_indicators_request_requires_every_name_but_end(name: str) -> None:
    query = IND_BASE.replace(f"&{name}=", "&zz=") if name != "venue" else IND_BASE.replace("venue=", "zz=")
    assert ind_error(query).details["parameter"] == "zz"
    pairs = [pair for pair in IND_BASE.split("&") if not pair.startswith(f"{name}=")]
    assert ind_error("&".join(pairs)).details["parameter"] == name


def test_indicators_empty_and_repeated_are_rejected() -> None:
    assert ind_error(f"{BASE}&indicators=").details["parameter"] == "indicators"
    assert ind_error(f"{IND_BASE}&indicators=rsi:period=7").details["parameter"] == "indicators"


@pytest.mark.parametrize(
    "value",
    ["foo", "rsi:period=0", "rsi:period=14;rsi:period=14", "rsi:bogus=1", "rsi:period=14;", "rsi:period=abc"],
)
def test_indicator_syntax_errors_surface_on_the_indicators_parameter(value: str) -> None:
    error = ind_error(f"{BASE}&indicators={value}")
    assert error.details["parameter"] == "indicators"
    assert error.details["received"] == value


def test_percent_encoded_indicators_equal_the_plain_form() -> None:
    plain = parse_ind(f"{BASE}&indicators=rsi:period=14;rsi:period=7")
    encoded = parse_ind(f"{BASE}&indicators=rsi%3Aperiod%3D14%3Brsi%3Aperiod%3D7")
    assert [s.id for s in encoded.indicators] == [s.id for s in plain.indicators]


def test_indicator_window_of_2016_slots_is_accepted_and_2017_rejected() -> None:
    assert parse_ind(f"{IND_BASE}&end={START + 2016 * TF}").end == START + 2016 * TF
    error = ind_error(f"{IND_BASE}&end={START + 2017 * TF}")
    assert (error.details["parameter"], error.details["maximum"]) == ("end", 2016)
    assert ind_error(f"{IND_BASE}&end={START}").details["parameter"] == "end"


def ind_request(end: int | None = None, start: int = START) -> IndicatorsRequest:
    return parse_ind(
        f"{BASE.replace(f'start={START}', f'start={start}')}&indicators=rsi:period=14" + (f"&end={end}" if end else "")
    )


DELAY = 15_000


def test_explicit_end_is_unchanged() -> None:
    assert (
        indicators_window_end(ind_request(START + 10 * TF), as_of_ms=0, publication_delay_ms=DELAY) == START + 10 * TF
    )


def test_default_end_is_the_newest_expected_slot_plus_timeframe() -> None:
    as_of = START + 10 * TF + TF + DELAY  # slot 10 has just become expected
    assert indicators_window_end(ind_request(), as_of_ms=as_of, publication_delay_ms=DELAY) == START + 11 * TF


def test_default_end_not_after_start_falls_back_to_one_slot() -> None:
    as_of = START - 5 * TF
    assert indicators_window_end(ind_request(), as_of_ms=as_of, publication_delay_ms=DELAY) == START + TF
    as_of = START + TF + DELAY  # newest expected is START itself: newest + tf == start + tf
    assert indicators_window_end(ind_request(), as_of_ms=as_of, publication_delay_ms=DELAY) == START + TF
    # start in the future of the newest expected slot
    assert indicators_window_end(ind_request(), as_of_ms=START - 100 * TF, publication_delay_ms=DELAY) == START + TF


def test_clock_before_the_first_expected_slot_gives_one_slot() -> None:
    assert indicators_window_end(ind_request(), as_of_ms=946684800000, publication_delay_ms=DELAY) == START + TF


def test_default_window_beyond_2016_slots_is_rejected_on_start() -> None:
    as_of = START + 3000 * TF
    with pytest.raises(ApiError) as caught:
        indicators_window_end(ind_request(), as_of_ms=as_of, publication_delay_ms=DELAY)
    assert caught.value.status == 400
    assert caught.value.details["parameter"] == "start"
    assert caught.value.details["maximum"] == 2016
    assert caught.value.details["received"] == str(START)
    assert caught.value.message == "the window up to the newest expected slot exceeds 2016 slots; pass an explicit end"
