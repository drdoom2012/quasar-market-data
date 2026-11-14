"""ADR-0005 P-4: what the API serves per row, from the stamp, the confirmation and the cut-over."""

import pytest

from quasar_market_data.domain.readability import READABLE_BOUND_MS, readable_by

CUTOVER = 1_792_600_000_000


def test_the_bound_is_the_contract_constant() -> None:
    assert READABLE_BOUND_MS == 2_000


@pytest.mark.parametrize("confirmed", [True, False])
def test_a_row_stored_before_the_cut_over_is_readable_by_the_cut_over_and_never_unconfirmed(confirmed: bool) -> None:
    assert readable_by(CUTOVER - 1, confirmed=confirmed, cutover_ms=CUTOVER) == (CUTOVER, False)
    assert readable_by(946_684_800_000, confirmed=confirmed, cutover_ms=CUTOVER) == (CUTOVER, False)


def test_a_confirmed_row_after_the_cut_over_is_readable_by_its_stamp_plus_the_bound() -> None:
    assert readable_by(CUTOVER + 1, confirmed=True, cutover_ms=CUTOVER) == (CUTOVER + 1 + READABLE_BOUND_MS, False)


def test_an_unconfirmed_row_after_the_cut_over_is_flagged() -> None:
    assert readable_by(CUTOVER + 5, confirmed=False, cutover_ms=CUTOVER) == (CUTOVER + 5 + READABLE_BOUND_MS, True)


def test_a_stamp_equal_to_the_cut_over_is_impossible_and_refused() -> None:
    with pytest.raises(ValueError, match="cut-over"):
        readable_by(CUTOVER, confirmed=True, cutover_ms=CUTOVER)


@pytest.mark.parametrize("bad", [True, 1.0, "1", None])
def test_non_integer_stamps_are_refused(bad: object) -> None:
    with pytest.raises(TypeError):
        readable_by(bad, confirmed=True, cutover_ms=CUTOVER)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        readable_by(CUTOVER + 1, confirmed=True, cutover_ms=bad)  # type: ignore[arg-type]
