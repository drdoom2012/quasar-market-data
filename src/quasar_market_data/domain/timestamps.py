"""Timestamp guard for canonical epoch-millisecond values."""

from typing import Final

MIN_TIMESTAMP_MS: Final = 946_684_800_000  # 2000-01-01T00:00:00Z
MAX_TIMESTAMP_MS: Final = 4_102_444_800_000  # 2100-01-01T00:00:00Z


def ensure_timestamp_ms(value: int, name: str = "timestamp_ms") -> int:
    """Return ``value`` if it is a plain ``int`` within [MIN_TIMESTAMP_MS, MAX_TIMESTAMP_MS].

    Raises ``TypeError`` for non-``int`` values (``bool``, ``float`` and ``int`` subclasses included)
    and ``ValueError`` for out-of-range values.
    """
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if not MIN_TIMESTAMP_MS <= value <= MAX_TIMESTAMP_MS:
        raise ValueError(f"{name} must be within [{MIN_TIMESTAMP_MS}, {MAX_TIMESTAMP_MS}], got {value}")
    return value
