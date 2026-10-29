"""The bounds the API puts on the configured expectation rule (contract 6.1.5). The rule itself has no runtime
default: it comes from the configuration file.

The domain's ``ExpectationRule`` only requires non-negative ``int`` values; a very large lag threshold would
keep every slot ``due`` and never ``stale``. The API therefore bounds both values. Type and sign rules stay
in the domain: the single checks build a rule to reuse them, then compare against the bound.
"""

from typing import Final

from quasar_market_data.domain.timed_coverage import ExpectationRule

MAX_PUBLICATION_DELAY_MS: Final = 300_000
MAX_LAG_THRESHOLD_MS: Final = 900_000


def check_publication_delay(value: int) -> int:
    ExpectationRule(value, 0)  # TypeError for non-int or bool, ValueError for a negative value
    if value > MAX_PUBLICATION_DELAY_MS:
        raise ValueError(f"publication_delay_ms must be <= {MAX_PUBLICATION_DELAY_MS}, got {value}")
    return value


def check_lag_threshold(value: int) -> int:
    ExpectationRule(0, value)  # TypeError for non-int or bool, ValueError for a negative value
    if value > MAX_LAG_THRESHOLD_MS:
        raise ValueError(f"lag_threshold_ms must be <= {MAX_LAG_THRESHOLD_MS}, got {value}")
    return value


def checked_rule(publication_delay_ms: int, lag_threshold_ms: int) -> ExpectationRule:
    check_publication_delay(publication_delay_ms)
    check_lag_threshold(lag_threshold_ms)
    return ExpectationRule(publication_delay_ms, lag_threshold_ms)
