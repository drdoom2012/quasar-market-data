"""The 30-day window and the heatmap strip (pure; spec 5.3 and 5.7)."""

from collections.abc import Sequence

from tools.symbol_overview.model import (
    BUCKET_SLOTS,
    BUCKETS,
    DAY_MS,
    MAX_FUNDING_WINDOW_MS,
    MAX_TS_MS,
    MIN_TS_MS,
    OI_STATE_CHARS,
    OI_STRIP_RANK,
    STATE_CHARS,
    STRIP_RANK,
    TF_MS,
    WINDOW_SLOTS,
    FundingRule,
    FundingWindow,
    Run,
    Window,
)

_BUCKET_MS = BUCKET_SLOTS * TF_MS


def window_for(as_of_ms: int) -> Window:
    """``[S, E)`` where ``E`` ends the slot containing ``as_of_ms`` and ``S = E - 30 days``."""
    if type(as_of_ms) is not int:
        raise TypeError(f"as_of_ms must be int, got {type(as_of_ms).__name__}")
    if not MIN_TS_MS <= as_of_ms <= MAX_TS_MS:
        raise ValueError(f"as_of_ms {as_of_ms} is outside the valid time range")
    end = (as_of_ms // TF_MS + 1) * TF_MS
    start = end - WINDOW_SLOTS * TF_MS
    if start < MIN_TS_MS:
        raise ValueError(f"the window would start before the valid time range: {start}")
    return Window(start, end)


def build_strip(runs: Sequence[Run], oldest_open_time_ms: int, window: Window) -> str:
    """One character per hour of the window; the worst state in the bucket wins.

    A bucket that ends at or before the oldest candle starts as ``_`` (history not there yet), any other as ``.``.
    A ``missing`` run is clipped to start at the oldest candle: the part before it is pre-history, not a hole.
    Raises ``ValueError`` for a run that is malformed, misaligned, outside the window or not a non-present state.
    """
    buckets = ["_" if window.start + (k + 1) * _BUCKET_MS <= oldest_open_time_ms else "." for k in range(BUCKETS)]
    for run in runs:
        if run.state not in STATE_CHARS:
            raise ValueError(f"not a strip state: {run.state!r}")
        if run.start % TF_MS or run.end % TF_MS or not window.start <= run.start < run.end <= window.end:
            raise ValueError(f"run [{run.start}, {run.end}) is malformed or outside the window")
        start = max(run.start, oldest_open_time_ms) if run.state == "missing" else run.start
        if start >= run.end:
            continue  # entirely pre-history
        char = STATE_CHARS[run.state]
        first = (start - window.start) // _BUCKET_MS
        last = (run.end - 1 - window.start) // _BUCKET_MS
        for k in range(first, last + 1):
            if STRIP_RANK.index(char) > STRIP_RANK.index(buckets[k]):
                buckets[k] = char
    return "".join(buckets)


def open_interest_window(window: Window) -> Window:
    """The window of the open interest request: ``[S + tf, E + tf)``.

    The slot ``T`` is the END of a period (contract 6.5.1), so the slots ``S + tf ... E`` describe exactly the periods
    ``[S, E)`` the candle window covers, and bucket ``k`` is the same hour in both strips.
    """
    return Window(window.start + TF_MS, window.end + TF_MS)


def build_open_interest_strip(runs: Sequence[Run], oldest_time_ms: int, oi_window: Window) -> str:
    """The open interest strip: as ``build_strip``, with ``r`` (no longer recoverable) and the order of design 8.6.

    A bucket that ends at or before the oldest stored row starts as ``_`` ("before the first stored row"); a
    ``missing`` or ``no_longer_recoverable`` run is clipped to start at that row, so ``r`` marks only a hole inside
    the stored history and slots after the newest row. Raises ``ValueError`` for a run that is malformed, misaligned,
    outside the window or not a non-present state.
    """
    buckets = ["_" if oi_window.start + (k + 1) * _BUCKET_MS <= oldest_time_ms else "." for k in range(BUCKETS)]
    for run in runs:
        if run.state not in OI_STATE_CHARS:
            raise ValueError(f"not an open interest strip state: {run.state!r}")
        if run.start % TF_MS or run.end % TF_MS or not oi_window.start <= run.start < run.end <= oi_window.end:
            raise ValueError(f"run [{run.start}, {run.end}) is malformed or outside the window")
        clipped = run.state in ("missing", "no_longer_recoverable")
        start = max(run.start, oldest_time_ms) if clipped else run.start
        if start >= run.end:
            continue  # entirely before the first stored row
        char = OI_STATE_CHARS[run.state]
        first = (start - oi_window.start) // _BUCKET_MS
        last = (run.end - 1 - oi_window.start) // _BUCKET_MS
        for k in range(first, last + 1):
            if OI_STRIP_RANK.index(char) > OI_STRIP_RANK.index(buckets[k]):
                buckets[k] = char
    return "".join(buckets)


def funding_window(window: Window, rule: FundingRule, as_of_ms: int) -> FundingWindow:
    """The funding window ``[start, end]`` (inclusive), design 4.1.

    ``end = as_of - behind_after_ms + 1``: by the contract's rule a series is not behind iff ``as_of - T <
    behind_after_ms``, i.e. iff ``T >= end``, so ``end <= verified_through_ms`` holds exactly for the series that are
    not behind. ``start`` is the start of the 30-day window, but not before ``backfill_days`` back: the Collector
    never verifies deeper, and a window that starts before that could never be ``verified``.
    Raises ``ValueError`` when the window cannot be built.
    """
    if rule.behind_after_ms < 0 or rule.backfill_days < 1:
        raise ValueError("behind_after_ms must be >= 0 and backfill_days >= 1")
    start = max(window.start, as_of_ms - rule.backfill_days * DAY_MS)
    end = as_of_ms - rule.behind_after_ms + 1
    if not (MIN_TS_MS <= start <= MAX_TS_MS and MIN_TS_MS <= end <= MAX_TS_MS):
        raise ValueError(f"the funding window [{start}, {end}] is outside the valid time range")
    if end < start:
        raise ValueError(f"the funding window [{start}, {end}] is empty")
    if end - start > MAX_FUNDING_WINDOW_MS:
        raise ValueError("the funding window is longer than 365 days")
    return FundingWindow(start, end)
