"""Run-wide watch on assumption A1: the server's forming slot is at most ``REACH_MARGIN_SLOTS`` ahead (S17, F-12).

It only observes: it sets no clock, adds no request and changes no behaviour. A warning is raised when a *new*
maximum above the margin appears, so a sustained lead warns once; the per-round figure on the round line shows it
every round. It is a diagnosis after the fact, not a guard.
"""

from collections.abc import Callable

from quasar_market_data.domain.reconcile import REACH_MARGIN_SLOTS, clock_lead_slots


class ClockLeadMonitor:
    def __init__(self, timeframe_ms: int, warn: Callable[[int, bool], None]) -> None:
        self._timeframe_ms = timeframe_ms
        self._warn = warn
        self._first_round = True
        self.max_lead: int | None = None

    def observe(self, symbol: str, forming_open_ms: int, wall_ms: int) -> None:
        lead = clock_lead_slots(forming_open_ms, wall_ms, self._timeframe_ms)
        if self.max_lead is not None and lead <= self.max_lead:
            return
        self.max_lead = lead
        if lead > REACH_MARGIN_SLOTS:
            self._warn(lead, self._first_round)

    def first_round_done(self) -> None:
        self._first_round = False
