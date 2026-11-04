"""The open interest expectation rule of the API, derived from the configuration file (contract api-v1 6.5.7).

The Collector and the API read the same file and derive the cadence ``c`` with the same pure function
(``open_interest_cadence``), so they agree (ADR-0003 P-10). ``None`` means open interest is not enabled in the
configuration: the endpoint answers ``404 open_interest_disabled`` and ``/health/ingest`` carries ``null``.
"""

from quasar_market_data.config.settings import Settings
from quasar_market_data.domain.open_interest_coverage import OpenInterestRule, open_interest_rule
from quasar_market_data.domain.open_interest_holes import DAY_MS
from quasar_market_data.domain.timeframe import Timeframe


def rule_from_settings(settings: Settings) -> OpenInterestRule | None:
    table = settings.open_interest
    if table is None or not table.enabled:
        return None
    return open_interest_rule(
        len(settings.venues[0].symbols),  # the file validates exactly one venue (S20)
        table.budget_per_5min,
        table.round_offset_ms,
        settings.rule.lag_threshold_ms,
        Timeframe(table.period),
        table.backfill_days * DAY_MS,  # the depth of the file: the Collector and the API cannot disagree (A-4)
    )
