"""Decoding and validation of the three API answers (pure; spec 5.6).

Every check is about internal consistency. Any failure returns ``Invalid`` and the caller turns it into ``unknown``
for the symbol (coverage) or into a page-level error (health, instruments); nothing here ever yields an "ok".
"""

import json
from typing import Any, Final, TypeGuard

from tools.symbol_overview.model import (
    CALL_TIMEOUT_MS,
    INVALID_CODES,
    MAX_BODY_BYTES,
    MAX_TS_MS,
    MIN_TS_MS,
    REFRESH_DEADLINE_MS,
    TF_MS,
    Coverage,
    Health,
    InstrumentRow,
    Invalid,
    Rule,
    Run,
    Window,
)

_RUN_STATES: Final = ("conflicted", "unavailable", "missing", "not_yet_expected", "due", "stale")
_CLOCK_STATES: Final = ("not_yet_expected", "due", "stale")
_COUNT_KEYS: Final = {
    "conflicted": "conflicted_slots",
    "unavailable": "unavailable_slots",
    "missing": "missing_slots",
    "not_yet_expected": "not_yet_expected_slots",
    "due": "due_slots",
    "stale": "stale_slots",
}


def _bad(code: str, detail: str = "") -> Invalid:
    if code not in INVALID_CODES:
        raise ValueError(f"unknown invalid code {code!r}")
    return Invalid(code, detail)


def _is_int(value: object) -> TypeGuard[int]:
    return type(value) is int


def _non_negative(value: object) -> TypeGuard[int]:
    return type(value) is int and value >= 0


def _ts(value: object) -> TypeGuard[int]:
    return type(value) is int and MIN_TS_MS <= value <= MAX_TS_MS


def _aligned_ts(value: object) -> TypeGuard[int]:
    return type(value) is int and MIN_TS_MS <= value <= MAX_TS_MS and value % TF_MS == 0


