"""Retry delays and suspension of a failing funding rate series (ADR-0004 P-10).

A failed request (network, ``SourceError``, parse error, write failure, unknown outcome) is counted per series. The
first two consecutive failures are retried after 1 s and 2 s (inside the same class share); the third consecutive
failure suspends the series for 6 h, repeats for 12 h and then 24 h. When a suspension has expired the next request
is one probe: a success clears everything, a failure suspends again with the next duration. A ``429`` or ``418`` is
not a series failure and never reaches this module.

All times are monotonic milliseconds. Integer arithmetic only.
"""

from dataclasses import dataclass
from typing import Final

RETRY_DELAYS_MS: Final = (1_000, 2_000)
SUSPEND_MS: Final = (21_600_000, 43_200_000, 86_400_000)
FAILURES_TO_SUSPEND: Final = 3


@dataclass(frozen=True)
class FailureRecord:
    consecutive: int = 0
    suspensions: int = 0  # suspensions since the last success; above 0 the next failure is a failed probe
    suspended_until_ms: int = 0
    retry_at_ms: int = 0


def on_failure(record: FailureRecord, now_ms: int) -> FailureRecord:
    consecutive = record.consecutive + 1
    if consecutive >= FAILURES_TO_SUSPEND or record.suspensions > 0:
        duration = SUSPEND_MS[min(record.suspensions, len(SUSPEND_MS) - 1)]
        return FailureRecord(0, record.suspensions + 1, now_ms + duration, 0)
    return FailureRecord(
        consecutive, record.suspensions, record.suspended_until_ms, now_ms + RETRY_DELAYS_MS[consecutive - 1]
    )


def on_success(record: FailureRecord) -> FailureRecord:
    return FailureRecord()


def unavailable_until_ms(record: FailureRecord) -> int:
    """The series is not eligible for any request before this instant (0: always eligible)."""
    return max(record.suspended_until_ms, record.retry_at_ms)
