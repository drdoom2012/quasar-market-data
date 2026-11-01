from itertools import pairwise

import pytest

from tests.tools.symbol_overview.helpers import T0
from tools.symbol_overview.model import BUCKET_SLOTS, BUCKETS, MAX_TS_MS, MIN_TS_MS, TF_MS, WINDOW_SLOTS, Run
from tools.symbol_overview.window import build_strip, window_for

W = window_for(T0)
S = W.start
E = W.end
BUCKET_MS = BUCKET_SLOTS * TF_MS


def run(state: str, first_slot: int, slots: int = 1) -> Run:
    return Run(state, S + first_slot * TF_MS, S + (first_slot + slots) * TF_MS)


# window_for


def test_window_for_slot_boundary_includes_the_slot_that_contains_the_time() -> None:
    assert window_for(T0).end == T0 + TF_MS
    assert window_for(T0 - 1).end == T0
    assert window_for(T0 + 1).end == T0 + TF_MS


@pytest.mark.parametrize("as_of", [T0, T0 - 1, T0 + 1, T0 + TF_MS - 1])
def test_window_is_exactly_30_days_and_aligned(as_of: int) -> None:
    window = window_for(as_of)
    assert window.end - window.start == WINDOW_SLOTS * TF_MS
    assert window.start % TF_MS == 0
    assert window.end % TF_MS == 0


def test_window_for_the_last_valid_time_ends_one_timeframe_past_it() -> None:
    assert window_for(MAX_TS_MS).end == MAX_TS_MS + TF_MS


@pytest.mark.parametrize("as_of", [MIN_TS_MS - 1, MAX_TS_MS + 1])
def test_window_for_rejects_out_of_range_time(as_of: int) -> None:
    with pytest.raises(ValueError):
        window_for(as_of)


# build_strip


def test_strip_is_720_chars() -> None:
    assert BUCKETS == 720
    assert len(build_strip([], S, W)) == BUCKETS


def test_all_present_is_dots() -> None:
    assert build_strip([], S, W) == "." * BUCKETS


def test_one_slot_hole_is_visible_in_its_bucket() -> None:
    strip = build_strip([run("missing", 5 * BUCKET_SLOTS + 3)], S, W)
    assert strip[5] == "m"
    assert strip.replace("m", ".") == "." * BUCKETS


def test_pre_history_buckets() -> None:
    oldest = S + 100 * TF_MS
    strip = build_strip([Run("missing", S, oldest)], oldest, W)
    assert strip[:8] == "_" * 8
    assert strip[8:] == "." * (BUCKETS - 8)  # bucket 8 holds slots 96..107, history starts at slot 100


def test_conflicted_slot_before_oldest_shows_in_the_pre_history_zone() -> None:
    oldest = S + 100 * TF_MS
    runs = [run("missing", 0, 10), run("conflicted", 10), run("missing", 11, 89)]
    strip = build_strip(runs, oldest, W)
    assert strip[0] == "c"
    assert strip[1:8] == "_" * 7


def test_a_missing_run_after_oldest_is_a_hole_even_when_it_touches_the_pre_history() -> None:
    oldest = S + 100 * TF_MS
    strip = build_strip([Run("missing", S, oldest), run("missing", 100, 2)], oldest, W)
    assert strip[8] == "m"


def test_oldest_before_the_window_means_no_pre_history() -> None:
    assert build_strip([], S - 50 * TF_MS, W) == "." * BUCKETS


RANKED_RUN_STATES = ["not_yet_expected", "due", "unavailable", "missing", "conflicted", "stale"]
CHARS = {
    "not_yet_expected": "n",
    "due": "d",
    "unavailable": "u",
    "missing": "m",
    "conflicted": "c",
    "stale": "s",
}


@pytest.mark.parametrize(("lower", "higher"), list(pairwise(RANKED_RUN_STATES)))
def test_worst_state_wins_within_a_bucket(lower: str, higher: str) -> None:
    low_first = build_strip([run(lower, 36), run(higher, 37)], S, W)
    high_first = build_strip([run(higher, 36), run(lower, 37)], S, W)
    assert low_first[3] == high_first[3] == CHARS[higher]


def test_any_state_ranks_above_present() -> None:
    assert build_strip([run("not_yet_expected", 0)], S, W)[0] == "n"


def test_trailing_runs() -> None:
    assert build_strip([run("not_yet_expected", WINDOW_SLOTS - 1)], S, W)[-1] == "n"
    strip = build_strip([run("stale", WINDOW_SLOTS - 40, 40)], S, W)
    assert strip[-4:] == "ssss"  # slots 8600..8639 touch buckets 716..719 (bucket 716 holds 8592..8603)
    assert strip[-5] == "."


@pytest.mark.parametrize(
    "bad",
    [
        Run("missing", S - TF_MS, S + TF_MS),  # starts before the window
        Run("missing", E - TF_MS, E + TF_MS),  # ends after the window
        Run("missing", S + TF_MS, S + TF_MS),  # empty
        Run("missing", S + 1, S + TF_MS),  # misaligned start
        Run("missing", S, S + TF_MS + 1),  # misaligned end
    ],
)
def test_strip_rejects_runs_outside_the_window_or_malformed(bad: Run) -> None:
    with pytest.raises(ValueError):
        build_strip([bad], S, W)
