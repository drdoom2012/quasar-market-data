"""The funding request: its path and its window (FR-6 design 4.1)."""

from urllib.parse import parse_qsl, urlsplit

import pytest

from tests.tools.symbol_overview.helpers import SYMBOL, T0, instrument
from tools.symbol_overview.fetch import ALLOWED_PATHS, funding_path, http_fetch
from tools.symbol_overview.model import DAY_MS, MIN_TS_MS, FundingRule, FundingWindow, Window
from tools.symbol_overview.window import funding_window, window_for

WINDOW = window_for(T0)
BEHIND = 7_230_000
RULE = FundingRule(30_000, BEHIND, 172_800_000, 365)


def rule(*, behind: int = BEHIND, backfill: int = 365) -> FundingRule:
    return FundingRule(30_000, behind, 172_800_000, backfill)


def test_the_window_runs_from_the_page_start_to_the_first_second_that_is_not_behind() -> None:
    window = funding_window(WINDOW, RULE, T0)

    assert window == FundingWindow(WINDOW.start, T0 - BEHIND + 1)


def test_end_is_exactly_the_oldest_verified_through_that_is_not_behind() -> None:
    # the API's rule: behind iff as_of - T >= behind_after_ms
    window = funding_window(WINDOW, RULE, T0)

    assert T0 - window.end < BEHIND  # a span ending at `end` is not behind
    assert T0 - (window.end - 1) >= BEHIND  # one millisecond earlier it is


def test_the_start_is_clamped_to_backfill_days_when_that_is_shorter_than_the_window() -> None:
    assert funding_window(WINDOW, rule(backfill=365), T0).start == WINDOW.start
    assert funding_window(WINDOW, rule(backfill=31), T0).start == WINDOW.start
    assert funding_window(WINDOW, rule(backfill=7), T0).start == T0 - 7 * DAY_MS
    assert funding_window(WINDOW, rule(backfill=1), T0).start == T0 - DAY_MS


def test_the_clamp_boundary() -> None:
    # S = T0 - 30 d - (T0 mod 5 min) - 5 min..., so the clamp starts to bind where T0 - n d > S
    days = (T0 - WINDOW.start) // DAY_MS
    assert funding_window(WINDOW, rule(backfill=days), T0).start == T0 - days * DAY_MS
    assert funding_window(WINDOW, rule(backfill=days + 1), T0).start == WINDOW.start


def test_an_empty_window_is_refused_and_a_one_millisecond_window_is_not() -> None:
    start = T0 - DAY_MS  # backfill 1 day
    assert funding_window(WINDOW, rule(behind=DAY_MS, backfill=1), T0) == FundingWindow(start, T0 - DAY_MS + 1)
    with pytest.raises(ValueError):
        funding_window(WINDOW, rule(behind=DAY_MS + 2, backfill=1), T0)  # end = start - 1


@pytest.mark.parametrize("bad", [rule(behind=-1), rule(backfill=0)], ids=["negative behind", "no backfill"])
def test_a_rule_that_cannot_make_a_window_is_refused(bad: FundingRule) -> None:
    with pytest.raises(ValueError):
        funding_window(WINDOW, bad, T0)


def test_a_window_outside_the_valid_range_is_refused() -> None:
    with pytest.raises(ValueError):
        funding_window(WINDOW, rule(), MIN_TS_MS + 1000)  # end before the valid range


def test_a_window_over_a_year_is_refused_and_exactly_a_year_is_not() -> None:
    far = Window(T0 - 366 * DAY_MS, T0)
    with pytest.raises(ValueError):
        funding_window(far, rule(behind=0, backfill=1000), T0)
    year = Window(T0 + 1 - 365 * DAY_MS, T0)  # end = T0 + 1 with behind 0: exactly 365 days
    assert funding_window(year, rule(behind=0, backfill=1000), T0) == FundingWindow(T0 + 1 - 365 * DAY_MS, T0 + 1)


def test_the_path_has_exactly_the_documented_parameters() -> None:
    window = FundingWindow(1000, 2000)
    path = funding_path(instrument(), window)

    split = urlsplit(path)
    assert split.path == "/v1/funding-rates"
    assert split.query == f"venue=binance-usdm&symbol={SYMBOL}&start=1000&end=2000&limit=2"
    assert dict(parse_qsl(split.query)) == {
        "venue": "binance-usdm",
        "symbol": SYMBOL,
        "start": "1000",
        "end": "2000",
        "limit": "2",
    }


def test_the_symbol_is_percent_encoded() -> None:
    odd = funding_path(instrument(symbol="A&B+C é"), FundingWindow(1, 2))

    assert "symbol=A%26B%2BC%20%C3%A9&" in odd


def test_the_path_is_allowed_and_a_near_miss_is_not() -> None:
    assert "/v1/funding-rates" in ALLOWED_PATHS
    fetch = http_fetch("http://127.0.0.1:1")
    for refused in ("/v1/funding-rates/", "/v1/funding", "/v1/funding-rates/x"):
        with pytest.raises(ValueError):
            fetch(refused)
