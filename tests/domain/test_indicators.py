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
        "ema:period=14",
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
