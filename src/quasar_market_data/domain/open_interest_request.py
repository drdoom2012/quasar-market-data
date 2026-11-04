"""Pure rules for one open interest history request (ADR-0003 P-5).

Binance returns the *newest* ``limit`` rows of an over-long range, so paging forward would silently skip
rows. Every request therefore names both bounds, aligned to the period, and covers at most
``MAX_PERIOD_ENDS`` period ends including both bounds; the ``limit`` to send is exactly that count, so
truncation cannot happen even if Binance changed its rule. No request starts older than the retrieval horizon
before the server lower bound (observed edge: 29 days accepted, 31 days rejected with ``-1130``).

A violation is a caller bug or a range that is no longer retrievable: it is refused before any request.
"""

from typing import Final

from quasar_market_data.domain.open_interest import period_end_count
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import ensure_timestamp_ms

MAX_PERIOD_ENDS: Final = 500
MIN_BACKFILL_DAYS: Final = 1
MAX_BACKFILL_DAYS: Final = 30  # ADR-0003 A-4: the configured depth is at most 30 days, a larger value stops the process
DEFAULT_BACKFILL_DAYS: Final = 29  # ADR-0003 P-5 H: one day under the observed 30/31-day edge of Binance
RETRIEVAL_HORIZON_MS: Final = DEFAULT_BACKFILL_DAYS * 86_400_000  # the depth of the shipped file; a default argument
MAX_RETRIEVAL_HORIZON_MS: Final = MAX_BACKFILL_DAYS * 86_400_000  # no request is ever older than this


class OpenInterestRequestError(ValueError):
    """The requested range breaks a P-5 rule; nothing may be sent."""


def _timestamp(value: int, name: str) -> int:
    try:
        return ensure_timestamp_ms(value, name)
    except ValueError as error:  # a TypeError is a caller bug and propagates unchanged
        raise OpenInterestRequestError(str(error)) from error


def validate_open_interest_request(start_ms: int, end_ms: int, period: Timeframe, as_of_ms: int) -> int:
    """Return the number of period ends in ``[start_ms, end_ms]`` (the ``limit`` to send), or raise.

    ``as_of_ms`` is the caller's server-time lower bound; ``end_ms`` may lie after it (eligibility is judged
    by the caller after the response).
    """
    start = _timestamp(start_ms, "start_ms")
    end = _timestamp(end_ms, "end_ms")
    as_of = _timestamp(as_of_ms, "as_of_ms")
    step = period.milliseconds
    if start % step:
        raise OpenInterestRequestError(f"start_ms {start} is not aligned to {period.label}")
    if end % step:
        raise OpenInterestRequestError(f"end_ms {end} is not aligned to {period.label}")
    if start > end:
        raise OpenInterestRequestError(f"start_ms {start} is after end_ms {end}")
    count = period_end_count(start, end, period)
    if count > MAX_PERIOD_ENDS:
        raise OpenInterestRequestError(f"{count} period ends exceed the maximum of {MAX_PERIOD_ENDS}")
    if start < as_of - MAX_RETRIEVAL_HORIZON_MS:
        raise OpenInterestRequestError(
            f"start_ms {start} is older than the retrieval horizon of {MAX_RETRIEVAL_HORIZON_MS} ms before {as_of}"
        )
    return count
