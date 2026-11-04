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


# Open interest (OI-6b, design 8; contract 6.6). A slot is a period END, so the window is shifted by one period.
OI_PATH: Final = "/v1/open-interest/coverage"
OI_STATE_CHARS: Final = {
    "not_yet_expected": "n",
    "no_longer_recoverable": "r",
    "due": "d",
    "missing": "m",
    "conflicted": "c",
    "stale": "s",
}
OI_STRIP_RANK: Final = "_.rndmcs"  # lowest to highest; "r" outranks "." so a permanent hole stays visible
OI_RUN_STATES: Final = ("conflicted", "no_longer_recoverable", "missing", "not_yet_expected", "due", "stale")
# Contract 6.6.4, P8: among slots without a row the state never decreases along the time axis (conflicted excepted).
OI_STATE_RANK: Final = {"no_longer_recoverable": 0, "missing": 1, "stale": 2, "due": 3, "not_yet_expected": 4}


# Funding rate (FR-6, design 4; contract 6.7). The series has no grid: one call per symbol, no strip.
FUNDING_PATH: Final = "/v1/funding-rates"
FUNDING_LIMIT: Final = 2  # the minimum (C§6.7.2); coverage speaks for the whole window whatever the limit (C§6.7.5)
DAY_MS: Final = 86_400_000
MAX_FUNDING_WINDOW_MS: Final = 365 * DAY_MS  # C§6.7.2
FUNDING_WINDOW_STATES: Final = ("verified", "partly_verified", "unverified")
FUNDING_STATUSES: Final = ("no_series", "degraded", "behind", "catching_up", "current")
# Headline states of one symbol's funding, worst first (design 4.3).
FUNDING_STATE_ORDER: Final = (
    "no_data",
    "unknown",
    "conflict",
    "quiet",
    "behind",
    "unverified",
    "partly_verified",
    "ok",
)


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


@dataclass(frozen=True)
class OiRule:
    cadence: int
    expected_after_ms: int
    lag_threshold_ms: int


@dataclass(frozen=True)
class OiHealth:
    """``health.open_interest``, validated: the rule every open interest answer must repeat."""

    rule: OiRule


@dataclass(frozen=True)
class OiCoverage:
    as_of_ms: int
    retrieval_horizon_ms: int
    oldest_time_ms: int
    newest_time_ms: int
    present_slots: int
    conflicted_slots: int
    not_yet_expected_slots: int
    no_longer_recoverable_slots: int
    due_slots: int
    stale_slots: int
    missing_slots: int
    runs: tuple[Run, ...]


@dataclass(frozen=True)
class OiReport:
    """One symbol's open interest, judged. It keeps no runs: the worker judges and drops them."""

    state: str
    reasons: tuple[Reason, ...]
    oldest_time_ms: int | None
    newest_time_ms: int | None
    as_of_ms: int | None
    retrieval_horizon_ms: int | None
    strip: str | None


@dataclass(frozen=True)
class FundingWindow:
    """The funding request window ``[start, end]``: both ends **inclusive** and not aligned (C§6.7.2).

    Not a ``Window``: that one is aligned with an exclusive end, and sharing the type would invite misuse.
    """

    start: int
    end: int


@dataclass(frozen=True)
class FundingRule:
    publication_delay_ms: int
    behind_after_ms: int
    quiet_after_ms: int
    backfill_days: int


@dataclass(frozen=True)
class FundingHealth:
    """``health.funding_rate``, validated. The counts are the API's own figures over all stored series."""

    rule: FundingRule
    status: str
    series_count: int
    current: int
    behind: int
    catching_up: int
    conflicted_series: int
    quiet_series: int


@dataclass(frozen=True)
class FundingCoverage:
    """One validated ``GET /v1/funding-rates`` answer. No row is kept: the rows are not used (design 4.1)."""

    as_of_ms: int
    oldest_funding_time_ms: int | None
    newest_funding_time_ms: int | None
    verified_from_ms: int | None
    verified_through_ms: int | None
    verified_at_ms: int | None
    window_state: str
    events_in_window: int
    conflicted_events: int
    complete: bool


@dataclass(frozen=True)
class FundingReport:
    """One symbol's funding, judged: the display fields of the answer, no more."""

    state: str
    reasons: tuple[Reason, ...]
    oldest_funding_time_ms: int | None
    newest_funding_time_ms: int | None
    verified_from_ms: int | None
    verified_through_ms: int | None
    verified_at_ms: int | None
    window_state: str | None
    events_in_window: int | None
    conflicted_events: int | None
    complete: bool | None
    as_of_ms: int | None
