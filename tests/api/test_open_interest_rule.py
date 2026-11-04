"""The API derives the open interest expectation from the configuration file like the Collector (ADR-0003 P-10, P-15).

``tests/`` may import both sides: the import contracts cover ``src`` only. The Collector's own
``_open_interest_run`` is called on the same parsed ``Settings``, so a change of how it derives ``c`` fails here.
"""

from pathlib import Path
from typing import Any

import pytest

from quasar_market_data.api.open_interest_rule import rule_from_settings
from quasar_market_data.collector.cli import _open_interest_run
from quasar_market_data.config.settings import ConfigError, Settings, parse_settings
from quasar_market_data.domain.open_interest_coverage import (
    OpenInterestRule,
    OpenInterestSlotState,
    open_interest_slot_states,
)
from quasar_market_data.domain.open_interest_holes import newest_expected_ms, oldest_allowed_ms
from quasar_market_data.domain.open_interest_schedule import expected_after_ms
from quasar_market_data.domain.timeframe import Timeframe
from tests.config_files import config_text

BASE = Path("/cfg")
P5 = Timeframe("5m")
P = P5.milliseconds
AS_OF = 1_792_674_600_000


def symbols(count: int) -> tuple[str, ...]:
    return tuple(f"C{index:04d}/USDT:USDT" for index in range(count))


def settings_for(
    count: int,
    budget: int,
    *,
    enabled: bool = True,
    offset: int = 200_000,
    lag: int = 60_000,
    table: bool = True,
    backfill_days: int = 29,
) -> Settings:
    text = config_text(symbols=symbols(count), lag_threshold_ms=lag)
    if table:
        text += (
            "\n[open_interest]\n"
            f"enabled = {'true' if enabled else 'false'}\n"
            'period = "5m"\n'
            "publication_delay_ms = 15000\n"
            f"round_offset_ms = {offset}\n"
            f"budget_per_5min = {budget}\n"
            f"backfill_days = {backfill_days}\n"
        )
    return parse_settings(text, base_dir=BASE)


def fake_open_source(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("the open interest source must not be opened")


def test_an_absent_table_gives_no_rule() -> None:
    assert rule_from_settings(settings_for(10, 400, table=False)) is None


def test_a_disabled_table_gives_no_rule() -> None:
    assert rule_from_settings(settings_for(10, 400, enabled=False)) is None


def test_an_enabled_table_gives_the_rule_of_the_file() -> None:
    rule = rule_from_settings(settings_for(10, 400, offset=250_000, lag=90_000))

    assert rule == OpenInterestRule(
        cadence=1,
        expected_after_ms=P + 250_000,
        lag_threshold_ms=90_000,
        period=P5,
        retrieval_horizon_ms=29 * 86_400_000,
    )


@pytest.mark.parametrize(
    ("count", "budget"),
    [(10, 400), (150, 400), (300, 400), (600, 400), (10, 200), (600, 200), (10, 100), (36, 100), (37, 100), (432, 100)],
)
def test_the_api_and_the_collector_derive_the_same_cadence(count: int, budget: int) -> None:
    settings = settings_for(count, budget)

    rule = rule_from_settings(settings)
    run = _open_interest_run(settings, fake_open_source)

    assert rule is not None and run is not None
    assert rule.cadence == run.config.cadence
    assert rule.expected_after_ms == expected_after_ms(
        run.config.cadence, run.config.round_offset_ms, run.config.period
    )
    assert rule.period == run.config.period


def test_a_lower_budget_raises_the_cadence_for_the_same_universe() -> None:
    """S = 300: b = 400 -> steady 300, c = 3 (2 x 100 + 2); b = 200 -> steady 150, c = 5 (2 x 60 + 2; c = 4 needs 152)."""
    high = rule_from_settings(settings_for(300, 400))
    low = rule_from_settings(settings_for(300, 200))

    assert high is not None and low is not None
    assert (high.cadence, low.cadence) == (3, 5)


def test_a_universe_that_needs_a_cadence_above_twelve_never_reaches_the_api() -> None:
    with pytest.raises(ConfigError) as error:
        settings_for(433, 100)

    assert error.value.key == "open_interest.budget_per_5min"


@pytest.mark.parametrize("as_of", [AS_OF, AS_OF + 1, AS_OF + 150_000, AS_OF + 299_999])
@pytest.mark.parametrize(("count", "budget"), [(10, 400), (300, 400), (600, 200)])
def test_the_slots_the_api_expects_are_the_window_the_collector_requests(count: int, budget: int, as_of: int) -> None:
    """Neither ``no_longer_recoverable`` nor ``not_yet_expected`` == ``[oldest_allowed, newest_expected]``."""
    rule = rule_from_settings(settings_for(count, budget))
    assert rule is not None
    oldest = oldest_allowed_ms(as_of, P5)
    newest = newest_expected_ms(as_of, rule.cadence, 200_000, P5)
    start = oldest - 3 * P
    end = newest + 4 * P

    states = open_interest_slot_states(
        start=start,
        end=end,
        stored=set(),
        conflicts=set(),
        newest_time_ms=start - P,
        as_of_ms=as_of,
        rule=rule,
    )
    requestable = [
        time_ms
        for time_ms, state in states
        if state not in (OpenInterestSlotState.NO_LONGER_RECOVERABLE, OpenInterestSlotState.NOT_YET_EXPECTED)
    ]

    assert requestable == list(range(oldest, newest + P, P))


@pytest.mark.parametrize("days", [1, 29, 30])
def test_the_rule_carries_the_configured_depth_as_the_retrieval_horizon_and_the_collector_agrees(days: int) -> None:
    settings = settings_for(10, 400, backfill_days=days)

    rule = rule_from_settings(settings)

    assert rule is not None and rule.retrieval_horizon_ms == days * 86_400_000
    run = _open_interest_run(settings, fake_open_source)
    assert run is not None and run.config.retrieval_horizon_ms == rule.retrieval_horizon_ms  # one file, one depth
    # a slot older than the depth can no longer be requested: the Collector's oldest allowed slot is the rule's edge
    assert (
        oldest_allowed_ms(AS_OF, P5, run.config.retrieval_horizon_ms)
        == -(-(AS_OF - rule.retrieval_horizon_ms) // P) * P
    )


def test_a_depth_above_30_days_never_reaches_a_rule() -> None:
    with pytest.raises(ConfigError, match="backfill_days"):
        settings_for(10, 400, backfill_days=31)
