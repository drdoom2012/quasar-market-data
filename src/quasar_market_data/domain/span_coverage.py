"""Slot states of a span from stored facts only (contract api-v1 section 4, clock-free subset)."""

from collections.abc import Set as AbstractSet
from dataclasses import dataclass


@dataclass(frozen=True)
class SpanCoverage:
    slots: int
    present: int
    conflicted: int
    unavailable: int
    missing: int
    beyond_stored: int
    conflicted_open_times: frozenset[int]

    @property
    def gap_free(self) -> bool:
        return self.conflicted == 0 and self.unavailable == 0 and self.missing == 0 and self.beyond_stored == 0


def classify_span(
    *,
    start: int,
    end: int,
    timeframe_ms: int,
    candles: AbstractSet[int],
    conflicts: AbstractSet[int],
    unavailable: AbstractSet[int],
    newest_open_time_ms: int,
) -> SpanCoverage:
    """Count the states of the slots of ``[start, end)``; each slot gets exactly one, the first that matches.

    Order: ``conflicted`` (a conflict record, or both a candle and an unavailable record), ``present``,
    ``unavailable``, ``missing`` (at or before the newest stored candle of the series), ``beyond_stored``.
    Raises ``ValueError`` for an empty or misaligned span and for any element outside it or misaligned,
    because a caller bug must not become a wrong count.
    """
    if timeframe_ms <= 0:
        raise ValueError(f"timeframe_ms must be positive, got {timeframe_ms}")
    if start % timeframe_ms != 0 or end % timeframe_ms != 0:
        raise ValueError("start and end must be aligned to the timeframe")
    if end <= start:
        raise ValueError(f"the span must not be empty, got [{start}, {end})")
    for name, members in (("candles", candles), ("conflicts", conflicts), ("unavailable", unavailable)):
        for open_time in members:
            if open_time % timeframe_ms != 0:
                raise ValueError(f"{name} contains the unaligned open time {open_time}")
            if not start <= open_time < end:
                raise ValueError(f"{name} contains {open_time}, outside [{start}, {end})")

    present = conflicted = unavailable_count = missing = beyond_stored = 0
    conflicted_open_times: set[int] = set()
    for open_time in range(start, end, timeframe_ms):
        has_candle = open_time in candles
        has_unavailable = open_time in unavailable
        if open_time in conflicts or (has_candle and has_unavailable):
            conflicted += 1
            conflicted_open_times.add(open_time)
        elif has_candle:
            present += 1
        elif has_unavailable:
            unavailable_count += 1
        elif open_time <= newest_open_time_ms:
            missing += 1
        else:
            beyond_stored += 1

    return SpanCoverage(
        slots=(end - start) // timeframe_ms,
        present=present,
        conflicted=conflicted,
        unavailable=unavailable_count,
        missing=missing,
        beyond_stored=beyond_stored,
        conflicted_open_times=frozenset(conflicted_open_times),
    )
