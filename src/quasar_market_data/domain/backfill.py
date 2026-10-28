"""Pure backfill rules (S16): bounded targets, page requests, page judgement and recovery attribution.

Nothing here reads a clock, the exchange or the database. A page is judged against the request that produced it
*before* anything is selected or written (D4): a violation rejects the page whole. What the exchange served
(``judge_target``), what the writer reported and what was recovered (``attribute_recovery``) are three different
statements and are never merged; only a read-back of the stored open times can say a slot is now present.
"""

from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import StrEnum

from quasar_market_data.domain.timestamps import ensure_timestamp_ms


def _int(value: int, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    return value


def _aligned(value: int, timeframe_ms: int, name: str) -> int:
    ensure_timestamp_ms(value, name)
    if value % timeframe_ms:
        raise ValueError(f"{name} is not aligned to the timeframe: {value}")
    return value


def _tf(timeframe_ms: int) -> int:
    if _int(timeframe_ms, "timeframe_ms") <= 0:
        raise ValueError(f"timeframe_ms must be > 0, got {timeframe_ms}")
    return timeframe_ms


@dataclass(frozen=True)
class BackfillTarget:
    """A half-open range of open times ``[first_ms, end_ms)``."""

    first_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        ensure_timestamp_ms(self.first_ms, "first_ms")
        ensure_timestamp_ms(self.end_ms, "end_ms")
        if self.first_ms >= self.end_ms:
            raise ValueError(f"target must have first_ms < end_ms, got {self.first_ms}, {self.end_ms}")


def last_open_ms(target: BackfillTarget, timeframe_ms: int) -> int:
    return target.end_ms - _tf(timeframe_ms)


def slot_count(target: BackfillTarget, timeframe_ms: int) -> int:
    span = target.end_ms - target.first_ms
    if span % _tf(timeframe_ms):
        raise ValueError("target is not aligned to the timeframe")
    return span // timeframe_ms


def plan_targets(
    *,
    horizon_start_ms: int,
    forming_open_ms: int,
    oldest_stored_ms: int | None,
    interior_runs: Sequence[tuple[int, int]],
    timeframe_ms: int,
) -> tuple[BackfillTarget, ...]:
    """Targets newest first: interior runs clipped to the horizon, then the head before the oldest stored row."""
    tf = _tf(timeframe_ms)
    _aligned(horizon_start_ms, tf, "horizon_start_ms")
    _aligned(forming_open_ms, tf, "forming_open_ms")
    if oldest_stored_ms is not None:
        _aligned(oldest_stored_ms, tf, "oldest_stored_ms")
    if horizon_start_ms >= forming_open_ms:
        return ()
    targets: list[BackfillTarget] = []
    for start, end in interior_runs:
        _aligned(start, tf, "run start")
        _aligned(end, tf, "run end")
        first, stop = max(start, horizon_start_ms), min(end, forming_open_ms)
        if first < stop:
            targets.append(BackfillTarget(first, stop))
    head_end = forming_open_ms if oldest_stored_ms is None else min(oldest_stored_ms, forming_open_ms)
    if horizon_start_ms < head_end:
        targets.append(BackfillTarget(horizon_start_ms, head_end))
    return tuple(targets)


@dataclass(frozen=True)
class PagePlan:
    """One klines request: ``end_ms`` is an inclusive open time."""

    start_ms: int
    end_ms: int
    limit: int


def plan_page(target: BackfillTarget, start_ms: int, timeframe_ms: int, max_limit: int) -> PagePlan:
    tf = _tf(timeframe_ms)
    _int(max_limit, "max_limit")
    if max_limit < 1:
        raise ValueError(f"max_limit must be >= 1, got {max_limit}")
    _aligned(start_ms, tf, "start_ms")
    last = last_open_ms(target, tf)
    if not target.first_ms <= start_ms <= last:
        raise ValueError(f"start_ms {start_ms} is outside the target")
    return PagePlan(start_ms, last, min(max_limit, (target.end_ms - start_ms) // tf))


class PageViolation(ValueError):
    """The page does not match its request; it must be rejected whole."""


class PageKind(StrEnum):
    EMPTY = "empty"
    FULL = "full"
    SHORT = "short"


@dataclass(frozen=True)
class PageOutcome:
    kind: PageKind
    rows: int
    next_start_ms: int | None  # None when the target ends


def judge_page(plan: PagePlan, open_times_ms: Sequence[int], timeframe_ms: int) -> PageOutcome:
    tf = _tf(timeframe_ms)
    if len(open_times_ms) > plan.limit:
        raise PageViolation(f"page has {len(open_times_ms)} rows, more than the limit {plan.limit}")
    previous: int | None = None
    for t in open_times_ms:
        _int(t, "open time")
        if t < plan.start_ms:
            raise PageViolation(f"row {t} is before the requested start {plan.start_ms}")
        if t > plan.end_ms:
            raise PageViolation(f"row {t} is after the requested end {plan.end_ms}")
        if t % tf:
            raise PageViolation(f"row {t} is not aligned to the timeframe")
        if previous is not None and t <= previous:
            raise PageViolation(f"rows are not strictly increasing at {t}")
        previous = t
    if previous is None:
        return PageOutcome(PageKind.EMPTY, 0, None)
    if len(open_times_ms) < plan.limit:
        return PageOutcome(PageKind.SHORT, len(open_times_ms), None)
    nxt = previous + tf
    return PageOutcome(PageKind.FULL, len(open_times_ms), nxt if nxt <= plan.end_ms else None)


class TargetResult(StrEnum):
    SERVED_ALL = "served_all"
    SERVED_SOME = "served_some"
    SERVED_NONE = "served_none"


def judge_target(target: BackfillTarget, timeframe_ms: int, rows_total: int) -> TargetResult:
    """What the exchange answered for a target. Says nothing about storage or recovery."""
    slots = slot_count(target, timeframe_ms)
    if _int(rows_total, "rows_total") < 0 or rows_total > slots:
        raise ValueError(f"rows_total {rows_total} is outside 0..{slots}")
    if rows_total == slots:
        return TargetResult.SERVED_ALL
    return TargetResult.SERVED_NONE if rows_total == 0 else TargetResult.SERVED_SOME


def max_pages(target: BackfillTarget, timeframe_ms: int, max_limit: int) -> int:
    slots = slot_count(target, timeframe_ms)
    return -(-slots // max_limit) + 1


class Attribution(StrEnum):
    OK = "ok"
    FOREIGN_WRITES = "foreign_writes"
    REPORT_MISMATCH = "report_mismatch"
    WRITE_UNKNOWN = "write_unknown"
    ANOMALY = "anomaly"
    READBACK_FAILED = "readback_failed"


@dataclass(frozen=True)
class RecoveryResult:
    new_in_targets: int | None
    recovered: int | None
    missing_in_targets: int | None
    attribution: Attribution


def attribute_recovery(
    targets: Sequence[BackfillTarget],
    before: Sequence[AbstractSet[int]] | None,
    after: Sequence[AbstractSet[int]] | None,
    sent_open_ms: AbstractSet[int],
    reported_inserted: int | None,
    timeframe_ms: int,
) -> RecoveryResult:
    """Recovery from two read-backs of the same targets. The write report may only downgrade, never add."""
    if before is None or after is None:
        return RecoveryResult(None, None, None, Attribution.READBACK_FAILED)
    if not len(targets) == len(before) == len(after):
        raise ValueError("before and after must hold one set per target")
    new: set[int] = set()
    removed = False
    new_count = missing = 0
    for target, b, a in zip(targets, before, after, strict=True):
        for t in (*b, *a):
            if not target.first_ms <= t < target.end_ms:
                raise ValueError(f"open time {t} is outside its target range")
        removed = removed or not b <= a
        fresh = a - b
        new |= fresh
        new_count += len(fresh)
        missing += slot_count(target, timeframe_ms) - len(a)
    if removed:
        return RecoveryResult(new_count, None, missing, Attribution.ANOMALY)
    if reported_inserted is None:  # first: a write that committed and then reported nothing leaves unsent-looking slots
        return RecoveryResult(new_count, None, missing, Attribution.WRITE_UNKNOWN)
    if not new <= sent_open_ms:
        return RecoveryResult(new_count, None, missing, Attribution.FOREIGN_WRITES)
    if reported_inserted != len(new):
        return RecoveryResult(new_count, None, missing, Attribution.REPORT_MISMATCH)
    return RecoveryResult(new_count, len(new), missing, Attribution.OK)
