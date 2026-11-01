"""Judging one symbol and building the overview object (pure; spec 5.4, 5.8).

The tool defines no time threshold of its own: ``due`` and ``stale`` come from the counts of the coverage answer,
which the API computed with its own clock and rule.
"""

from collections.abc import Sequence
from typing import Final

from tools.symbol_overview.model import (
    BUCKET_SLOTS,
    TF_MS,
    Coverage,
    Health,
    InstrumentRow,
    Invalid,
    Reason,
    SymbolReport,
    Window,
)
from tools.symbol_overview.window import build_strip

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
    health: Health, window: Window, reports: Sequence[SymbolReport], *, duration_ms: int, deadline_hit: bool
) -> dict[str, object]:
    """The object the page draws (spec 5.8). The clock and the rule are the API's, from ``/health/ingest``."""
    summary: dict[str, int] = {"total": len(reports), **dict.fromkeys(STATE_ORDER, 0)}
    for report in reports:
        summary[report.state] += 1
    return {
        "generated_at_ms": health.as_of_ms,
        "rule": {
            "publication_delay_ms": health.rule.publication_delay_ms,
            "lag_threshold_ms": health.rule.lag_threshold_ms,
        },
        "window": {"start": window.start, "end": window.end, "bucket_slots": BUCKET_SLOTS},
        "refresh": {"duration_ms": duration_ms, "deadline_hit": deadline_hit},
        "summary": summary,
        "symbols": [_symbol_body(report) for report in reports],
    }


def page_error(invalid: Invalid) -> dict[str, object]:
    """A failure of the whole page: no symbol states are drawn (spec 5.9)."""
    return {"api_error": {"code": invalid.code, "detail": invalid.detail}}
