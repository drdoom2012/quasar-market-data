import pytest

from quasar_market_data.domain.funding_rate_capacity import (
    DEFAULT_BUDGET_PER_5MIN,
    LIMITER_WINDOW_MS,
    MAX_BUDGET_PER_5MIN,
    MAX_FULL_GROUPS,
    MAX_ROUND_OFFSET_MS,
    MIN_BUDGET_PER_5MIN,
    MIN_ROUND_OFFSET_MS,
    ROUND_LAST_START_BUDGET_MS,
    CapacityError,
    assess_capacity,
    full_groups_per_round,
    groups_k,
    history_catchup_share,
    isolated_round_capacity,
    planned_start_offset_ms,
    round_share,
    sustained_capacity,
    warning_threshold,
)
from tests.typing_helpers import untyped

OFFSET_BOUNDARIES = (60_000, 200_000, 200_001, 300_000, 505_000, 505_001, 600_000)
# Q at each boundary, written out by hand from the ADR (P-10, E-14) so the checks below do not reuse the code.
Q_AT = {60_000: 11, 200_000: 11, 200_001: 11, 300_000: 11, 505_000: 11, 505_001: 10, 600_000: 10}


def test_constants() -> None:
    assert (MIN_BUDGET_PER_5MIN, MAX_BUDGET_PER_5MIN, DEFAULT_BUDGET_PER_5MIN) == (50, 250, 150)
    assert (MIN_ROUND_OFFSET_MS, MAX_ROUND_OFFSET_MS) == (60_000, 600_000)
    assert LIMITER_WINDOW_MS == 305_000
    assert ROUND_LAST_START_BUDGET_MS == 3_555_000
    assert MAX_FULL_GROUPS == 11


@pytest.mark.parametrize(
    ("budget", "round_part", "other_part"), [(50, 25, 25), (150, 75, 75), (250, 125, 125), (151, 75, 76)]
)
def test_shares(budget: int, round_part: int, other_part: int) -> None:
    assert round_share(budget) == round_part
    assert history_catchup_share(budget) == other_part


def test_the_two_shares_always_sum_to_the_budget() -> None:
    for budget in range(MIN_BUDGET_PER_5MIN, MAX_BUDGET_PER_5MIN + 1):
        assert round_share(budget) + history_catchup_share(budget) == budget


@pytest.mark.parametrize(("budget", "sustained", "isolated"), [(250, 1_374, 1_499), (150, 824, 899), (50, 274, 299)])
def test_capacity_at_the_default_offset(budget: int, sustained: int, isolated: int) -> None:
    assert groups_k(60_000) == 11
    assert full_groups_per_round(60_000) == 11
    assert sustained_capacity(budget, 60_000) == sustained
    assert isolated_round_capacity(budget, 60_000) == isolated


@pytest.mark.parametrize(
    ("offset", "k", "q"),
    [
        (60_000, 11, 11),
        (200_000, 11, 11),
        (200_001, 10, 11),
        (300_000, 10, 11),
        (505_000, 10, 11),
        (505_001, 9, 10),
        (600_000, 9, 10),
    ],
)
def test_offset_boundaries(offset: int, k: int, q: int) -> None:
    assert groups_k(offset) == k
    assert full_groups_per_round(offset) == q


@pytest.mark.parametrize("offset", [59_999, 600_001, 0, -1])
def test_offset_range(offset: int) -> None:
    for function in (groups_k, full_groups_per_round):
        with pytest.raises(ValueError):
            function(offset)


def test_accept_and_refuse_at_the_boundary_for_every_share() -> None:
    for budget in range(MIN_BUDGET_PER_5MIN, MAX_BUDGET_PER_5MIN + 1):
        for offset in OFFSET_BOUNDARIES:
            sustained = Q_AT[offset] * (budget // 2) - 1
            assert sustained_capacity(budget, offset) == sustained
            assessment = assess_capacity(sustained, budget, offset)
            assert assessment.sustained == sustained
            with pytest.raises(CapacityError):
                assess_capacity(sustained + 1, budget, offset)


def test_group_arithmetic_is_consistent_with_the_window() -> None:
    offsets = {*range(MIN_ROUND_OFFSET_MS, MAX_ROUND_OFFSET_MS + 1, 1_000), *OFFSET_BOUNDARIES}
    for offset in offsets:
        k = groups_k(offset)
        q = full_groups_per_round(offset)
        assert offset + k * LIMITER_WINDOW_MS <= ROUND_LAST_START_BUDGET_MS < offset + (k + 1) * LIMITER_WINDOW_MS
        assert q * LIMITER_WINDOW_MS <= 3_600_000
        assert q <= 11


@pytest.mark.parametrize(("budget", "threshold"), [(50, 246), (150, 741), (250, 1_236)])
def test_warning_above_ninety_percent(budget: int, threshold: int) -> None:
    sustained = sustained_capacity(budget, 60_000)
    assert warning_threshold(sustained) == threshold
    at = assess_capacity(threshold, budget, 60_000)
    assert (at.warn_above, at.warn) == (threshold, False)
    assert assess_capacity(threshold + 1, budget, 60_000).warn is True


def test_the_659_usdt_perpetuals_fit_the_default_budget_without_a_warning() -> None:
    assessment = assess_capacity(659, DEFAULT_BUDGET_PER_5MIN, 60_000)
    assert assessment.warn is False


def test_refusal_names_the_values() -> None:
    with pytest.raises(CapacityError) as error:
        assess_capacity(825, 150, 60_000)
    message = str(error.value)
    for text in ("825", "150", "60000", "824"):
        assert text in message


@pytest.mark.parametrize(
    ("index", "expected"), [(0, 0), (74, 0), (75, 305_000), (100, 305_000), (300, 1_220_000), (659, 2_440_000)]
)
def test_planned_start_offset(index: int, expected: int) -> None:
    assert planned_start_offset_ms(index, 150) == expected


def test_planned_latency_table_of_the_adr() -> None:
    wake_offset = 60_000
    latest = {10: 60_000, 100: 365_000, 300: 1_280_000, 659: 2_500_000}
    for series, expected in latest.items():
        assert wake_offset + planned_start_offset_ms(series, 150) == expected


def test_argument_checks() -> None:
    for wrong in (True, 150.0, "150"):
        with pytest.raises(TypeError):
            round_share(untyped(wrong))
        with pytest.raises(TypeError):
            history_catchup_share(untyped(wrong))
        with pytest.raises(TypeError):
            sustained_capacity(untyped(wrong), 60_000)
        with pytest.raises(TypeError):
            sustained_capacity(150, untyped(wrong))
        with pytest.raises(TypeError):
            groups_k(untyped(wrong))
        with pytest.raises(TypeError):
            assess_capacity(untyped(wrong), 150, 60_000)
        with pytest.raises(TypeError):
            planned_start_offset_ms(untyped(wrong), 150)
    for bad_budget in (49, 251):
        with pytest.raises(ValueError):
            round_share(bad_budget)
    with pytest.raises(ValueError) as zero:
        assess_capacity(0, 150, 60_000)
    assert type(zero.value) is not CapacityError
    with pytest.raises(ValueError):
        planned_start_offset_ms(-1, 150)
    with pytest.raises(ValueError):
        warning_threshold(-1)
    with pytest.raises(TypeError):
        warning_threshold(untyped(True))


def test_planned_start_offset_uses_the_round_share_not_the_other_share() -> None:
    # b = 151: the round share is 75 and the history/catch-up share 76.
    assert planned_start_offset_ms(74, 151) == 0
    assert planned_start_offset_ms(75, 151) == 305_000
    assert planned_start_offset_ms(76, 151) == 305_000
