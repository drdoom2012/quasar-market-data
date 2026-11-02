import random

import pytest

from quasar_market_data.domain.open_interest_cadence import (
    MAX_BUDGET_PER_5MIN,
    MAX_CADENCE,
    MIN_BUDGET_PER_5MIN,
    CadenceError,
    backfill_share,
    open_interest_cadence,
    steady_budget,
    symbols_for_round,
)
from quasar_market_data.domain.timeframe import Timeframe
from tests.typing_helpers import untyped

T = 1_793_620_800_000  # (T // 300_000) % 3 == 0 and % 5 == 0
P5 = Timeframe("5m")
FIVE_MIN = 300_000


def largest_accepted(budget: int) -> int:
    return MAX_CADENCE * ((steady_budget(budget) - 2) // 2)


def test_constants() -> None:
    assert (MIN_BUDGET_PER_5MIN, MAX_BUDGET_PER_5MIN, MAX_CADENCE) == (100, 400, 12)
    assert steady_budget(400) == 300
    assert steady_budget(100) == 75


@pytest.mark.parametrize(("symbols", "expected"), [(1, 1), (149, 1), (150, 2), (300, 3), (600, 5), (1_788, 12)])
def test_cadence_at_default_budget(symbols: int, expected: int) -> None:
    assert open_interest_cadence(symbols, 400) == expected


def test_cadence_refuses_too_many_symbols() -> None:
    with pytest.raises(CadenceError, match="1788"):
        open_interest_cadence(1_789, 400)
    with pytest.raises(CadenceError, match="432"):
        open_interest_cadence(433, 100)


@pytest.mark.parametrize(
    ("symbols", "budget", "expected"), [(10, 100, 1), (36, 100, 1), (37, 100, 2), (432, 100, 12), (600, 200, 9)]
)
def test_cadence_at_lower_budgets(symbols: int, budget: int, expected: int) -> None:
    assert open_interest_cadence(symbols, budget) == expected


@pytest.mark.parametrize("budget", [100, 101, 199, 200, 300, 399, 400])
def test_backfill_share_is_at_least_a_quarter(budget: int) -> None:
    for symbols in range(1, largest_accepted(budget) + 1):
        assert backfill_share(symbols, budget) >= budget - steady_budget(budget)
    with pytest.raises(CadenceError):
        open_interest_cadence(largest_accepted(budget) + 1, budget)


@pytest.mark.parametrize(
    ("symbols", "budget", "expected"), [(10, 400, 378), (600, 400, 158), (10, 100, 78), (600, 200, 64)]
)
def test_backfill_share_values(symbols: int, budget: int, expected: int) -> None:
    assert backfill_share(symbols, budget) == expected


@pytest.mark.parametrize("budget", [99, 401, 0, -1])
def test_budget_out_of_range(budget: int) -> None:
    with pytest.raises(ValueError):
        steady_budget(budget)
    with pytest.raises(ValueError):
        open_interest_cadence(10, budget)


@pytest.mark.parametrize("budget", [250.0, "400", True])
def test_budget_wrong_type(budget: object) -> None:
    with pytest.raises(TypeError):
        open_interest_cadence(10, untyped(budget))


def test_symbol_count_checks() -> None:
    with pytest.raises(ValueError) as error:
        open_interest_cadence(0, 400)
    assert type(error.value) is not CadenceError
    with pytest.raises(TypeError):
        open_interest_cadence(untyped(True), 400)


def test_every_symbol_once_per_cadence_rounds() -> None:
    symbols = [f"S{i:04d}/USDT:USDT" for i in range(600)]
    rounds = [symbols_for_round(symbols, T + k * FIVE_MIN, 5, P5) for k in range(5)]
    assert [len(r) for r in rounds] == [120] * 5
    flat = [s for r in rounds for s in r]
    assert sorted(flat) == sorted(symbols)


def test_round_sizes_and_order() -> None:
    symbols = [f"{letter}/USDT:USDT" for letter in "ABCDEFG"]
    rounds = [symbols_for_round(symbols, T + k * FIVE_MIN, 3, P5) for k in range(3)]
    assert [len(r) for r in rounds] == [3, 2, 2]
    assert rounds[0] == (symbols[0], symbols[3], symbols[6])
    assert symbols_for_round(symbols, T + 3 * FIVE_MIN, 3, P5) == rounds[0]


def test_assignment_ignores_input_order() -> None:
    symbols = [f"S{i:02d}/USDT:USDT" for i in range(20)]
    shuffled = symbols[:]
    random.Random(7).shuffle(shuffled)
    for k in range(4):
        assert symbols_for_round(shuffled, T + k * FIVE_MIN, 4, P5) == symbols_for_round(
            symbols, T + k * FIVE_MIN, 4, P5
        )


def test_empty_symbols() -> None:
    assert symbols_for_round([], T, 1, P5) == ()


def test_round_argument_checks() -> None:
    good = ["A/USDT:USDT", "B/USDT:USDT"]
    for args in [
        (good, T + 1, 1),
        (["A/USDT:USDT", "A/USDT:USDT"], T, 1),
        (good, T, 0),
        (good, T, 13),
        (["A/USDT:USDT", ""], T, 1),
    ]:
        with pytest.raises(ValueError):
            symbols_for_round(args[0], args[1], args[2], P5)
    with pytest.raises(TypeError):
        symbols_for_round(good, T, untyped(True), P5)
    with pytest.raises(TypeError):
        symbols_for_round(untyped(["A/USDT:USDT", 1]), T, 1, P5)
