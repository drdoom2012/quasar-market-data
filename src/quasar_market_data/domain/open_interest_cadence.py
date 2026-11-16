"""Open interest request cadence, budget shares and round assignment (ADR-0003 P-10, amendment A-6).

Each configured symbol is asked every ``c`` periods. ``c`` is not configured: it is derived from the
symbol count ``S`` and the configured budget ``b`` (request starts per 5 minutes) by
``open_interest_cadence``. The Collector and the API both derive it from this function and the same
configuration, so they agree.

``steady(b) = floor(3b / 4)``. A round asks ``n = ceil(S / c)`` symbols and hands out at most
``retry_reserve(n) = min(RETRY_RESERVE, n)`` retries, shared by the whole round, plus two server-time calls:
``round_cost(S, c) = n + retry_reserve(n) + 2``. ``c(S, b)`` is the smallest ``c >= 1`` with
``round_cost(S, c) <= steady(b)``. The rest of ``b``, at least ``b - steady(b)``, is the backfill share.
``c`` above ``MAX_CADENCE`` is refused (fail closed).

Integer arithmetic only.
"""

from collections.abc import Sequence
from typing import Final

from quasar_market_data.domain.candle import ensure_symbol
from quasar_market_data.domain.timeframe import Timeframe
from quasar_market_data.domain.timestamps import ensure_timestamp_ms

MIN_BUDGET_PER_5MIN: Final = 100
MAX_BUDGET_PER_5MIN: Final = 400
MAX_CADENCE: Final = 12
RETRY_RESERVE: Final = 50
# Request slots a round keeps in flight. The Collector's ``MAX_IN_FLIGHT`` must equal it; a test pins that, because
# the domain does not import the Collector.
ROUND_IN_FLIGHT: Final = 2
PACE_MS_PER_REQUEST: Final = 600


class CadenceError(ValueError):
    """No cadence up to ``MAX_CADENCE`` fits the symbol count within the budget."""


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _ensure_int(value: int, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    return value


def steady_budget(budget_per_5min: int) -> int:
    """Request starts per 5 minutes available to rounds, retries and server time: ``floor(3b / 4)``."""
    budget = _ensure_int(budget_per_5min, "budget_per_5min")
    if not MIN_BUDGET_PER_5MIN <= budget <= MAX_BUDGET_PER_5MIN:
        raise ValueError(f"budget_per_5min must be within [{MIN_BUDGET_PER_5MIN}, {MAX_BUDGET_PER_5MIN}], got {budget}")
    return budget * 3 // 4


def retry_reserve(round_size: int) -> int:
    """Retries one round may start in all: ``min(RETRY_RESERVE, n)`` for a round of ``n >= 1`` symbols."""
    size = _ensure_int(round_size, "round_size")
    if size < 1:
        raise ValueError(f"round_size must be >= 1, got {size}")
    return min(RETRY_RESERVE, size)


def round_cost(symbol_count: int, c: int) -> int:
    """Request starts per 5 minutes a round needs: its requests, the shared retry reserve and two time calls."""
    n = _ceil_div(symbol_count, c)
    return n + retry_reserve(n) + 2


def largest_symbol_count(budget_per_5min: int) -> int:
    """Largest symbol count accepted at ``budget_per_5min``: the symbols of ``MAX_CADENCE`` full rounds."""
    steady = steady_budget(budget_per_5min)
    # n + min(R, n) + 2 <= steady: 2n + 2 while n <= R, otherwise n + R + 2
    if steady >= 2 * RETRY_RESERVE + 2:
        per_round = steady - RETRY_RESERVE - 2
    else:
        per_round = (steady - 2) // 2
    return MAX_CADENCE * per_round


def pace_limit_ms(round_size: int) -> int:
    """Span above which a round counts as slow: ``ceil(n / ROUND_IN_FLIGHT)`` requests of 600 ms each."""
    size = _ensure_int(round_size, "round_size")
    if size < 1:
        raise ValueError(f"round_size must be >= 1, got {size}")
    return _ceil_div(size, ROUND_IN_FLIGHT) * PACE_MS_PER_REQUEST


def open_interest_cadence(symbol_count: int, budget_per_5min: int) -> int:
    """Smallest ``c >= 1`` whose round, shared reserve and time starts fit ``steady(b)``; ``CadenceError`` above 12."""
    count = _ensure_int(symbol_count, "symbol_count")
    if count < 1:
        raise ValueError(f"symbol_count must be >= 1, got {count}")
    steady = steady_budget(budget_per_5min)
    for c in range(1, MAX_CADENCE + 1):
        if round_cost(count, c) <= steady:
            return c
    raise CadenceError(
        f"symbol_count {count} needs a cadence above {MAX_CADENCE} at budget_per_5min {budget_per_5min}; "
        f"the largest accepted symbol count is {largest_symbol_count(budget_per_5min)}"
    )


def backfill_share(symbol_count: int, budget_per_5min: int) -> int:
    """Request starts per 5 minutes left for the backfill: ``b - round_cost(S, c)``."""
    c = open_interest_cadence(symbol_count, budget_per_5min)
    return budget_per_5min - round_cost(symbol_count, c)


def symbols_for_round(symbols: Sequence[str], boundary_ms: int, c: int, period: Timeframe) -> tuple[str, ...]:
    """Symbols asked in the round of ``boundary_ms``, in sorted order.

    The symbol at index ``i`` of the sorted list is asked iff ``(boundary_ms // period) % c == i % c``, so
    every symbol is asked exactly once per ``c`` consecutive rounds, whatever the input order.
    """
    checked = [ensure_symbol(symbol) for symbol in symbols]
    if len(set(checked)) != len(checked):
        raise ValueError("symbols must not contain duplicates")
    cadence = _ensure_int(c, "c")
    if not 1 <= cadence <= MAX_CADENCE:
        raise ValueError(f"c must be within [1, {MAX_CADENCE}], got {cadence}")
    if type(period) is not Timeframe:
        raise TypeError(f"period must be Timeframe, got {type(period).__name__}")
    boundary = ensure_timestamp_ms(boundary_ms, "boundary_ms")
    if not period.is_aligned(boundary):
        raise ValueError(f"boundary_ms {boundary} is not aligned to period {period.label}")
    residue = (boundary // period.milliseconds) % cadence
    return tuple(symbol for index, symbol in enumerate(sorted(checked)) if index % cadence == residue)
