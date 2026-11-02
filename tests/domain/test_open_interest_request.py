import pytest

from quasar_market_data.domain.open_interest_request import (
    MAX_PERIOD_ENDS,
    RETRIEVAL_HORIZON_MS,
    OpenInterestRequestError,
    validate_open_interest_request,
)
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, MIN_TIMESTAMP_MS
from tests.typing_helpers import untyped

P5 = Timeframe("5m")
STEP = P5.milliseconds
AS_OF = 1_793_620_800_000  # 2026-11-02T12:00:00Z, aligned to 5m
START = AS_OF - 3 * STEP


def test_constants() -> None:
    assert MAX_PERIOD_ENDS == 500
    assert RETRIEVAL_HORIZON_MS == 29 * 86_400_000


def test_single_period_end() -> None:
    assert validate_open_interest_request(START, START, P5, AS_OF) == 1


def test_returns_the_number_of_period_ends_including_both_bounds() -> None:
    assert validate_open_interest_request(START, START + 2 * STEP, P5, AS_OF) == 3


def test_500_period_ends_are_accepted() -> None:
    start = AS_OF - 499 * STEP
    assert validate_open_interest_request(start, AS_OF, P5, AS_OF) == 500


def test_501_period_ends_are_refused() -> None:
    start = AS_OF - 500 * STEP
    with pytest.raises(OpenInterestRequestError, match="501"):
        validate_open_interest_request(start, AS_OF, P5, AS_OF)


def test_unaligned_start_is_refused() -> None:
    with pytest.raises(OpenInterestRequestError, match="start"):
        validate_open_interest_request(START + 1, START + STEP, P5, AS_OF)


def test_unaligned_end_is_refused() -> None:
    with pytest.raises(OpenInterestRequestError, match="end"):
        validate_open_interest_request(START, START + STEP + 1, P5, AS_OF)


def test_start_after_end_is_refused() -> None:
    with pytest.raises(OpenInterestRequestError, match="after"):
        validate_open_interest_request(START + STEP, START, P5, AS_OF)


def test_start_exactly_at_the_horizon_is_accepted() -> None:
    as_of = AS_OF
    start = as_of - RETRIEVAL_HORIZON_MS
    assert start % STEP == 0
    assert validate_open_interest_request(start, start + STEP, P5, as_of) == 2


def test_start_one_period_before_the_horizon_is_refused() -> None:
    start = AS_OF - RETRIEVAL_HORIZON_MS - STEP
    with pytest.raises(OpenInterestRequestError, match="horizon"):
        validate_open_interest_request(start, start + STEP, P5, AS_OF)


def test_horizon_with_an_early_as_of_compares_correctly() -> None:
    as_of = MIN_TIMESTAMP_MS + 10 * STEP  # as_of - H lies before the first valid timestamp
    assert validate_open_interest_request(MIN_TIMESTAMP_MS, MIN_TIMESTAMP_MS + STEP, P5, as_of) == 2


@pytest.mark.parametrize("position", ["start", "end", "as_of"])
def test_out_of_range_timestamps_are_refused_as_request_errors(position: str) -> None:
    args = {"start": START, "end": START + STEP, "as_of": AS_OF}
    args[position] = MAX_TIMESTAMP_MS + STEP
    with pytest.raises(OpenInterestRequestError, match=position):
        validate_open_interest_request(args["start"], args["end"], P5, args["as_of"])


@pytest.mark.parametrize("bad", [True, 1.0, "1", None])
@pytest.mark.parametrize("position", ["start", "end", "as_of"])
def test_wrong_types_are_not_converted(bad: object, position: str) -> None:
    args: dict[str, object] = {"start": START, "end": START + STEP, "as_of": AS_OF}
    args[position] = bad
    with pytest.raises(TypeError):
        validate_open_interest_request(untyped(args["start"]), untyped(args["end"]), P5, untyped(args["as_of"]))
