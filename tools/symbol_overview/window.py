"""The 30-day window and the heatmap strip (pure; spec 5.3 and 5.7)."""

from collections.abc import Sequence

from tools.symbol_overview.model import (
    BUCKET_SLOTS,
    BUCKETS,
    MAX_TS_MS,
    MIN_TS_MS,
    STATE_CHARS,
    STRIP_RANK,
    TF_MS,
    WINDOW_SLOTS,
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
