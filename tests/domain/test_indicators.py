import pytest

from quasar_market_data.domain.indicators import IndicatorSyntaxError, parse_indicators


def test_parses_two_rsi_specs_in_request_order() -> None:
    specs = parse_indicators("rsi:period=14;rsi:period=7")
    assert [s.id for s in specs] == ["rsi:period=14", "rsi:period=7"]
    assert specs[0].warmup_slots == 140
    assert specs[0].outputs == ("rsi",)
    assert specs[0].param("period") == 14


def test_leading_zero_normalizes_and_duplicates_are_rejected() -> None:
    assert parse_indicators("rsi:period=014")[0].id == "rsi:period=14"
    with pytest.raises(IndicatorSyntaxError):
        parse_indicators("rsi:period=14;rsi:period=014")


@pytest.mark.parametrize(
    "text",
    [
        "",
        ";",
        "rsi:period=14;",
        ";rsi:period=14",
        "rsi:period=14;;rsi:period=7",
        "RSI:period=14",
        "rsi",
        "rsi:",
        "rsi:period",
        "rsi:period=",
        "rsi:period=1",
        "rsi:period=101",
        "rsi:period=14,period=14",
        "rsi:period=14,length=3",
        "rsi:length=14",
        "sma:period=14",
        "rsi:period=+14",
        "rsi:period=1_4",
        "rsi:period=1.0",
        "rsi:period= 14",
        "rsi :period=14",
        "rsi:period=١٤",
        "rsi:period=12345678901234567",
    ],
)
def test_rejects(text: str) -> None:
    with pytest.raises(IndicatorSyntaxError):
        parse_indicators(text)


def test_rejects_more_than_ten_and_accepts_ten() -> None:
    ten = ";".join(f"rsi:period={p}" for p in range(2, 12))
    assert len(parse_indicators(ten)) == 10
    with pytest.raises(IndicatorSyntaxError):
        parse_indicators(ten + ";rsi:period=12")


def test_bounds_are_inclusive() -> None:
    assert [s.warmup_slots for s in parse_indicators("rsi:period=2;rsi:period=100")] == [20, 1000]


@pytest.mark.parametrize(
    ("text", "spec_id", "warmup", "outputs"),
    [
        ("ema:period=0200", "ema:period=200", 1200, ("ema",)),
        ("macd:signal=9,slow=26,fast=12", "macd:fast=12,slow=26,signal=9", 210, ("macd", "signal", "histogram")),
        ("atr:period=14", "atr:period=14", 140, ("atr",)),
        ("adx:period=14", "adx:period=14", 182, ("adx", "plus_di", "minus_di")),
        ("bbands:stddev=2,period=20", "bbands:period=20,stddev=2", 19, ("upper", "middle", "lower")),
    ],
)
def test_new_indicators_normalize(text: str, spec_id: str, warmup: int, outputs: tuple[str, ...]) -> None:
    (spec,) = parse_indicators(text)
    assert spec.id == spec_id
    assert spec.warmup_slots == warmup
    assert spec.outputs == outputs


def _bounds_cases() -> list[tuple[str, str]]:
    # (valid text, invalid text) pairs: each key at min, at max, one below, one above
    return [
        ("ema:period=2", "ema:period=1"),
        ("ema:period=200", "ema:period=201"),
        ("macd:fast=2,slow=3,signal=2", "macd:fast=1,slow=3,signal=2"),
        ("macd:fast=100,slow=200,signal=50", "macd:fast=101,slow=200,signal=50"),
        ("macd:fast=2,slow=3,signal=2", "macd:fast=2,slow=2,signal=2"),
        ("macd:fast=2,slow=200,signal=50", "macd:fast=2,slow=201,signal=50"),
        ("macd:fast=2,slow=3,signal=2", "macd:fast=2,slow=3,signal=1"),
        ("macd:fast=2,slow=3,signal=50", "macd:fast=2,slow=3,signal=51"),
        ("atr:period=2", "atr:period=1"),
        ("atr:period=100", "atr:period=101"),
        ("adx:period=2", "adx:period=1"),
        ("adx:period=100", "adx:period=101"),
        ("bbands:period=2,stddev=1", "bbands:period=1,stddev=1"),
        ("bbands:period=200,stddev=5", "bbands:period=201,stddev=5"),
        ("bbands:period=2,stddev=1", "bbands:period=2,stddev=0"),
        ("bbands:period=2,stddev=5", "bbands:period=2,stddev=6"),
    ]


@pytest.mark.parametrize(("valid", "invalid"), _bounds_cases())
def test_bounds_are_inclusive_for_every_key(valid: str, invalid: str) -> None:
    assert len(parse_indicators(valid)) == 1
    with pytest.raises(IndicatorSyntaxError):
        parse_indicators(invalid)


def test_largest_warmups() -> None:
    assert parse_indicators("macd:fast=100,slow=200,signal=50")[0].warmup_slots == 1500
    assert parse_indicators("adx:period=100")[0].warmup_slots == 1300
    assert parse_indicators("ema:period=200")[0].warmup_slots == 1200


@pytest.mark.parametrize(
    "text",
    [
        "macd:fast=26,slow=12,signal=9",
        "macd:fast=12,slow=12,signal=9",
        "macd:fast=12,slow=26",
        "bbands:period=20",
        "bbands:period=20,stddev=0",
        "bbands:period=20,stddev=6",
        "ema:period=1",
        "ema:period=201",
        "adx:period=101",
        "atr:length=14",
        "bbands:period=20,stddev=2.5",
        "EMA:period=14",
    ],
)
def test_rejects_new(text: str) -> None:
    with pytest.raises(IndicatorSyntaxError):
        parse_indicators(text)


def test_macd_constraint_message_names_the_rule() -> None:
    with pytest.raises(IndicatorSyntaxError, match="fast must be less than slow"):
        parse_indicators("macd:fast=26,slow=12,signal=9")


def test_mixed_ten_items_with_three_output_indicators_parse_in_order() -> None:
    text = (
        "rsi:period=14;ema:period=20;macd:fast=12,slow=26,signal=9;atr:period=14;adx:period=14;"
        "bbands:period=20,stddev=2;ema:period=50;macd:fast=5,slow=35,signal=5;adx:period=7;bbands:period=20,stddev=3"
    )
    specs = parse_indicators(text)
    assert [s.name for s in specs] == ["rsi", "ema", "macd", "atr", "adx", "bbands", "ema", "macd", "adx", "bbands"]
    assert specs[2].outputs == ("macd", "signal", "histogram")
    assert specs[4].outputs == ("adx", "plus_di", "minus_di")
    assert specs[5].outputs == ("upper", "middle", "lower")


def test_every_allowed_warmup_is_at_most_1500() -> None:
    worst = 0
    for period in range(2, 101):
        worst = max(
            worst, parse_indicators(f"rsi:period={period};atr:period={period};adx:period={period}")[2].warmup_slots
        )
    for period in range(2, 201):
        worst = max(worst, parse_indicators(f"ema:period={period}")[0].warmup_slots)
        worst = max(worst, parse_indicators(f"bbands:period={period},stddev=1")[0].warmup_slots)
    for slow in range(3, 201):
        for signal in range(2, 51):
            worst = max(worst, parse_indicators(f"macd:fast=2,slow={slow},signal={signal}")[0].warmup_slots)
    assert worst == 1500
