"""Segments, warm-up and points of one indicator over a window (design S18 sections 5.2 and 5.3; pure, no I/O).

The indicator function itself is injected as ``compute``; this module never inspects its values, so finiteness
checks belong to the adapter that computes them.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from quasar_market_data.domain.timed_coverage import SlotState
from quasar_market_data.domain.timestamps import MIN_TIMESTAMP_MS

type PointReason = Literal["conflicted", "unavailable", "missing", "not_yet_expected", "due", "stale", "warmup"]

# One sequence per output, each as long as the input closes.
type Compute = Callable[[Sequence[float]], Sequence[Sequence[float]]]

_REASONS: Final[Mapping[SlotState, PointReason]] = {
    SlotState.CONFLICTED: "conflicted",
    SlotState.UNAVAILABLE: "unavailable",
    SlotState.MISSING: "missing",
    SlotState.NOT_YET_EXPECTED: "not_yet_expected",
    SlotState.DUE: "due",
    SlotState.STALE: "stale",
}


@dataclass(frozen=True)
class IndicatorPoint:
    """Either ``values`` (one per output) and ``reason is None``, or ``values is None`` and a ``reason``."""

    open_time_ms: int
    values: tuple[float, ...] | None
    reason: PointReason | None


@dataclass(frozen=True)
class IndicatorSeries:
    """The points of ``[start, end)``; ``anchor_open_time_ms`` is the first slot of the segment holding the first
    point with a value, or ``None`` when no point has one."""

    anchor_open_time_ms: int | None
    points: tuple[IndicatorPoint, ...]


def read_start_ms(start: int, warmup_slots: int, timeframe_ms: int) -> int:
    """The first open time of the read range ``[start - warmup_slots * timeframe_ms, end)``, clamped at ``T_min``."""
    return max(start - warmup_slots * timeframe_ms, MIN_TIMESTAMP_MS)


def _ensure_int(value: int, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value).__name__}")


def _segments(states: Sequence[tuple[int, SlotState]]) -> list[tuple[int, int]]:
    """Index ranges ``[first, last)`` into ``states`` of the maximal runs of ``present`` slots."""
    segments: list[tuple[int, int]] = []
    first: int | None = None
    for index, (_, state) in enumerate(states):
        if state is SlotState.PRESENT:
            if first is None:
                first = index
        elif first is not None:
            segments.append((first, index))
            first = None
    if first is not None:
        segments.append((first, len(states)))
    return segments


def evaluate(
    *,
    start: int,
    end: int,
    timeframe_ms: int,
    warmup_slots: int,
    states: Sequence[tuple[int, SlotState]],
    closes: Mapping[int, float],
    compute: Compute,
    output_count: int,
) -> IndicatorSeries:
    """Evaluate one indicator over the points of ``[start, end)``.

    ``states`` must hold one ``(open_time_ms, state)`` per slot of ``[read_start_ms(...), end)`` in ascending
    order and ``closes`` exactly the closes of its ``present`` slots. ``compute`` is called once per segment (a
    maximal run of ``present`` slots of the read range) with that segment's closes in order. A present slot with
    index ``k`` in its segment has values only if ``k >= warmup_slots``, otherwise reason ``warmup``; any other
    slot has the reason named after its state. Raises ``TypeError`` or ``ValueError`` for inconsistent inputs or a
    ``compute`` result with the wrong number of outputs or lengths.
    """
    for name, value in (
        ("start", start),
        ("end", end),
        ("timeframe_ms", timeframe_ms),
        ("warmup_slots", warmup_slots),
        ("output_count", output_count),
    ):
        _ensure_int(value, name)
    if timeframe_ms <= 0:
        raise ValueError(f"timeframe_ms must be positive, got {timeframe_ms}")
    if warmup_slots < 0:
        raise ValueError(f"warmup_slots must be >= 0, got {warmup_slots}")
    if output_count < 1:
        raise ValueError(f"output_count must be >= 1, got {output_count}")
    if start < MIN_TIMESTAMP_MS:
        raise ValueError(f"start must be at least {MIN_TIMESTAMP_MS}, got {start}")
    if start % timeframe_ms != 0 or end % timeframe_ms != 0:
        raise ValueError("start and end must be aligned to the timeframe")
    if end <= start:
        raise ValueError(f"the window must not be empty, got [{start}, {end})")

    read_start = read_start_ms(start, warmup_slots, timeframe_ms)
    expected_open_times = range(read_start, end, timeframe_ms)
    if len(states) != len(expected_open_times):
        raise ValueError(
            f"states must hold {len(expected_open_times)} slots of [{read_start}, {end}), got {len(states)}"
        )
    present: set[int] = set()
    for (open_time, state), expected in zip(states, expected_open_times, strict=True):
        if open_time != expected:
            raise ValueError(
                f"states must cover [{read_start}, {end}) in order; got {open_time} where {expected} is due"
            )
        if not isinstance(state, SlotState):
            raise TypeError(f"state must be SlotState, got {type(state).__name__}")
        if state is SlotState.PRESENT:
            present.add(open_time)
    if set(closes) != present:
        raise ValueError("closes must hold exactly the present slots of the states")

    # valued[i] is (values, anchor) of states[i] when it is a present slot past the warm-up of its segment.
    valued: dict[int, tuple[tuple[float, ...], int]] = {}
    for first, last in _segments(states):
        segment_closes = [closes[open_time] for open_time, _ in states[first:last]]
        outputs = compute(segment_closes)
        if len(outputs) != output_count:
            raise ValueError(f"compute returned {len(outputs)} outputs, expected {output_count}")
        for output in outputs:
            if len(output) != len(segment_closes):
                raise ValueError(f"compute returned an output of length {len(output)}, expected {len(segment_closes)}")
        for k in range(warmup_slots, last - first):
            valued[first + k] = (tuple(output[k] for output in outputs), states[first][0])

    points: list[IndicatorPoint] = []
    anchor: int | None = None
    for index, (open_time, state) in enumerate(states):
        if open_time < start:
            continue
        if state is not SlotState.PRESENT:
            points.append(IndicatorPoint(open_time, None, _REASONS[state]))
        elif index in valued:
            point_values, segment_anchor = valued[index]
            if anchor is None:
                anchor = segment_anchor
            points.append(IndicatorPoint(open_time, point_values, None))
        else:
            points.append(IndicatorPoint(open_time, None, "warmup"))
    return IndicatorSeries(anchor_open_time_ms=anchor, points=tuple(points))
