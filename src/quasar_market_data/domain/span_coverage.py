"""Slot states of a span from stored facts only (contract api-v1 section 4, clock-free subset)."""

from collections.abc import Iterator
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Literal

StoredState = Literal["present", "conflicted", "unavailable", "missing", "beyond_stored"]


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


def span_slot_states(
    *,
    start: int,
    end: int,
    timeframe_ms: int,
    candles: AbstractSet[int],
    conflicts: AbstractSet[int],
    unavailable: AbstractSet[int],
    newest_open_time_ms: int,
) -> Iterator[tuple[int, StoredState]]:
    """Yield ``(open_time_ms, state)`` for every slot of ``[start, end)`` in ascending order.

    Each slot gets exactly one state, the first that matches: ``conflicted`` (a conflict record, or both a
    candle and an unavailable record), ``present``, ``unavailable``, ``missing`` (at or before the newest
    stored candle of the series), ``beyond_stored``.
    Raises ``ValueError`` before the first item for an empty or misaligned span and for any element outside
    it or misaligned, because a caller bug must not become a wrong count.
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

    def states() -> Iterator[tuple[int, StoredState]]:
        for open_time in range(start, end, timeframe_ms):
            has_candle = open_time in candles
            has_unavailable = open_time in unavailable
            if open_time in conflicts or (has_candle and has_unavailable):
                yield open_time, "conflicted"
            elif has_candle:
                yield open_time, "present"
            elif has_unavailable:
                yield open_time, "unavailable"
            elif open_time <= newest_open_time_ms:
                yield open_time, "missing"
            else:
                yield open_time, "beyond_stored"

    return states()


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
    """Count the states of the slots of ``[start, end)`` (see ``span_slot_states`` for precedence and errors)."""
    counts: dict[str, int] = {"present": 0, "conflicted": 0, "unavailable": 0, "missing": 0, "beyond_stored": 0}
    conflicted_open_times: set[int] = set()
    for open_time, state in span_slot_states(
        start=start,
        end=end,
        timeframe_ms=timeframe_ms,
        candles=candles,
        conflicts=conflicts,
        unavailable=unavailable,
        newest_open_time_ms=newest_open_time_ms,
    ):
        counts[state] += 1
        if state == "conflicted":
            conflicted_open_times.add(open_time)

    return SpanCoverage(
        slots=(end - start) // timeframe_ms,
        present=counts["present"],
        conflicted=counts["conflicted"],
        unavailable=counts["unavailable"],
        missing=counts["missing"],
        beyond_stored=counts["beyond_stored"],
        conflicted_open_times=frozenset(conflicted_open_times),
    )
