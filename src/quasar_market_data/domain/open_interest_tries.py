"""In-memory memory of slots Binance answered without their row (ADR-0003 P-12, "Holes Binance does not serve").

Binance documents that a period without trades has no record. Such a slot stays a hole for ever; without a rule it
would be asked for in every idle window. A slot is *tried* when a page that planned it succeeded and the slot is still
absent from the response. After the 1st try it waits 1 h, after the 2nd 6 h, after the 3rd it is not planned again in
this process run. Nothing is stored: a restart forgets everything, so every hole gets at most one new try per start.

The book never decides what a try is (the caller does: a failed page, or a response with a pending row, is not a
try) and never writes to the database. Times are **monotonic** milliseconds.

Memory is kept as runs of consecutive slots with the same state, so a page that tried 500 slots together costs one
run; the bound is the number of distinct runs, at most the number of holes (``H`` / period per series).
"""

from collections.abc import Collection, Iterable, Mapping
from typing import Final

from quasar_market_data.domain.timeframe import Timeframe

MAX_TRIES: Final = 3
COOLDOWN_AFTER_TRY_MS: Final = (3_600_000, 21_600_000, None)  # after the 1st, 2nd and 3rd try; None: not again

type _State = tuple[int, int]  # tries, next allowed monotonic ms
type _Run = tuple[int, int, _State]  # first slot, last slot (inclusive), state


def _int(value: int, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    return value


class OpenInterestTryBook:
    def __init__(self, period: Timeframe) -> None:
        if type(period) is not Timeframe:
            raise TypeError(f"period must be Timeframe, got {type(period).__name__}")
        self._step = period.milliseconds
        self._runs: dict[str, list[_Run]] = {}

    def _expand(self, symbol: str) -> dict[int, _State]:
        states: dict[int, _State] = {}
        for first, last, state in self._runs.get(symbol, ()):
            for slot in range(first, last + 1, self._step):
                states[slot] = state
        return states

    def _store(self, symbol: str, states: Mapping[int, _State]) -> None:
        runs: list[_Run] = []
        for slot in sorted(states):
            state = states[slot]
            if runs and runs[-1][1] + self._step == slot and runs[-1][2] == state:
                runs[-1] = (runs[-1][0], slot, state)
            else:
                runs.append((slot, slot, state))
        if runs:
            self._runs[symbol] = runs
        else:
            self._runs.pop(symbol, None)

    def _check(self, slots: Iterable[int]) -> list[int]:
        checked = []
        for slot in slots:
            if _int(slot, "slot") % self._step:
                raise ValueError(f"slot {slot} is not aligned to the period")
            checked.append(slot)
        return checked

    def available(self, symbol: str, slots: Iterable[int], now_ms: int) -> tuple[int, ...]:
        """The slots that may be planned now: fewer than 3 tries and the cooldown over (at its end, inclusive)."""
        now = _int(now_ms, "now_ms")
        states = self._expand(symbol)
        out = []
        for slot in self._check(slots):
            state = states.get(slot)
            if state is None or (state[0] < MAX_TRIES and now >= state[1]):
                out.append(slot)
        return tuple(out)

    def record_tries(self, symbol: str, slots: Iterable[int], now_ms: int) -> None:
        """Count one try for each slot and start its cooldown."""
        now = _int(now_ms, "now_ms")
        checked = self._check(slots)
        if not checked:
            return
        states = self._expand(symbol)
        for slot in checked:
            tries = (states[slot][0] if slot in states else 0) + 1
            cooldown = COOLDOWN_AFTER_TRY_MS[min(tries, MAX_TRIES) - 1]
            states[slot] = (tries, now + cooldown if cooldown is not None else 0)
        self._store(symbol, states)

    def forget_present(self, symbol: str, holes: Collection[int]) -> None:
        """Drop the memory of every slot that is no longer a hole (it was stored in the meantime)."""
        keep = set(holes)
        states = {slot: state for slot, state in self._expand(symbol).items() if slot in keep}
        self._store(symbol, states)

    def exhausted(self, symbol: str) -> int:
        return sum(
            (last - first) // self._step + 1
            for first, last, state in self._runs.get(symbol, ())
            if state[0] >= MAX_TRIES
        )

    def exhausted_counts(self) -> Mapping[str, int]:
        counts = {symbol: self.exhausted(symbol) for symbol in sorted(self._runs)}
        return {symbol: count for symbol, count in counts.items() if count}

    def run_count(self, symbol: str) -> int:
        return len(self._runs.get(symbol, ()))
