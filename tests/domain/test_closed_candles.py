import pytest

from quasar_market_data.domain.candle import Candle
from quasar_market_data.domain.closed_candles import (
    DEFAULT_PUBLICATION_DELAY_MS,
    ClosedCandleSelection,
    InvalidCandle,
    is_closed_eligible,
    select_closed_candles,
)
from quasar_market_data.domain.server_time import TimeRejection, TrustedTime, UntrustedTime
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.validation import validate_candle
from tests.domain.test_candle import make_candle, make_key
from tests.typing_helpers import untyped

OPEN_MS = 1_700_000_100_000
END_MS = 1_700_000_400_000
DELAY_MS = 15_000
M5 = Timeframe("5m")
H1 = Timeframe("1h")


def candle_at(open_time_ms: int, timeframe: Timeframe = M5, **fields: str) -> Candle:
    return make_candle(key=make_key(timeframe=timeframe, open_time_ms=open_time_ms), **fields)


def test_boundary_is_eligible_and_one_millisecond_earlier_is_pending() -> None:
    candle = candle_at(OPEN_MS)
    assert is_closed_eligible(OPEN_MS, M5, DELAY_MS, END_MS + DELAY_MS)
    assert not is_closed_eligible(OPEN_MS, M5, DELAY_MS, END_MS + DELAY_MS - 1)

    at_boundary = select_closed_candles([candle], DELAY_MS, TrustedTime(END_MS + DELAY_MS))
    assert [c for c in at_boundary.eligible] == [candle]
    assert at_boundary.pending == ()

    one_earlier = select_closed_candles([candle], DELAY_MS, TrustedTime(END_MS + DELAY_MS - 1))
    assert one_earlier.eligible == ()
    assert one_earlier.pending == (candle,)


def test_zero_delay_makes_the_candle_end_the_boundary() -> None:
    assert is_closed_eligible(OPEN_MS, M5, 0, END_MS)
    assert not is_closed_eligible(OPEN_MS, M5, 0, END_MS - 1)


def test_each_candle_uses_its_own_timeframe() -> None:
    open_1h = 1_700_002_800_000  # aligned to 1h
    end_1h = open_1h + 3_600_000
    assert is_closed_eligible(open_1h, H1, DELAY_MS, end_1h + DELAY_MS)
    assert not is_closed_eligible(open_1h, H1, DELAY_MS, end_1h + DELAY_MS - 1)

    five_minute = candle_at(OPEN_MS)
    hourly = candle_at(open_1h, H1)
    selection = select_closed_candles([five_minute, hourly], DELAY_MS, TrustedTime(END_MS + 60_000))
    assert [c for c in selection.eligible] == [five_minute]
    assert selection.pending == (hourly,)


def test_mixed_batch_is_split_in_order() -> None:
    closed = candle_at(OPEN_MS - 300_000)
    boundary = candle_at(OPEN_MS)
    forming = candle_at(OPEN_MS + 300_000)
    selection = select_closed_candles([closed, boundary, forming], DELAY_MS, TrustedTime(END_MS + DELAY_MS))
    assert [c for c in selection.eligible] == [closed, boundary]
    assert selection.pending == (forming,)
    assert selection.invalid == ()
    assert selection.untrusted_reason is None
    assert len(selection.eligible) + len(selection.pending) + len(selection.invalid) == 3


@pytest.mark.parametrize("reason", list(TimeRejection))
def test_untrusted_time_selects_nothing(reason: TimeRejection) -> None:
    old = candle_at(OPEN_MS - 3_000_000)  # would be eligible under any trusted time
    broken = candle_at(OPEN_MS - 2_700_000, high="1", low="2")
    forming = candle_at(OPEN_MS)
    candles = [old, broken, forming]
    selection = select_closed_candles(candles, DELAY_MS, UntrustedTime(reason))
    assert selection.eligible == ()
    assert selection.invalid == ()
    assert selection.pending == tuple(candles)
    assert selection.untrusted_reason == reason


def test_invalid_eligible_candle_is_reported_not_selected() -> None:
    broken = candle_at(OPEN_MS, high="98", low="99")
    selection = select_closed_candles([broken], DELAY_MS, TrustedTime(END_MS + DELAY_MS))
    assert selection.eligible == ()
    assert selection.pending == ()
    assert selection.invalid == (InvalidCandle(candle=broken, violations=validate_candle(broken)),)
    assert selection.invalid[0].violations != ()


def test_invalid_candle_that_is_not_yet_eligible_is_pending() -> None:
    broken = candle_at(OPEN_MS, high="98", low="99")
    selection = select_closed_candles([broken], DELAY_MS, TrustedTime(END_MS + DELAY_MS - 1))
    assert selection.eligible == ()
    assert selection.invalid == ()
    assert selection.pending == (broken,)


def test_valid_and_invalid_keep_input_order_within_groups() -> None:
    first_ok = candle_at(OPEN_MS - 900_000)
    first_bad = candle_at(OPEN_MS - 600_000, high="98", low="99")
    second_ok = candle_at(OPEN_MS - 300_000)
    second_bad = candle_at(OPEN_MS, high="98", low="99")
    selection = select_closed_candles(
        [first_ok, first_bad, second_ok, second_bad], DELAY_MS, TrustedTime(END_MS + DELAY_MS)
    )
    assert [c for c in selection.eligible] == [first_ok, second_ok]
    assert [i.candle for i in selection.invalid] == [first_bad, second_bad]


@pytest.mark.parametrize("time", [TrustedTime(END_MS + DELAY_MS), UntrustedTime(TimeRejection.NO_OBSERVATION)])
def test_empty_input_gives_empty_selection(time: TrustedTime | UntrustedTime) -> None:
    selection = select_closed_candles([], DELAY_MS, time)
    assert (selection.eligible, selection.pending, selection.invalid) == ((), (), ())
    assert isinstance(selection, ClosedCandleSelection)


def test_negative_delay_is_rejected() -> None:
    with pytest.raises(ValueError):
        is_closed_eligible(OPEN_MS, M5, -1, END_MS)
    with pytest.raises(ValueError):
        select_closed_candles([], -1, TrustedTime(END_MS))


@pytest.mark.parametrize("bad", [True, 1.5])
def test_non_int_delay_is_rejected(bad: object) -> None:
    with pytest.raises(TypeError):
        is_closed_eligible(OPEN_MS, M5, untyped(bad), END_MS)
    with pytest.raises(TypeError):
        select_closed_candles([], untyped(bad), TrustedTime(END_MS))


def test_non_int_lower_bound_is_rejected() -> None:
    with pytest.raises(TypeError):
        is_closed_eligible(OPEN_MS, M5, DELAY_MS, untyped(1.5))


def test_default_publication_delay_matches_adr() -> None:
    assert DEFAULT_PUBLICATION_DELAY_MS == 15_000
