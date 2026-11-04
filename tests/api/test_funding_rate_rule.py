"""The API reads the funding rate table of the same file as the Collector (contract api-v1 6.7.7, ADR-0004 P-12)."""

from pathlib import Path

from quasar_market_data.api.funding_rate_rule import rule_from_settings
from quasar_market_data.config.settings import Settings, parse_settings
from quasar_market_data.domain.funding_rate_coverage import FundingRateRule
from tests.config_files import TEN_SYMBOLS, config_text

BASE = Path("/cfg")


def settings_for(table: str | None) -> Settings:
    text = config_text(symbols=TEN_SYMBOLS)
    if table is not None:
        text += "\n" + table
    return parse_settings(text, base_dir=BASE)


def table(*, enabled: bool = True, delay: int = 45_000, backfill_days: int = 20) -> str:
    return (
        "[funding_rate]\n"
        f"enabled = {'true' if enabled else 'false'}\n"
        f"publication_delay_ms = {delay}\n"
        "round_offset_ms = 60000\n"
        "budget_per_5min = 150\n"
        f"backfill_days = {backfill_days}\n"
        "recheck_ms = 86400000\n"
    )


def test_an_absent_table_gives_no_rule() -> None:
    assert rule_from_settings(settings_for(None)) is None


def test_a_disabled_table_gives_no_rule() -> None:
    assert rule_from_settings(settings_for(table(enabled=False))) is None


def test_an_enabled_table_gives_the_delay_and_the_depth_of_the_file() -> None:
    rule = rule_from_settings(settings_for(table(delay=45_000, backfill_days=20)))

    assert rule == FundingRateRule(publication_delay_ms=45_000, backfill_days=20)
