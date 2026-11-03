"""Judging one symbol and building the overview object (pure; spec 5.4, 5.8).

The tool defines no time threshold of its own: ``due`` and ``stale`` come from the counts of the coverage answer,
which the API computed with its own clock and rule.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from tools.symbol_overview.model import (
    BUCKET_SLOTS,
    TF_MS,
    Coverage,
    Health,
    InstrumentRow,
    Invalid,
    OiCoverage,
    OiReport,
    Reason,
    SymbolReport,
    Window,
)
from tools.symbol_overview.window import build_open_interest_strip, build_strip, open_interest_window

STATE_ORDER: Final = ("no_data", "unknown", "stale", "conflict", "holes", "behind", "ok")
_REASON_STATE: Final = {
    "no_data": "no_data",
    "invalid": "unknown",
    "stored_ahead": "unknown",
    "stale": "stale",
    "conflict": "conflict",
    "missing": "holes",
    "unavailable": "holes",
    "due": "behind",
}


def _report(
    instrument: InstrumentRow,
    state: str,
    reasons: tuple[Reason, ...],
    coverage: Coverage | None = None,
    strip: str | None = None,
) -> SymbolReport:
    return SymbolReport(
        venue=instrument.venue,
        symbol=instrument.symbol,
        timeframe=instrument.timeframe,
        state=state,
        reasons=reasons,
        oldest_open_time_ms=coverage.oldest_open_time_ms if coverage else None,
        newest_open_time_ms=coverage.newest_open_time_ms if coverage else None,
        as_of_ms=coverage.as_of_ms if coverage else None,
        strip=strip,
    )


def judge_symbol(instrument: InstrumentRow, outcome: Coverage | Invalid | None, window: Window) -> SymbolReport:
    """The state and all reasons of one symbol.

    ``outcome`` is ``None`` exactly when nothing is stored for the instrument (``no_data``); any other pairing is a
    caller bug and raises ``ValueError``. A ``Coverage`` must already have passed ``coverage_result``.
    """
    stored = instrument.newest_open_time_ms is not None
    if outcome is None:
        if stored:
            raise ValueError("a stored instrument needs an outcome")
        return _report(instrument, "no_data", (Reason("no_data", 0, 0),))
    if not stored:
        raise ValueError("an instrument with nothing stored has no outcome")
    if isinstance(outcome, Invalid):
        detail = f"{outcome.code}:{outcome.detail}" if outcome.detail else outcome.code
        return _report(instrument, "unknown", (Reason("invalid", 0, 0, detail),))

    oldest = outcome.oldest_open_time_ms

    def runs_of(state: str) -> int:
        return sum(1 for run in outcome.runs if run.state == state)

    # `missing` slots before the oldest candle are history that is not there yet, not holes (spec 5.4 step A).
    pre_history = sum((r.end - r.start) // TF_MS for r in outcome.runs if r.state == "missing" and r.end <= oldest)
    real_missing = outcome.missing_slots - pre_history
    hole_runs = sum(1 for r in outcome.runs if r.state == "missing" and r.end > oldest)

    reasons: list[Reason] = []
    if outcome.stored_ahead_of_as_of:
        reasons.append(Reason("stored_ahead", 0, 0))
    if outcome.stale_slots:
        reasons.append(Reason("stale", outcome.stale_slots, runs_of("stale")))
    if outcome.conflicted_slots:
        reasons.append(Reason("conflict", outcome.conflicted_slots, runs_of("conflicted")))
    if real_missing:
        reasons.append(Reason("missing", real_missing, hole_runs))
    if outcome.unavailable_slots:
        reasons.append(Reason("unavailable", outcome.unavailable_slots, runs_of("unavailable")))
    if outcome.due_slots:
        reasons.append(Reason("due", outcome.due_slots, runs_of("due")))

    states = {_REASON_STATE[reason.code] for reason in reasons}
    state = next((s for s in STATE_ORDER if s in states), "ok")
    return _report(instrument, state, tuple(reasons), outcome, build_strip(outcome.runs, oldest, window))


def _symbol_body(report: SymbolReport) -> dict[str, object]:
    return {
        "venue": report.venue,
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "state": report.state,
        "reasons": [{"code": r.code, "slots": r.slots, "runs": r.runs, "detail": r.detail} for r in report.reasons],
        "oldest_open_time_ms": report.oldest_open_time_ms,
        "newest_open_time_ms": report.newest_open_time_ms,
        "as_of_ms": report.as_of_ms,
        "strip": report.strip,
    }


def build_overview(
    health: Health,
    window: Window,
    reports: Sequence[SymbolReport],
    *,
    duration_ms: int,
    deadline_hit: bool,
    open_interest: "OiPart | None" = None,
) -> dict[str, object]:
    """The object the page draws (spec 5.8). The clock and the rule are the API's, from ``/health/ingest``.

    Without ``open_interest`` the object is exactly the candle-only one. With it, keys are only **added**: a top-level
    ``open_interest`` block, ``candle_not_asked``, ``oi_deadline_hit`` and ``oi_not_asked`` in ``refresh`` and an
    ``open_interest`` block per symbol (``None`` when disabled). Every existing key keeps its meaning (design 8.7).
    """
    summary: dict[str, int] = {"total": len(reports), **dict.fromkeys(STATE_ORDER, 0)}
    for report in reports:
        summary[report.state] += 1
    refresh: dict[str, object] = {"duration_ms": duration_ms, "deadline_hit": deadline_hit}
    symbols = [_symbol_body(report) for report in reports]
    body: dict[str, object] = {
        "generated_at_ms": health.as_of_ms,
        "rule": {
            "publication_delay_ms": health.rule.publication_delay_ms,
            "lag_threshold_ms": health.rule.lag_threshold_ms,
        },
        "window": {"start": window.start, "end": window.end, "bucket_slots": BUCKET_SLOTS},
        "refresh": refresh,
        "summary": summary,
        "symbols": symbols,
    }
    if open_interest is None:
        return body

    judged = open_interest.reports
    if open_interest.status == "disabled":
        if judged:
            raise ValueError("a disabled open interest part has no reports")
    elif len(judged) != len(reports):
        raise ValueError(f"{len(judged)} open interest reports for {len(reports)} symbols")
    shifted = open_interest_window(window)
    refresh["candle_not_asked"] = open_interest.candle_not_asked
    refresh["oi_deadline_hit"] = open_interest.deadline_hit
    refresh["oi_not_asked"] = open_interest.oi_not_asked
    body["open_interest"] = {
        "status": open_interest.status,
        "detail": open_interest.detail,
        "summary": None if open_interest.status == "disabled" else open_interest_summary(judged),
        "window": {"start": shifted.start, "end": shifted.end},
    }
    for index, symbol in enumerate(symbols):
        symbol["open_interest"] = None if open_interest.status == "disabled" else _oi_body(judged[index])
    return body


def page_error(invalid: Invalid) -> dict[str, object]:
    """A failure of the whole page: no symbol states are drawn (spec 5.9)."""
    return {"api_error": {"code": invalid.code, "detail": invalid.detail}}


# --- open interest (OI-6b, design 8.4, 8.6, 8.7) ----------------------------------------------------------------------

_OI_HEADLINE: Final = {"stale": "stale", "conflict": "conflict", "missing": "holes", "due": "behind"}


@dataclass(frozen=True)
class OiPart:
    """The open interest half of one refresh. ``status``: ``disabled``, ``unknown`` (the health object cannot be
    used) or ``enabled``; ``reports`` line up by position with the candle reports (none when disabled)."""

    status: str
    detail: str
    reports: tuple[OiReport, ...]
    deadline_hit: bool
    candle_not_asked: int
    oi_not_asked: int


def _oi_report(
    state: str, reasons: tuple[Reason, ...], coverage: OiCoverage | None = None, strip: str | None = None
) -> OiReport:
    return OiReport(
        state=state,
        reasons=reasons,
        oldest_time_ms=coverage.oldest_time_ms if coverage else None,
        newest_time_ms=coverage.newest_time_ms if coverage else None,
        as_of_ms=coverage.as_of_ms if coverage else None,
        retrieval_horizon_ms=coverage.retrieval_horizon_ms if coverage else None,
        strip=strip,
    )


def judge_open_interest(instrument: InstrumentRow, outcome: OiCoverage | Invalid, oi_window: Window) -> OiReport:
    """The state and all reasons of one symbol's open interest (design 8.4). ``outcome`` has passed validation.

    Slots before the oldest stored row are not problems (step A): ``missing`` and ``no_longer_recoverable`` slots
    there are cut off. A ``no_longer_recoverable`` slot inside the stored history is the information ``unrecoverable``
    (option C): it has no headline state. After the newest row such a slot is never a reason.
    """
    del instrument  # the answer was validated against it; the judgement needs only the answer
    if isinstance(outcome, Invalid):
        if outcome == Invalid("api_error", "unknown_series"):
            return _oi_report("no_data", (Reason("no_data", 0, 0),))
        detail = f"{outcome.code}:{outcome.detail}" if outcome.detail else outcome.code
        return _oi_report("unknown", (Reason("invalid", 0, 0, detail),))

    oldest, newest = outcome.oldest_time_ms, outcome.newest_time_ms
    missing_slots = missing_runs = unrecoverable_slots = unrecoverable_runs = 0
    runs_of = dict.fromkeys(("stale", "conflicted", "due"), 0)
    for run in outcome.runs:
        if run.state in runs_of:
            runs_of[run.state] += 1
        elif run.state in ("missing", "no_longer_recoverable"):
            slots = (run.end - max(run.start, oldest)) // TF_MS if run.end > oldest else 0
            if slots <= 0:
                continue  # entirely before the first stored row
            if run.state == "missing":
                missing_slots += slots
                missing_runs += 1
            elif run.end <= newest:  # inside the stored history; after the newest row it is not a reason
                unrecoverable_slots += slots
                unrecoverable_runs += 1

    reasons: list[Reason] = []
    if outcome.stale_slots:
        reasons.append(Reason("stale", outcome.stale_slots, runs_of["stale"]))
    if outcome.conflicted_slots:
        reasons.append(Reason("conflict", outcome.conflicted_slots, runs_of["conflicted"]))
    if missing_slots:
        reasons.append(Reason("missing", missing_slots, missing_runs))
    if outcome.due_slots:
        reasons.append(Reason("due", outcome.due_slots, runs_of["due"]))
    if unrecoverable_slots:
        reasons.append(Reason("unrecoverable", unrecoverable_slots, unrecoverable_runs))

    states = {_OI_HEADLINE[reason.code] for reason in reasons if reason.code in _OI_HEADLINE}
    state = next((s for s in STATE_ORDER if s in states), "ok")
    return _oi_report(state, tuple(reasons), outcome, build_open_interest_strip(outcome.runs, oldest, oi_window))


def open_interest_summary(reports: Sequence[OiReport]) -> dict[str, int]:
    """Counts of symbols by open interest headline, in the shape of the candle ``summary``."""
    summary: dict[str, int] = {"total": len(reports), **dict.fromkeys(STATE_ORDER, 0)}
    for report in reports:
        summary[report.state] += 1
    return summary


def _oi_body(report: OiReport) -> dict[str, object]:
    return {
        "state": report.state,
        "reasons": [{"code": r.code, "slots": r.slots, "runs": r.runs, "detail": r.detail} for r in report.reasons],
        "oldest_time_ms": report.oldest_time_ms,
        "newest_time_ms": report.newest_time_ms,
        "as_of_ms": report.as_of_ms,
        "retrieval_horizon_ms": report.retrieval_horizon_ms,
        "strip": report.strip,
    }
