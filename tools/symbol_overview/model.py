"""Constants and plain data types of the symbol overview tool. No logic here.

The tool is a separate consumer of the HTTP API: it does not import ``quasar_market_data``, so the few contract
values it needs are repeated here with their source (``docs/contracts/api-v1.md``, "C§").
"""

from dataclasses import dataclass
from typing import Final

TF_MS: Final = 300_000  # the only timeframe of API v1 (C§5.1): 5m
WINDOW_SLOTS: Final = 8640  # 30 days of 5m; at most 10 000 slots per coverage request (C§6.1.1)
BUCKET_SLOTS: Final = 12  # one hour per strip character
BUCKETS: Final = WINDOW_SLOTS // BUCKET_SLOTS  # 720
MIN_TS_MS: Final = 946_684_800_000  # valid times, C§1
MAX_TS_MS: Final = 4_102_444_800_000
VENUE: Final = "binance-usdm"
TIMEFRAME: Final = "5m"

REFRESH_DEADLINE_MS: Final = 60_000  # no new API call starts after this long into a refresh (spec 5.10)
CALL_TIMEOUT_MS: Final = 10_000  # longer than SQLite's 5 s busy timeout (spec V18)
MAX_BODY_BYTES: Final = 2_000_000  # a 10 000-run coverage body is about 0.7 MB (C§6.1.3)

# Strip alphabet (spec 5.7). "_" = history not there yet, "." = present; the others are the non-present slot states.
STATE_CHARS: Final = {
    "not_yet_expected": "n",
    "due": "d",
    "unavailable": "u",
    "missing": "m",
    "conflicted": "c",
    "stale": "s",
}
STRIP_RANK: Final = "_.ndumcs"  # lowest to highest; the worst state of a bucket wins


# The closed set of reasons an answer is not trusted (spec 5.6, 5.9, 5.10). ``api_error`` carries the API's own
# ``error.code`` as its detail.
INVALID_CODES: Final = frozenset(
    {
        "bad_status",
        "api_error",
        "bad_content_type",
        "too_large",
        "bad_json",
        "schema",
        "counts",
        "runs",
        "inconsistent_runs",
        "echo",
        "rule_mismatch",
        "clock_inconsistent",
        "regressed",
        "inconsistent_with_instruments",
        "empty_universe",
        "transport",
        "deadline",
    }
)


@dataclass(frozen=True)
class Rule:
    publication_delay_ms: int
    lag_threshold_ms: int


@dataclass(frozen=True)
class Window:
    start: int  # inclusive, aligned
    end: int  # exclusive, aligned


@dataclass(frozen=True)
class Health:
    as_of_ms: int  # the API server's clock
    rule: Rule


@dataclass(frozen=True)
class InstrumentRow:
    venue: str
    symbol: str
    timeframe: str
    newest_open_time_ms: int | None  # None: nothing stored


@dataclass(frozen=True)
class Run:
    state: str  # a non-present slot state
    start: int
    end: int  # exclusive


@dataclass(frozen=True)
class Coverage:
    as_of_ms: int
    rule: Rule
    oldest_open_time_ms: int
    newest_open_time_ms: int
    stored_ahead_of_as_of: bool
    present_slots: int
    conflicted_slots: int
    unavailable_slots: int
    missing_slots: int
    not_yet_expected_slots: int
    due_slots: int
    stale_slots: int
    runs: tuple[Run, ...]


@dataclass(frozen=True)
class Invalid:
    """Why an answer cannot be trusted. ``code`` is from a closed set; ``detail`` adds the API's own error code."""

    code: str
    detail: str = ""


@dataclass(frozen=True)
class Reason:
    code: str
    slots: int
    runs: int
    detail: str = ""


@dataclass(frozen=True)
class SymbolReport:
    venue: str
    symbol: str
    timeframe: str
    state: str
    reasons: tuple[Reason, ...]
    oldest_open_time_ms: int | None
    newest_open_time_ms: int | None
    as_of_ms: int | None
    strip: str | None
