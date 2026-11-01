"""Shared constants and builders for the symbol overview tests."""

from collections.abc import Mapping
from typing import Any

from tools.symbol_overview.model import TF_MS, TIMEFRAME, VENUE, InstrumentRow, Rule, Window

T0 = 1_792_677_000_000  # slot-aligned: T0 % TF_MS == 0
RULE = Rule(15_000, 60_000)
SYMBOL = "BTC/USDT:USDT"
STATES = ("present", "conflicted", "unavailable", "missing", "not_yet_expected", "due", "stale")


def instrument(symbol: str = SYMBOL, newest: int | None = T0 - 2 * TF_MS) -> InstrumentRow:
    return InstrumentRow(VENUE, symbol, TIMEFRAME, newest)


def rule_doc(rule: Rule = RULE) -> dict[str, int]:
    return {"publication_delay_ms": rule.publication_delay_ms, "lag_threshold_ms": rule.lag_threshold_ms}


def health_doc(as_of_ms: int = T0, rule: Rule = RULE) -> dict[str, Any]:
    return {"as_of_ms": as_of_ms, "rule": rule_doc(rule), "scope": "stored_series", "status": "current"}


def coverage_doc(
    window: Window,
    *,
    oldest: int,
    newest: int,
    symbol: str = SYMBOL,
    slot_states: Mapping[int, str] | None = None,
    as_of_ms: int = T0 + 1000,
    rule: Rule = RULE,
    stored_ahead: bool = False,
) -> dict[str, Any]:
    """A self-consistent ``/v1/coverage`` body.

    Slots in ``[oldest, newest]`` are ``present``, slots before ``oldest`` are ``missing``, slots after ``newest``
    are ``not_yet_expected``; ``slot_states`` overrides single slots (even into a contradiction the API would
    never produce, so a test can build exactly one inconsistency). Counts and maximal runs are derived.
    """
    overrides = slot_states or {}
    counts = dict.fromkeys(STATES, 0)
    runs: list[dict[str, Any]] = []
    for t in range(window.start, window.end, TF_MS):
        if t in overrides:
            state = overrides[t]
        elif t < oldest:
            state = "missing"
        elif t <= newest:
            state = "present"
        else:
            state = "not_yet_expected"
        counts[state] += 1
        if state == "present":
            continue
        if runs and runs[-1]["state"] == state and runs[-1]["end"] == t:
            runs[-1]["end"] = t + TF_MS
            runs[-1]["slots"] += 1
        else:
            runs.append({"state": state, "start": t, "end": t + TF_MS, "slots": 1})
    slots = (window.end - window.start) // TF_MS
    return {
        "venue": VENUE,
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "start": window.start,
        "end": window.end,
        "as_of_ms": as_of_ms,
        "rule": rule_doc(rule),
        "series": {"oldest_open_time_ms": oldest, "newest_open_time_ms": newest},
        "stored_ahead_of_as_of": stored_ahead,
        "coverage": {
            "slots": slots,
            "present_slots": counts["present"],
            "conflicted_slots": counts["conflicted"],
            "unavailable_slots": counts["unavailable"],
            "missing_slots": counts["missing"],
            "not_yet_expected_slots": counts["not_yet_expected"],
            "due_slots": counts["due"],
            "stale_slots": counts["stale"],
            "fully_present": counts["present"] == slots,
        },
        "runs": runs,
    }
