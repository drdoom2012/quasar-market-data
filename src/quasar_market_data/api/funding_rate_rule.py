"""The funding rate rule of the API, read from the configuration file (contract api-v1 6.7.7).

The Collector and the API read the same file. ``None`` means funding is not enabled: the endpoint answers
``404 funding_rate_disabled`` without opening the database and ``/health/ingest`` carries ``null``. Only the
publication delay and the history depth are needed (``recheck_ms`` is validated at load but not used here).
"""

from quasar_market_data.config.settings import Settings
from quasar_market_data.domain.funding_rate_coverage import FundingRateRule


def rule_from_settings(settings: Settings) -> FundingRateRule | None:
    table = settings.funding_rate
    if table is None or not table.enabled:
        return None
    return FundingRateRule(table.publication_delay_ms, table.backfill_days)
