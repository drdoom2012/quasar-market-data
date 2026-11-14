"""Row readability (ADR-0005 P-1, P-4; contract api-v1 section 4.1): what a row's stamp, confirmation and the cut-over mean.

Pure: no clock, no I/O. The Collector stamps each write transaction after it holds the write lock and confirms it only
when ``COMMIT`` ended within ``READABLE_BOUND_MS`` of the stamp; the API turns the stored facts into the two served
fields with ``readable_by``.
"""

from typing import Final

from quasar_market_data.domain.timestamps import ensure_timestamp_ms

READABLE_BOUND_MS: Final = 2_000  # the contract constant B; within /v1 it may only grow, never shrink


def readable_by(stamp_ms: int, *, confirmed: bool, cutover_ms: int) -> tuple[int, bool]:
    """Return ``(readable_by_ms, readable_by_unconfirmed)`` for a stored row (the table of ADR-0005 P-4).

    A row stored before the cut-over (its stamp below ``cutover_ms``) was readable by the cut-over and is never
    unconfirmed. A later row is readable by its stamp plus the bound, and is unconfirmed without a confirmation, in
    which case the bound does not hold. A stamp equal to the cut-over cannot be stored (migration 4 puts the cut-over
    above every old stamp and the database refuses a new one at or below it), so it is refused instead of guessed.
    """
    ensure_timestamp_ms(stamp_ms, "stamp_ms")
    ensure_timestamp_ms(cutover_ms, "cutover_ms")
    if type(confirmed) is not bool:
        raise TypeError(f"confirmed must be bool, got {type(confirmed).__name__}")
    if stamp_ms < cutover_ms:
        return cutover_ms, False
    if stamp_ms == cutover_ms:
        raise ValueError(f"a stamp equal to the cut-over ({cutover_ms}) cannot be stored")
    return stamp_ms + READABLE_BOUND_MS, not confirmed
