"""The shifted open interest window, its strip and its request path (OI-6b, design 8.1 and 8.6)."""

from itertools import pairwise

import pytest

from tests.tools.symbol_overview.helpers import SYMBOL, instrument
from tests.tools.symbol_overview.oi_helpers import E1, OI_WINDOW, S1, WINDOW, slot
from tools.symbol_overview.fetch import ALLOWED_PATHS, coverage_path, http_fetch, open_interest_path
from tools.symbol_overview.model import BUCKET_SLOTS, BUCKETS, TF_MS, WINDOW_SLOTS, Run, Window
from tools.symbol_overview.window import build_open_interest_strip, open_interest_window

BUCKET_MS = BUCKET_SLOTS * TF_MS


def run(state: str, first: int, last_exclusive: int) -> Run:
    return Run(state, slot(first), slot(last_exclusive))


def test_the_open_interest_window_is_shifted_by_one_period() -> None:
    shifted = open_interest_window(Window(WINDOW.start, WINDOW.end))

    assert shifted == Window(WINDOW.start + TF_MS, WINDOW.end + TF_MS)
    assert (shifted.end - shifted.start) // TF_MS == WINDOW_SLOTS == 8640


def test_a_bucket_of_both_strips_is_the_same_hour() -> None:
    for k in (0, 1, 359, 719):
        first_slot = S1 + k * BUCKET_MS  # the first slot of open interest bucket k: the END of a period
        period_start = first_slot - TF_MS  # the candle that period belongs to
        assert (first_slot - OI_WINDOW.start) // BUCKET_MS == (period_start - WINDOW.start) // BUCKET_MS == k
        last_slot = first_slot + (BUCKET_SLOTS - 1) * TF_MS
        assert (last_slot - TF_MS - WINDOW.start) // BUCKET_MS == k  # the last slot of the bucket: same candle hour


def test_the_strip_has_720_characters_and_the_alphabet_of_open_interest() -> None:
    assert build_open_interest_strip([], S1, OI_WINDOW) == "." * BUCKETS
    states = {
        "no_longer_recoverable": "r",
        "not_yet_expected": "n",
        "due": "d",
        "missing": "m",
        "conflicted": "c",
        "stale": "s",
    }
    for index, (state, char) in enumerate(states.items()):
        strip = build_open_interest_strip([run(state, BUCKET_SLOTS * index, BUCKET_SLOTS * index + 1)], S1, OI_WINDOW)
        assert len(strip) == BUCKETS
        assert strip[index] == char
        assert strip.count(char) == 1


def test_the_worst_state_of_a_bucket_wins_in_the_documented_order() -> None:
    order = ["no_longer_recoverable", "not_yet_expected", "due", "missing", "conflicted", "stale"]  # r n d m c s
    chars = dict(zip(order, "rndmcs", strict=True))
    for low, high in pairwise(order):
        for runs in ([run(low, 0, 1), run(high, 1, 2)], [run(high, 0, 1), run(low, 1, 2)]):
            assert build_open_interest_strip(runs, S1, OI_WINDOW)[0] == chars[high]
    assert build_open_interest_strip([run("no_longer_recoverable", 0, 1)], S1, OI_WINDOW)[0] == "r"  # r outranks "."


def test_old_holes_are_clipped_at_the_oldest_row_inside_a_bucket() -> None:
    oldest = slot(18)  # inside bucket 1 (slots 12 to 23)
    for state in ("missing", "no_longer_recoverable"):
        strip = build_open_interest_strip([run(state, 0, 18)], oldest, OI_WINDOW)
        assert strip[0] == "_"  # the bucket ends before the oldest row
        assert strip[1] == "."  # the part before the oldest row is pre-history, not a hole
        assert strip[2] == "."
    assert build_open_interest_strip([run("no_longer_recoverable", 19, 20)], oldest, OI_WINDOW)[1] == "r"
    assert build_open_interest_strip([run("conflicted", 13, 14)], oldest, OI_WINDOW)[1] == "c"
    assert build_open_interest_strip([run("not_yet_expected", 13, 14)], oldest, OI_WINDOW)[1] == "n"
    whole = build_open_interest_strip([], slot(24), OI_WINDOW)
    assert whole[:2] == "__" and whole[2] == "."


@pytest.mark.parametrize(
    "bad",
    [
        Run("missing", slot(0) - TF_MS, slot(1)),  # starts before the window
        Run("missing", slot(8639), E1 + TF_MS),  # ends after the window
        Run("missing", slot(0) + 1, slot(1)),  # unaligned
        Run("missing", slot(2), slot(2)),  # empty
        Run("present", slot(0), slot(1)),  # not a non-present state
        Run("unavailable", slot(0), slot(1)),  # a candle state
    ],
)
def test_a_run_outside_the_window_or_misaligned_is_refused(bad: Run) -> None:
    with pytest.raises(ValueError):
        build_open_interest_strip([bad], S1, OI_WINDOW)


def test_open_interest_path_has_exactly_the_documented_parameters() -> None:
    path = open_interest_path(instrument(), OI_WINDOW)

    assert path == (
        f"/v1/open-interest/coverage?venue=binance-usdm&symbol={SYMBOL}&period=5m"
        f"&start={OI_WINDOW.start}&end={OI_WINDOW.end}"
    )
    odd = open_interest_path(instrument(symbol="A&B+C é"), OI_WINDOW)
    assert "symbol=A%26B%2BC%20%C3%A9&" in odd
    assert coverage_path(instrument(symbol="A&B+C é"), WINDOW).count("symbol=A%26B%2BC%20%C3%A9&") == 1


def test_the_new_path_is_allowed_and_everything_else_still_is_not() -> None:
    assert ALLOWED_PATHS == frozenset(
        {"/health/ingest", "/v1/instruments", "/v1/coverage", "/v1/open-interest/coverage"}
    )
    fetch = http_fetch("http://127.0.0.1:1")
    for refused in ("/v1/candles", "/v1/open-interest", "/v1/indicators", "/v1/open-interest/coverage/"):
        with pytest.raises(ValueError):
            fetch(refused)
