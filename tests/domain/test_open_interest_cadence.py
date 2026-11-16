import random

import pytest

from quasar_market_data.collector.binance_usdm_open_interest import MAX_IN_FLIGHT
from quasar_market_data.domain.open_interest_cadence import (
    MAX_BUDGET_PER_5MIN,
    MAX_CADENCE,
    MIN_BUDGET_PER_5MIN,
    RETRY_RESERVE,
    ROUND_IN_FLIGHT,
    CadenceError,
    backfill_share,
    largest_symbol_count,
    open_interest_cadence,
    pace_limit_ms,
    retry_reserve,
    round_cost,
    steady_budget,
    symbols_for_round,
)
from quasar_market_data.domain.timeframe import Timeframe
from tests.typing_helpers import untyped

T = 1_793_620_800_000  # (T // 300_000) % 3 == 0 and % 5 == 0
P5 = Timeframe("5m")
FIVE_MIN = 300_000


def test_constants() -> None:
    assert (MIN_BUDGET_PER_5MIN, MAX_BUDGET_PER_5MIN, MAX_CADENCE) == (100, 400, 12)
    assert steady_budget(400) == 300
    assert steady_budget(100) == 75


def test_constants_of_the_shared_reserve() -> None:
    assert RETRY_RESERVE == 50
    assert ROUND_IN_FLIGHT == 2


def test_the_domain_in_flight_count_equals_the_collectors() -> None:
    # The domain must not import the collector; this pins that the two constants cannot drift apart.
    assert MAX_IN_FLIGHT == ROUND_IN_FLIGHT


@pytest.mark.parametrize(
    ("symbols", "budget", "expected", "share"),
    [
        (10, 100, 1, None),
        (36, 100, 1, None),
        (37, 100, 2, None),
        (432, 100, 12, None),
        (200, 400, 1, 148),
        (248, 400, 1, None),
        (249, 400, 2, None),
        (522, 400, 3, 174),
        (600, 400, 3, 148),
        (200, 200, 3, 81),
        (522, 200, 6, 61),
        (600, 200, 7, 62),
        (200, 100, 6, 30),
        (2_976, 400, 12, 100),
    ],
)
def test_fixed_points(symbols: int, budget: int, expected: int, share: int | None) -> None:
    assert open_interest_cadence(symbols, budget) == expected
    if share is not None:
        assert backfill_share(symbols, budget) == share


def test_433_symbols_at_100_are_refused() -> None:
    with pytest.raises(CadenceError):
        open_interest_cadence(433, 100)


def test_largest_symbol_count() -> None:
    assert largest_symbol_count(400) == 2_976
    assert largest_symbol_count(200) == 1_176
    assert largest_symbol_count(100) == 432
    for budget, largest in [(400, 2_976), (200, 1_176), (100, 432)]:
        open_interest_cadence(largest, budget)
        with pytest.raises(CadenceError, match=str(largest)):
            open_interest_cadence(largest + 1, budget)


@pytest.mark.parametrize(("round_size", "expected"), [(1, 1), (49, 49), (50, 50), (51, 50), (200, 50)])
def test_retry_reserve(round_size: int, expected: int) -> None:
    assert retry_reserve(round_size) == expected


@pytest.mark.parametrize("round_size", [0, -1])
def test_retry_reserve_refuses_an_empty_round(round_size: int) -> None:
    with pytest.raises(ValueError):
        retry_reserve(round_size)


@pytest.mark.parametrize("round_size", [True, 1.0, "1"])
def test_retry_reserve_refuses_a_wrong_type(round_size: object) -> None:
    with pytest.raises(TypeError):
        retry_reserve(untyped(round_size))


@pytest.mark.parametrize(("round_size", "expected"), [(200, 60_000), (131, 39_600), (1, 600), (2, 600), (3, 1_200)])
def test_pace_limit(round_size: int, expected: int) -> None:
    assert pace_limit_ms(round_size) == expected


@pytest.mark.parametrize("round_size", [0, -1])
def test_pace_limit_refuses_an_empty_round(round_size: int) -> None:
    with pytest.raises(ValueError):
        pace_limit_ms(round_size)


def test_pace_limit_refuses_a_wrong_type() -> None:
    with pytest.raises(TypeError):
        pace_limit_ms(untyped(True))


def test_round_cost_is_requests_reserve_and_time_calls() -> None:
    assert round_cost(200, 1) == 200 + 50 + 2
    assert round_cost(40, 1) == 40 + 40 + 2
    assert round_cost(522, 3) == 174 + 50 + 2


def _legacy_cadence(symbols: int, budget: int) -> int:
    """The per-symbol reserve of ADR-0003 P-10 before A-6, kept here as the oracle: ``2 * ceil(S / c) + 2``."""
    steady = budget * 3 // 4
    for c in range(1, MAX_CADENCE + 1):
        if 2 * -(-symbols // c) + 2 <= steady:
            return c
    return MAX_CADENCE + 1


def test_every_budget_and_symbol_count() -> None:
    """Spec section 5, properties (1) to (6), for every integer budget and every symbol count up to largest + 1."""

    def cost(symbols: int, c: int) -> int:
        """The round cost of ADR-0003 P-10 (A-6) written out, an oracle independent of ``round_cost``."""
        n = -(-symbols // c)
        return n + min(50, n) + 2

    refused = MAX_CADENCE + 1
    previous_budget: list[int] = []
    for budget in range(MIN_BUDGET_PER_5MIN, MAX_BUDGET_PER_5MIN + 1):
        steady = steady_budget(budget)
        largest = largest_symbol_count(budget)
        # the closed form of the spec, written independently of the implementation
        n_at_12 = steady - 52 if steady >= 102 else (steady - 2) // 2
        assert largest == MAX_CADENCE * n_at_12
        current: list[int] = []
        last_c = 1
        for symbols in range(1, largest + 2):
            try:
                c = open_interest_cadence(symbols, budget)
            except CadenceError:
                c = refused
            current.append(c)
            # (2) 1 <= c <= 12 and refused exactly above the closed form
            assert (c == refused) == (symbols > largest)
            if c == refused:
                # nothing up to 12 fits
                assert cost(symbols, MAX_CADENCE) > steady
                continue
            assert 1 <= c <= MAX_CADENCE
            # (1) c fits and is minimal
            assert cost(symbols, c) <= steady
            assert c == 1 or cost(symbols, c - 1) > steady
            # (3) never above the legacy cadence
            assert c <= _legacy_cadence(symbols, budget)
            # (4) the backfill keeps at least b - steady(b), at least 25; (5) round cost plus share is b
            share = backfill_share(symbols, budget)
            assert share >= budget - steady >= 25
            assert cost(symbols, c) + share == budget
            # (6) non-decreasing in S
            assert c >= last_c
            last_c = c
            # (6) non-increasing in b: the previous (smaller) budget needed at least this cadence
            if symbols <= len(previous_budget):
                assert c <= previous_budget[symbols - 1]
        previous_budget = current


@pytest.mark.parametrize("budget", [100, 101, 199, 200, 300, 399, 400])
def test_backfill_share_is_at_least_a_quarter(budget: int) -> None:
    for symbols in range(1, largest_symbol_count(budget) + 1):
        assert backfill_share(symbols, budget) >= budget - steady_budget(budget)
    with pytest.raises(CadenceError):
        open_interest_cadence(largest_symbol_count(budget) + 1, budget)


@pytest.mark.parametrize(
    ("symbols", "budget", "expected"), [(10, 400, 378), (600, 400, 148), (10, 100, 78), (600, 200, 62)]
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