def _parse(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def _decode(status: int, content_type: str | None, body: bytes) -> dict[str, Any] | Invalid:
    """The JSON object of a 200 answer, or why there is none. A non-200 with the API's error body keeps its code."""
    if len(body) > MAX_BODY_BYTES:
        return _bad("too_large")
    if status != 200:
        doc = _parse(body)
        error = doc.get("error") if isinstance(doc, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str):
            return _bad("api_error", code)
        return _bad("bad_status", str(status))
    if content_type is None or not content_type.lower().startswith("application/json"):
        return _bad("bad_content_type")
    doc = _parse(body)
    if not isinstance(doc, dict):
        return _bad("bad_json")
    return doc


def _rule(value: object) -> Rule | None:
    if not isinstance(value, dict):
        return None
    delay = value.get("publication_delay_ms")
    lag = value.get("lag_threshold_ms")
    if not (_non_negative(delay) and _non_negative(lag)):
        return None
    return Rule(delay, lag)


def health_result(status: int, content_type: str | None, body: bytes) -> Health | Invalid:
    doc = _decode(status, content_type, body)
    if isinstance(doc, Invalid):
        return doc
    rule = _rule(doc.get("rule"))
    as_of = doc.get("as_of_ms")
    if rule is None or not _ts(as_of):
        return _bad("schema")
    return Health(as_of, rule)


def instruments_result(status: int, content_type: str | None, body: bytes) -> tuple[InstrumentRow, ...] | Invalid:
    doc = _decode(status, content_type, body)
    if isinstance(doc, Invalid):
        return doc
    items = doc.get("instruments")
    if not isinstance(items, list):
        return _bad("schema")
    rows: list[InstrumentRow] = []
    for item in items:
        if not isinstance(item, dict) or "newest_open_time_ms" not in item:
            return _bad("schema")
        venue, symbol, timeframe = item.get("venue"), item.get("symbol"), item.get("timeframe")
        newest = item["newest_open_time_ms"]
        if not (isinstance(venue, str) and isinstance(symbol, str) and isinstance(timeframe, str)):
            return _bad("schema")
        if newest is not None and not _aligned_ts(newest):
            return _bad("schema")
        rows.append(InstrumentRow(venue, symbol, timeframe, newest))
    if len({(r.venue, r.symbol, r.timeframe) for r in rows}) != len(rows):
        return _bad("schema")
    if not rows:
        return _bad("empty_universe")
    return tuple(rows)


def _runs(raw: list[Any]) -> list[tuple[Run, int]] | None:
    parsed: list[tuple[Run, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        state, start, end, slots = item.get("state"), item.get("start"), item.get("end"), item.get("slots")
        if not (isinstance(state, str) and _is_int(start) and _is_int(end) and _is_int(slots)):
            return None
        parsed.append((Run(state, start, end), slots))
    return parsed


def coverage_result(
    status: int,
    content_type: str | None,
    body: bytes,
    *,
    instrument: InstrumentRow,
    window: Window,
    health: Health,
) -> Coverage | Invalid:
    """Validate one ``/v1/coverage`` answer for a stored ``instrument`` asked for ``window`` (spec 5.6)."""
    if instrument.newest_open_time_ms is None:
        raise ValueError("coverage_result is for a stored instrument; a null newest is no_data and is never asked")
    doc = _decode(status, content_type, body)
    if isinstance(doc, Invalid):
        if doc == Invalid("api_error", "unknown_series"):
            return _bad("inconsistent_with_instruments")  # candles are never deleted (C§5.2)
        return doc

    # Types and ranges
    series, block, raw_runs = doc.get("series"), doc.get("coverage"), doc.get("runs")
    if not (isinstance(series, dict) and isinstance(block, dict) and isinstance(raw_runs, list)):
        return _bad("schema")
    oldest, newest = series.get("oldest_open_time_ms"), series.get("newest_open_time_ms")
    as_of, rule, ahead = doc.get("as_of_ms"), _rule(doc.get("rule")), doc.get("stored_ahead_of_as_of")
    slots, fully = block.get("slots"), block.get("fully_present")
    present = block.get("present_slots")
    counts = {state: block.get(key) for state, key in _COUNT_KEYS.items()}
    runs = _runs(raw_runs)
    strings = (doc.get("venue"), doc.get("symbol"), doc.get("timeframe"))
    if (
        runs is None
        or rule is None
        or not all(isinstance(s, str) for s in strings)
        or not (_is_int(doc.get("start")) and _is_int(doc.get("end")))
        or not (_aligned_ts(oldest) and _aligned_ts(newest) and _ts(as_of))
        or type(ahead) is not bool
        or type(fully) is not bool
        or not all(_non_negative(v) for v in (slots, present, *counts.values()))
    ):
        return _bad("schema")
    assert isinstance(oldest, int) and isinstance(newest, int) and isinstance(as_of, int)
    assert isinstance(slots, int) and isinstance(present, int)
    if oldest > newest:
        return _bad("schema")

    # Echo of the request
    asked = (instrument.venue, instrument.symbol, instrument.timeframe, window.start, window.end)
    if (*strings, doc["start"], doc["end"]) != asked:
        return _bad("echo")

    # Counts
    counted = {state: int(v) for state, v in counts.items() if isinstance(v, int)}
    if (
        slots != (window.end - window.start) // TF_MS
        or present + sum(counted.values()) != slots
        or fully != (present == slots)
    ):
        return _bad("counts")

    # Runs
    previous_end = window.start
    per_state = dict.fromkeys(_RUN_STATES, 0)
    for run, declared in runs:
        if (
            run.state not in per_state
            or run.start % TF_MS
            or run.end % TF_MS
            or not window.start <= run.start < run.end <= window.end
            or declared != (run.end - run.start) // TF_MS
            or run.start < previous_end
        ):
            return _bad("runs")
        per_state[run.state] += declared
        previous_end = run.end
    if any(per_state[state] != counted[state] for state in _RUN_STATES):
        return _bad("runs")

    # Runs against the series block (C§4: `missing` is a slot at or before the newest candle, the clock states
    # are slots after it, and the oldest candle's own slot is never `missing`)
    for run, _ in runs:
        if run.state in _CLOCK_STATES and run.start <= newest:
            return _bad("runs")
        if run.state == "missing":
            if run.start < oldest < run.end:
                return _bad("inconsistent_runs")
            if run.end > newest + TF_MS:
                return _bad("runs")
    before_oldest = min(oldest, window.end)
    if before_oldest > window.start:  # no candle before the oldest one: every slot there is in some run
        covered = sum(max(0, min(r.end, before_oldest) - max(r.start, window.start)) for r, _ in runs)
        if covered != before_oldest - window.start:
            return _bad("runs")
    after_newest = max(newest + TF_MS, window.start)
    if after_newest < window.end:  # a slot after the newest candle is never present: every one is in some run
        covered = sum(max(0, min(r.end, window.end) - max(r.start, after_newest)) for r, _ in runs)
        if covered != window.end - after_newest:
            return _bad("runs")

    # Against the other answers
    if rule != health.rule:
        return _bad("rule_mismatch")
    if not health.as_of_ms <= as_of <= health.as_of_ms + REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS:
        return _bad("clock_inconsistent")
    if newest >= window.end and not ahead:  # then newest + tf + delay > as_of by integer identity (C§6.1.3, V9)
        return _bad("clock_inconsistent")
    if newest < instrument.newest_open_time_ms:
        return _bad("regressed")  # the newest candle only moves later (C§5.2)

    return Coverage(
        as_of_ms=as_of,
        rule=rule,
        oldest_open_time_ms=oldest,
        newest_open_time_ms=newest,
        stored_ahead_of_as_of=ahead,
        present_slots=present,
        conflicted_slots=counted["conflicted"],
        unavailable_slots=counted["unavailable"],
        missing_slots=counted["missing"],
        not_yet_expected_slots=counted["not_yet_expected"],
        due_slots=counted["due"],
        stale_slots=counted["stale"],
        runs=tuple(run for run, _ in runs),
    )
