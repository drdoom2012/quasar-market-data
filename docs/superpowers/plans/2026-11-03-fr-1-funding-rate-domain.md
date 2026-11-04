# FR-1 Funding Rate Domain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pure building blocks for funding rate history: the `FundingRate` value object, eligibility, the page-coverage and span `merge` rules, the history flags, the sustainable-capacity and latency arithmetic, and a strict all-or-nothing parser for raw `fundingRate` rows. No I/O, no storage, no network (issue #16).

**Architecture:** Three domain modules and one collector module, mirroring OI-1. `domain/funding_rate.py` holds identity, values, eligibility and the off-hour heuristic (ADR-0004 P-2, P-3, P-4). `domain/funding_rate_span.py` holds the closed-interval arithmetic of the verified span: page coverage, adjacency `merge`, `history_pending`, `behind`, the next history chunk (P-5, P-7, P-10, P-11). `domain/funding_rate_capacity.py` holds the limiter shares, `K`, `Q`, `S_sustained`, the warning threshold and the planned start offset (P-9, P-10, P-12). `collector/binance_funding_rate.py` parses one raw response strictly, like `collector/binance_open_interest.py` (P-3, P-5); it imports only the stdlib and `domain`.

**Tech Stack:** Python 3.12, stdlib only, pytest, mypy strict, ruff, import-linter.

**Spec:** `docs/adr/0004-funding-rate.md` (P-2, P-3, P-4, P-5, P-7, P-9, P-10, P-12 ranges, P-15 rows marked FR-1, P-16). Related: `docs/adr/0001-binance-ingestion-fidelity-and-closed-candles.md` (P-4 time trust, P-7 decimal grammar), `docs/adr/0003-open-interest.md`. Pattern to copy: `docs/superpowers/plans/2026-11-02-oi-1-open-interest-domain.md` and the code it produced (`domain/open_interest.py`, `domain/open_interest_cadence.py`, `collector/binance_open_interest.py`).

## Global Constraints

- Branch `fr-1-funding-rate-domain` from `main`; one commit per task, message given in each task, trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Push and open a PR only at the end (closes #16). A PreToolUse hook runs `make check` on `git commit`; run `make check` before each commit and report the real output.
- `domain` imports only the stdlib allowlist in `tests/test_domain_purity.py` (`__future__`, `collections`, `dataclasses`, `decimal`, `enum`, `re`, `typing`) and itself. No I/O, no clock reads, no `float`, no `math`. Integer arithmetic only.
- `collector/binance_funding_rate.py` imports only the stdlib, `domain` and `collector/binance_klines.py` (for `VENUE_BINANCE_USDM`); never `ccxt`, `fastapi`, `talib`, `sqlite3` or `api`.
- Do not edit `pyproject.toml`, `uv.lock`, the purity allowlist, the import-linter contracts, `docs/adr/`, `docs/contracts/` or any storage, API, config or other collector module. No new dependencies. No schema change. `domain/candle.py` already exports `ensure_venue` and `ensure_symbol`; reuse them.
- Values stay the strings Binance sends, canonicalized by `canonical_decimal` (ADR-0001 P-7); a JSON number in a value slot is an error, never converted (P-1, P-2). `funding_time_ms` is stored as reported, never rounded to the hour (P-3).
- Fail closed: a malformed response yields no rows and verifies nothing; untrusted time makes nothing eligible (P-4, P-5).
- Values from ADR-0004: publication delay `30_000` ms (P-4); page `limit` 1 000 (P-5); budget `50 <= b <= 250`; limiter window `305_000` ms; `round_offset_ms` in `[60_000, 600_000]` (P-12); history chunk 90 days; `behind` threshold `7_200_000` ms; `backfill_days` in `[1, 3650]` (P-10, P-12).
- TDD: failing test first; record RED and GREEN output. Deliberately wrong runtime types only through `tests/typing_helpers.py::untyped`. Test files go in `tests/domain/` and `tests/collector/`.

## Decisions this plan makes (not fixed by the ADR)

- **Ordering of a coincident pair (user decision, 2026-11-03):** P-5 lists "rows not strictly ascending by `(fundingTime, rateType)`" as a parse error but also says page coverage is correct "whatever the order of two rows with the same `fundingTime`", and P-3 refuses rejections that would stall a series' span. The user chose: `fundingTime` must be **non-decreasing** and the key `(fundingTime, rateType)` **unique** within a response; a Regular and a Special row of one `fundingTime` are accepted in either order. Only time-ascending is documented by Binance [D]. The tests pin both orders.
- **A leading `+` in `fundingRate` is accepted** and canonicalized away, because the value goes through the shared grammar of ADR-0001 P-7 and a sign is legitimate for this field. Binance never sends it. `-0`, `-0.00000000` become `0` (`canonical_decimal` already does this). Exponent forms (`"1e-4"`) are accepted for the same reason, as in OI-1.
- **`markPrice` rejects any sign**, `"+1"` and `"-0"` included. That is stricter than P-5's "non-negative decimal string" (both are numerically non-negative); it fails closed and mirrors OI-1. `markPrice ""` is the only way to a `None`.
- **The off-hour heuristic is inclusive at 5 000 ms**: an offset of 5 000 ms is on-hour, 5 001 ms is off-hour (the ADR says "within 5 s"). `count_off_hour_rows` is a pure helper so FR-3/FR-4 only log it.
- **Near `MIN_TIMESTAMP_MS`:** a history chunk or target below the schema range raises `ValueError` from `Interval`/`ensure_timestamp_ms` (fail closed). It needs a series whose `through_ms` is before 2010, so it is unreachable in production; it is tested so the failure is a `ValueError`, not silence.
- **`markPrice ""` is turned into `None` by the parser**, not by the value object. The value object accepts `str | None` only, and `""` there is an `InvalidDecimalError`.
- **The off-hour heuristic is a predicate only** (`is_off_hour`): a `fundingTime` more than `5_000` ms after a whole hour. It is never a rejection. Counting (`off_hour_rows`) and logging belong to the Collector slices (FR-3/FR-4).
- **Every `Interval` is validated with `ensure_timestamp_ms`** (the schema CHECK range of `funding_rate_verified`), field names `from_ms` and `through_ms` as in the table. A request range is the same type.
- **`page_coverage` takes the parsed rows**, not counts, and derives `n` and `last` itself. A page of exactly `limit` rows whose last `fundingTime` is not above the range start is a `ValueError("... does not advance")`. P-5's progress proof needs `limit >= 3` (1 000 keys over two rate types span at least 500 distinct milliseconds); for `limit` 1 or 2 a valid parse can reach the guard (one row at `a`, or a Regular and a Special row at `a`). Production always uses 1 000; the guard is tested with `limit = 2`.
- **`merge_span` is pure and raises `NotAdjacentError`**; the SQL `merge` of FR-2 is the enforcement and must agree with it (same boundary tests: `a = through + 1` accepted, `+ 2` refused).
- **Seed/R/C request ranges stay in FR-4**; this slice supplies only the pieces whose boundaries P-15 tests in FR-1: `forward_end_bound_ms`, `next_history_chunk`, `history_pending`.
- **`assess_capacity` requires `symbol_count >= 1`** (`ValueError`), like `open_interest_cadence`; `S = 0` is not a funding configuration.
- **Invariant register:** this slice adds two ENFORCED rows (eligibility, strict parser). The rows for the span, the limiter, conflicts and isolation need FR-2 to FR-4.

## Review Focus

1. A page of exactly 1 000 rows whose last two rows share a `fundingTime` (Regular then Special, and Special then Regular): the parser accepts both orders and `page_coverage` covers `[a, last − 1]`, continues at `last`, skips neither (Tasks 3, 5).
2. Signs: `fundingRate` `"-0.00000000"` → `"0"`, `"-0.0001"` kept; `markPrice` `"-1"`, `"+1"` and `" 1"` rejected; `markPrice ""` → `None` (Tasks 1, 5).
3. The HTTP-200 error object of ADR-0004 E-4 (`{"status": "ERROR", ...}`) and `[]`-vs-`{}`: the object is rejected with `row_index None`; the empty list returns `()` (Task 5).
4. `bool` where an `int` belongs (`fundingTime`, `limit`, budget, symbol count, bounds) → `TypeError`, never treated as `1` (Tasks 1 to 5).
5. A valid empty answer covers the whole requested range; a truncated page never claims the row at `last` (Task 3).

---

### Task 1: `FundingRate` value object and the off-hour heuristic

**Files:**
- Create: `src/quasar_market_data/domain/funding_rate.py`, `tests/domain/test_funding_rate.py`

**Interfaces:**
- Produces in `funding_rate.py`:
  - `FUNDING_RATE_PAGE_LIMIT: Final = 1_000`, `OFF_HOUR_TOLERANCE_MS: Final = 5_000`, `HOUR_MS: Final = 3_600_000`.
  - `class RateType(StrEnum)`: `REGULAR = "Regular"`, `SPECIAL = "Special"`.
  - `@dataclass(frozen=True) FundingRateKey(venue: str, symbol: str, funding_time_ms: int, rate_type: RateType)`: `ensure_venue`, `ensure_symbol`, `ensure_timestamp_ms(funding_time_ms, "funding_time_ms")`, `type(rate_type) is RateType` (`TypeError`). Module docstring: `funding_time_ms` is the settlement time as Binance reports it, it is never rounded to the hour, and it is joined to a candle by the hour it falls in, never by equality (P-3).
  - `@dataclass(frozen=True) FundingRate(key: FundingRateKey, funding_rate: str, mark_price: str | None)`: `type(key) is FundingRateKey` (`TypeError`); `funding_rate` `type(...) is str` (`TypeError`) then `canonical_decimal` (sign allowed); `mark_price`: `None` stays `None`; a non-`str` → `TypeError` (checked before anything else); a `str` starting with `+` or `-` → `InvalidDecimalError`; then `canonical_decimal` (`""` → `InvalidDecimalError`).
  - `is_off_hour(funding_time_ms: int) -> bool`: `funding_time_ms % HOUR_MS > OFF_HOUR_TOLERANCE_MS` (5 000 is on-hour, 5 001 off-hour); the argument passes `ensure_timestamp_ms`.
  - `count_off_hour_rows(rows: Sequence[FundingRate]) -> int`: how many rows are off-hour (for the `off_hour_rows` log field of FR-3/FR-4).

- [ ] **Step 1: Write failing tests** with `KEY = FundingRateKey("binance-usdm", "BTC/USDT:USDT", 1_793_736_000_005, RateType.REGULAR)`:
  - `test_rate_is_canonicalized_and_signed`: `"0.00010000"` → `"0.0001"`; `"-0.00010000"` → `"-0.0001"`; `"0.00000000"` → `"0"`; `"-0.00000000"` → `"0"`; `"+0.0001"` → `"0.0001"`.
  - `test_mark_price_is_canonicalized_or_none`: `"67123.45000000"` → `"67123.45"`; `None` stays `None`; `"0.00000000"` → `"0"`.
  - `test_mark_price_rejects_sign_and_empty`, parametrized over `"-1"`, `"-0"`, `"+1"`, `""`, `" 1"`, `"abc"` → `InvalidDecimalError`.
  - `test_rate_rejects_malformed`: `""`, `" 0.1"`, `"0.1 "`, `"abc"`, `"1,5"` → `InvalidDecimalError`; `untyped(0.0001)`, `untyped(1)`, `untyped(None)` → `TypeError`; `untyped(1.5)` as `mark_price` → `TypeError`.
  - `test_key_type_checks`: `untyped(True)` and `untyped(1.0)` as `funding_time_ms` → `TypeError`; out-of-range time → `ValueError`; `untyped("Regular")` as `rate_type` → `TypeError`; venue `"Binance"` and symbol `""` → `ValueError`.
  - `test_regular_and_special_of_one_hour_are_different_keys`: keys at `T + 1` Regular and `T + 1_002` Special (and the same `T + 1` with both types) are unequal; equal constructions are equal and hash equal.
  - `test_is_off_hour`: offsets `0`, `26`, `1_003`, `5_000` after a whole hour → `False`; `5_001` and `1_800_000` → `True`; `untyped(True)` → `TypeError`.
  - `test_count_off_hour_rows`: rows at offsets `0`, `26`, `5_001`, `1_800_000` → `2`; `[]` → `0`.
- [ ] **Step 2: Run** `uv run pytest tests/domain/test_funding_rate.py -v`. Expect: FAIL (module missing).
- [ ] **Step 3: Implement** `funding_rate.py` per the interfaces.
- [ ] **Step 4: Run** the new tests, then `make check`. Expect: PASS; record output.
- [ ] **Step 5: Commit** `feat(domain): funding rate value object (ADR-0004 P-2, P-3)`.

### Task 2: Eligibility and the forward end bound

**Files:**
- Modify: `src/quasar_market_data/domain/funding_rate.py`
- Test: `tests/domain/test_funding_rate.py`

**Interfaces:**
- Consumes: `TrustedTime`, `UntrustedTime`, `TimeRejection` from `domain/server_time.py`; `FundingRate` from Task 1.
- Produces in `funding_rate.py`:
  - `DEFAULT_FUNDING_RATE_PUBLICATION_DELAY_MS: Final = 30_000` (P-4).
  - `is_funding_rate_eligible(funding_time_ms: int, publication_delay_ms: int, lower_bound_ms: int) -> bool`: `funding_time_ms + publication_delay_ms <= lower_bound_ms` (equality is eligible). Argument rules as `is_open_interest_eligible`.
  - `forward_end_bound_ms(publication_delay_ms: int, lower_bound_ms: int) -> int`: `lower_bound_ms - publication_delay_ms` (P-4/P-7: the end `b` of every forward request). Delay as above; `lower_bound_ms` an `int`.
  - `@dataclass(frozen=True) FundingRateSelection(eligible: tuple[FundingRate, ...], pending: tuple[FundingRate, ...], untrusted_reason: TimeRejection | None)`.
  - `select_eligible_funding_rates(rows: Sequence[FundingRate], publication_delay_ms: int, time: TrustedTime | UntrustedTime) -> FundingRateSelection`: the delay is validated **first**, before the trust branch; `UntrustedTime` → all pending with its reason; `TrustedTime` → split in input order; `len(eligible) + len(pending) == len(rows)`; any other `time` type → `TypeError`.

- [ ] **Step 1: Write failing tests** (rows at `T = 1_793_736_000_000`, lower bound `T + 30_000`):
  - `test_boundary_is_eligible_and_one_millisecond_earlier_is_not`: delay `30_000`, lower bound `T + 30_000` → eligible, `T + 29_999` → not (both functions); delay `0` makes `T` the boundary.
  - `test_forward_end_bound_makes_every_row_up_to_it_eligible`: for lower bound `L` and delay `d`, `forward_end_bound_ms(d, L) == L - d`, a row at the bound is eligible and a row one ms later is not.
  - `test_mixed_rows_are_split_in_order`; `test_untrusted_time_selects_nothing` parametrized over every `TimeRejection`; `test_empty_rows`.
  - argument tests: delay `-1` → `ValueError`, also with `UntrustedTime`; `untyped(True)` / `untyped(1.5)` as delay or lower bound → `TypeError`; `untyped(object())` as `time` → `TypeError`; `DEFAULT_FUNDING_RATE_PUBLICATION_DELAY_MS == 30_000`.
- [ ] **Step 2: Run** the new tests. Expect: FAIL.
- [ ] **Step 3: Implement** per the interfaces; docstring states that an absent row is never inferred.
- [ ] **Step 4: Run** the tests, then `make check`. Expect: PASS; record output.
- [ ] **Step 5: Commit** `feat(domain): funding rate eligibility and fail-closed selection (ADR-0004 P-4)`.

### Task 3: Verified-span arithmetic

**Files:**
- Create: `src/quasar_market_data/domain/funding_rate_span.py`, `tests/domain/test_funding_rate_span.py`

**Interfaces:**
- Consumes: `FundingRate`, `FUNDING_RATE_PAGE_LIMIT` (Task 1), `ensure_timestamp_ms`.
- Produces in `funding_rate_span.py`:
  - `DAY_MS: Final = 86_400_000`, `HISTORY_CHUNK_MS: Final = 90 * DAY_MS`, `BEHIND_THRESHOLD_MS: Final = 7_200_000`, `MIN_BACKFILL_DAYS: Final = 1`, `MAX_BACKFILL_DAYS: Final = 3_650`.
  - `@dataclass(frozen=True) Interval(from_ms: int, through_ms: int)`: both `ensure_timestamp_ms`; `from_ms <= through_ms` (`ValueError`). Closed integer interval.
  - `class NotAdjacentError(ValueError)`.
  - `touches(first: Interval, second: Interval) -> bool`: `first.from_ms <= second.through_ms + 1 and second.from_ms <= first.through_ms + 1`.
  - `merge_span(current: Interval | None, covered: Interval) -> Interval`: `None` → `covered`; `touches` → the union (`min` of the starts, `max` of the ends); otherwise `NotAdjacentError`. A `covered` inside `current` returns `current`.
  - `@dataclass(frozen=True) PageCoverage(covered: Interval, next_start_ms: int | None)`.
  - `page_coverage(rows: Sequence[FundingRate], *, start_ms: int, end_ms: int, limit: int = FUNDING_RATE_PAGE_LIMIT) -> PageCoverage` (P-5): `n < limit` → `covered = [start_ms, end_ms]`, `next_start_ms = None` (an empty list included); `n == limit` → `last = rows[-1].key.funding_time_ms`, `covered = [start_ms, last - 1]`, `next_start_ms = last`. Checks: both bounds `ensure_timestamp_ms` (so `bool` is `TypeError`) and `start_ms <= end_ms`; `limit` an `int` in `[1, 1_000]`; `len(rows) > limit` → `ValueError`; every row's `funding_time_ms` inside `[start_ms, end_ms]` and non-decreasing (`ValueError`); for `n == limit`, `last > start_ms` or `ValueError` whose message contains `"does not advance"`.
  - `history_target_from_ms(through_ms: int, backfill_days: int) -> int`: `through_ms - backfill_days * DAY_MS`; `through_ms` passes `ensure_timestamp_ms`; `backfill_days` an `int` (not `bool`) in `[MIN_BACKFILL_DAYS, MAX_BACKFILL_DAYS]`.
  - `history_pending(span: Interval, backfill_days: int) -> bool`: `span.from_ms > history_target_from_ms(span.through_ms, backfill_days)`. No clock, no tolerance.
  - `next_history_chunk(span: Interval, backfill_days: int) -> Interval | None`: `None` when not pending, else `Interval(max(target_from, span.from_ms - HISTORY_CHUNK_MS), span.from_ms - 1)`.
  - `is_behind(b_now_ms: int, span: Interval) -> bool`: `b_now_ms - span.through_ms >= BEHIND_THRESHOLD_MS`; `b_now_ms` passes `ensure_timestamp_ms`.

- [ ] **Step 1: Write failing tests** (helper `row(t, rate_type=RateType.REGULAR)` builds a `FundingRate` at `t` with rate `"0.0001"`):
  - `test_interval_validation`: `from_ms > through_ms` → `ValueError`; `untyped(True)` → `TypeError`; out-of-range → `ValueError`; single-point interval allowed.
  - `test_merge_span_boundaries`: span `[X, X + 1_000_000]` with `X = 1_793_000_000_000`: `None` + `J` → `J`; extend up with `a = through + 1` accepted, `a = through + 2` → `NotAdjacentError`; extend down with `e = from - 1` accepted, `e = from - 2` → `NotAdjacentError`; overlapping, containing and contained intervals give the exact union; an interval inside the span returns the span unchanged.
  - `test_page_coverage_short_page`: 999 rows and `[]` both cover `[a, b]`, `next_start_ms is None`.
  - `test_page_coverage_full_page_continues_at_the_last_row`: exactly 1 000 rows with distinct times → covers `[a, last - 1]`, next starts at `last`; the next page's range `[last, b]` with one row (at `last`) covers `[last, b]` and `merge_span` of the two is exactly `[a, b]` (no gap, no overlap).
  - `test_full_page_with_a_shared_final_fundingtime_skips_neither_row`: 1 000 rows whose last two are Regular and Special at the same `funding_time_ms` → covers `[a, last - 1]`, next starts at `last` (the pair is fetched again, never skipped).
  - `test_empty_answer_extends_the_span`: `merge_span(span, page_coverage([], start_ms=span.through_ms + 1, end_ms=b).covered)` is `Interval(span.from_ms, b)`.
  - `test_two_thousand_five_hundred_rows_need_three_consecutive_pages`: simulate the paging loop with a fake server holding 2 500 rows in `[a, b]` and returning the oldest `limit` rows of the requested range (inclusive bounds) over `page_coverage` and `merge_span`; exactly three requests, each starting at the previous `next_start_ms`, the union is exactly `[a, b]`; the set of distinct row keys seen equals all 2 500 rows and exactly rows number 1 000 and 1 999 (1-based) were returned twice.
  - `test_page_coverage_rejects_inconsistent_input`, each case asserting its own message word via `match=`: more than `limit` rows (`"more than"`); a row outside `[start, end]` (`"outside"`); descending times (`"non-decreasing"`); a full page whose last time equals `start_ms`, with `limit = 2` and a Regular and a Special row at `start_ms` (`"does not advance"`); `limit` 0 and 1 001 (`"limit"`); `untyped(True)` as `limit`, `start_ms` and `end_ms` → `TypeError`.
  - `test_history_pending_boundary_has_no_tolerance`: width exactly `backfill_days * DAY_MS` → `False`; one ms less → `True`; 24 h less → `True`; one ms more → `False`.
  - `test_moving_target_does_not_reopen_finished_history`: a pending span whose last chunk is planned at `through_ms = t`; the span then advances to `t + 3_600_000` (a round) before the chunk is merged; after merging the chunk `history_pending` is `False`. A not-pending span advanced through 24 rounds (`+3_600_000` each, via `merge_span`) stays not pending after every round.
  - `test_history_pending_property`: a seeded random walk (fixed seeds, 200 steps, several walks) over: chunk completion (`merge_span` with `next_history_chunk`), a crash (chunk planned, not merged: state unchanged), a restart (state recomputed from the span only), a round advance (`through_ms += 3_600_000` through `merge_span`) and a change of `backfill_days` (raised or unchanged only in one walk, lowered in another). Assertions: `history_pending` goes from `False` to `True` only in a step that raised `backfill_days`; `from_ms` never rises, `through_ms` never falls; after lowering `backfill_days` no chunk is planned beyond the new target.
  - `test_next_history_chunk`: `None` when not pending; a chunk is `[from_ms - 90 d, from_ms - 1]` when the target is further away; the last chunk is shorter and its start equals `target_from` exactly. Step counts: merging each chunk into the span until `history_pending` is `False` takes `ceil((days * DAY_MS - width) / HISTORY_CHUNK_MS)` steps, asserted for a seed span of width 0 and of width 24 h with `days = 365` → 5 and 5, `90` → 1 and 1, `91` → 2 and 1.
  - `test_chunk_near_the_minimum_timestamp_fails_closed`: `from_ms == MIN_TIMESTAMP_MS` with a pending span, and a `through_ms` whose target is below the minimum, → `ValueError`, never a silent clamp.
  - `test_is_behind`: difference `7_199_999` → `False`, `7_200_000` → `True`.
  - `test_backfill_days_bounds`: 0 and 3 651 → `ValueError`; `untyped(True)`, `untyped(365.0)` → `TypeError`; 1 and 3 650 accepted.
- [ ] **Step 2: Run** `uv run pytest tests/domain/test_funding_rate_span.py -v`. Expect: FAIL.
- [ ] **Step 3: Implement** per the interfaces; module docstring cites P-5, P-7 and states that the SQL `merge` of FR-2 is the enforcement and this module is its pure specification.
- [ ] **Step 4: Run** the tests, then `make check`. Expect: PASS; record output.
- [ ] **Step 5: Commit** `feat(domain): funding rate verified-span arithmetic and history flags (ADR-0004 P-5, P-7, P-10)`.

### Task 4: Limiter shares, sustainable capacity and planned latency

**Files:**
- Create: `src/quasar_market_data/domain/funding_rate_capacity.py`, `tests/domain/test_funding_rate_capacity.py`

**Interfaces:**
- Produces in `funding_rate_capacity.py`:
  - `MIN_BUDGET_PER_5MIN: Final = 50`, `MAX_BUDGET_PER_5MIN: Final = 250`, `DEFAULT_BUDGET_PER_5MIN: Final = 150`, `MIN_ROUND_OFFSET_MS: Final = 60_000`, `MAX_ROUND_OFFSET_MS: Final = 600_000`, `LIMITER_WINDOW_MS: Final = 305_000` (300 000 + guard 5 000), `ROUND_LAST_START_BUDGET_MS: Final = 3_555_000` (3 600 000 − 15 000 deadline margin − 30 000 last-start margin), `MAX_FULL_GROUPS: Final = 11` (`3_600_000 // LIMITER_WINDOW_MS`).
  - `class CapacityError(ValueError)`: raised when `symbol_count > sustained`; the message names `symbol_count`, `budget_per_5min`, `round_offset_ms` and `sustained`.
  - `round_share(budget_per_5min: int) -> int`: `m = b // 2`; `history_catchup_share(budget_per_5min: int) -> int`: `b - m`. Budget an `int` (not `bool`, `TypeError`) in `[50, 250]` (`ValueError`).
  - `groups_k(round_offset_ms: int) -> int`: `(ROUND_LAST_START_BUDGET_MS - round_offset_ms) // LIMITER_WINDOW_MS`; offset an `int` in `[60_000, 600_000]`.
  - `full_groups_per_round(round_offset_ms: int) -> int`: `Q = min(K + 1, MAX_FULL_GROUPS)`.
  - `isolated_round_capacity(budget_per_5min: int, round_offset_ms: int) -> int`: `(K + 1) * m - 1` (documented as **not sustainable**; kept so FR-4 can test why it was replaced).
  - `sustained_capacity(budget_per_5min: int, round_offset_ms: int) -> int`: `Q * m - 1`.
  - `warning_threshold(sustained: int) -> int`: `sustained * 9 // 10`.
  - `@dataclass(frozen=True) CapacityAssessment(sustained: int, warn_above: int, warn: bool)`; `assess_capacity(symbol_count: int, budget_per_5min: int, round_offset_ms: int) -> CapacityAssessment`: `symbol_count >= 1` (`ValueError`, `TypeError` for non-`int`/`bool`); `CapacityError` when above `sustained`; `warn = symbol_count > warn_above`.
  - `planned_start_offset_ms(index: int, budget_per_5min: int) -> int`: `(index // m) * LIMITER_WINDOW_MS`, the planned start of request `index` after the wake of an isolated round (index 0 is the server-time call; the planned settlement-to-start latency adds `round_offset_ms`). `index >= 0`.

- [ ] **Step 1: Write failing tests**:
  - `test_shares`: `b = 50, 150, 250, 151` → `(25, 25), (75, 75), (125, 125), (75, 76)`; the two shares always sum to `b`.
  - `test_sustained_capacity_at_default_offset`: `b = 250, 150, 50` at `60_000` → `1_374, 824, 274`; `isolated_round_capacity` → `1_499, 899, 299`; `K = 11` and `Q = 11` at `60_000`.
  - `test_accept_and_refuse_at_the_boundary_for_every_share`: for every `b` in `50..250` (so `m` takes every value `25..125`) and every offset in `{60_000, 200_000, 200_001, 300_000, 505_000, 505_001, 600_000}`: `assess_capacity(sustained, b, offset)` is accepted and `assess_capacity(sustained + 1, b, offset)` raises `CapacityError`.
  - `test_group_arithmetic_is_consistent_with_the_window`: for every offset from `60_000` to `600_000` in steps of `1_000` plus the boundary values: `offset + K * 305_000 <= 3_555_000 < offset + (K + 1) * 305_000`, `Q * 305_000 <= 3_600_000`, and `Q <= 11`. (This checks the arithmetic against the window independently of the formula in the code.)
  - `test_offset_boundaries`: `(offset, K, Q)` = `(60_000, 11, 11)`, `(200_000, 11, 11)`, `(200_001, 10, 11)`, `(300_000, 10, 11)`, `(505_000, 10, 11)`, `(505_001, 9, 10)`, `(600_000, 9, 10)`; `59_999` and `600_001` → `ValueError`.
  - `test_assess_capacity_refuses_above_and_warns_above_ninety_percent`: for `b = 50, 150, 250` at `60_000`: `S = sustained` accepted, `S = sustained + 1` → `CapacityError` naming the key values; warn thresholds `246, 741, 1_236`: `S = threshold` no warning, `threshold + 1` warning; `659` at default → accepted without warning.
  - `test_planned_start_offset`: `b = 150`, `S = 10, 100, 300, 659` → indexes give `0, 305_000, 1_220_000, 2_440_000` (`j // 75 * 305 s`: 0, 1, 4, 8); index 74 → 0 and 75 → `305_000`; adding `round_offset_ms` reproduces the ADR latency table (60 s, 365 s, 1 280 s, 2 500 s).
  - `test_argument_checks`: `untyped(True)`, `untyped(150.0)`, `untyped("150")` as budget, offset, symbol count or index → `TypeError`; budget 49 and 251 → `ValueError`; `symbol_count` 0 → `ValueError` and `type(err) is not CapacityError`; negative `index` → `ValueError`.
- [ ] **Step 2: Run** `uv run pytest tests/domain/test_funding_rate_capacity.py -v`. Expect: FAIL.
- [ ] **Step 3: Implement** per the interfaces; module docstring cites P-9, P-10, P-12 and states that the Collector (FR-4) refuses to start above `sustained` and warns above `warn_above`.
- [ ] **Step 4: Run** the tests, then `make check`. Expect: PASS; record output.
- [ ] **Step 5: Commit** `feat(domain): funding rate limiter shares, sustainable capacity and latency (ADR-0004 P-9, P-10)`.

### Task 5: Raw `fundingRate` parser

**Files:**
- Create: `src/quasar_market_data/collector/binance_funding_rate.py`, `tests/collector/test_binance_funding_rate.py`

**Interfaces:**
- Consumes: `FundingRate`, `FundingRateKey`, `RateType`, `FUNDING_RATE_PAGE_LIMIT` (Task 1), `ensure_symbol`, `ensure_timestamp_ms`, `VENUE_BINANCE_USDM` from `collector/binance_klines.py`.
- Produces in `binance_funding_rate.py`:
  - `class FundingRateParseError(ValueError)` with `.row_index: int | None` (same shape as `OpenInterestParseError`).
  - `parse_funding_rate_history(response: object, *, market_id: str, symbol: str, start_time_ms: int, end_time_ms: int, limit: int = FUNDING_RATE_PAGE_LIMIT) -> tuple[FundingRate, ...]`.

Argument checks first (plain `TypeError`/`ValueError`): `market_id` a non-empty `str`; `ensure_symbol(symbol)`; both bounds `ensure_timestamp_ms`; `start_time_ms <= end_time_ms`; `limit` an `int` in `[1, 1_000]`.

Response checks, each a `FundingRateParseError` and nothing returned: not a `list` (this includes the error object of E-4; `row_index None`); more rows than `limit` (`row_index None`, checked before any row is parsed); per row, **in this order** (so no later guard masks an earlier one): not a `dict`; missing `symbol`, `fundingTime`, `fundingRate`, `markPrice` or `rateType`; `symbol` not equal to `market_id`; `fundingTime` not `type(...) is int`; `fundingTime` outside `[start_time_ms, end_time_ms]`; `rateType` not exactly `"Regular"` or `"Special"` (converted to `RateType`); order against the previous row (user decision): `fundingTime` below the previous row's, or the key `(fundingTime, rateType)` equal to a key already seen at that `fundingTime` (track the previous time and the rate types seen at it); `fundingRate` not a `str`; `markPrice` not a `str`; `markPrice == ""` → `None`; any error from `FundingRateKey`/`FundingRate` construction, chained. Other keys are ignored. An empty list returns `()`. Off-hour rows are accepted.

Error message words (each differs from what the fallback guard would print, so a missing guard fails its own case): not a dict `"dict"`; missing key `"missing key"`; wrong symbol `"symbol"`; `fundingTime` type `"JSON integer"`; before start `"before"`; after end `"after"`; `rateType` `"Regular or Special"`; decreasing `"ascending"`; repeated key `"repeated"`; non-str rate or price `"decimal string"`; bad grammar `"grammar"`; signed price `"sign"`; row count `"more than"`.

- [ ] **Step 1: Write failing tests** with `ROW = {"symbol": "BTCUSDT", "fundingTime": 1793736000003, "fundingRate": "0.00010000", "markPrice": "67123.45000000", "rateType": "Regular"}`, `market_id="BTCUSDT"`, `symbol="BTC/USDT:USDT"`, range `[1793700000000, 1793750000000]`:
  - `test_parses_a_row`: key `("binance-usdm", "BTC/USDT:USDT", 1793736000003, RateType.REGULAR)`, rate `"0.0001"`, price `"67123.45"`; an extra unknown key gives the same result.
  - `test_negative_zero_and_empty_mark_price`: `"-0.00010000"` → `"-0.0001"`; `"-0.00000000"` → `"0"`; `"0.00000000"` → `"0"`; `markPrice ""` → `mark_price is None`; a Special row 1 002 ms after a Regular one of the same hour → two rows with different keys.
  - `test_empty_response_is_valid`: `[]` → `()`.
  - `test_inclusive_bounds`: rows at `start` and at `end` accepted.
  - `test_off_hour_row_is_accepted`: `fundingTime` 30 minutes past the hour parses and `is_off_hour(...)` is `True` for it, `count_off_hour_rows` of the result is 1; a timestamp in seconds (`1793736000`) is rejected by the range rule (message contains `"before"`).
  - `test_coincident_regular_and_special_pair_is_accepted_in_either_order`: same `fundingTime`, Regular then Special → both rows; Special then Regular → both rows; the pair as the last two of a `limit`-row response is accepted in both orders; a third row at the same `fundingTime` can only be a repeated key.
  - `test_malformed_response_is_rejected_entirely`, parametrized (ids name the defect), asserting `FundingRateParseError`, `.row_index` and the message word from the list above. Each case trips **only its own guard** (the bad row stays inside the range and after the previous row unless that is the defect): at index 0: row before `start` (`"before"`); `fundingTime` as `str`, `float`, `bool` (`"JSON integer"`); wrong `symbol` (`"symbol"`); `rateType` `"regular"`, `"Other"`, `None` (`"Regular or Special"`); `fundingRate` as `float` (`0.0001`), `int`, `None` (`"decimal string"`) and `""`, `"abc"`, `" 0.1"` (`"grammar"`); `markPrice` as `float`, `int`, `None` (`"decimal string"`), `"-1"`, `"+1"` (`"sign"`) and `"abc"` (`"grammar"`); each of the five keys missing, one case per key (`"missing key"`); row as `list` or `str` (`"dict"`). At index 1 after a valid first row at `T`: row after `end` (`"after"`); `fundingTime` below `T` (`"ascending"`); identical row repeated (`"repeated"`); same key with a different `fundingRate` (`"repeated"`: the response is rejected, it is not a conflict); same `fundingTime` as `T` with `rateType` `None` and with `"Other"` (`"Regular or Special"`, proving `rateType` is checked before the order).
  - `test_non_list_response_is_rejected`: `{}`, `None`, `"[]"` and the E-4 error object `{"status": "ERROR", "type": "GENERAL", "code": "99099990", "errorData": "illegal params."}` → `FundingRateParseError` with `row_index is None`.
  - `test_row_count_is_checked_before_rows`: `limit = 2`, three rows whose row 0 is malformed → `row_index is None` and message contains `"more than"`; at the default limit 1 001 valid rows → rejected the same way.
  - `test_limit_rows_are_accepted`: exactly `limit` valid rows (limit 3 and 1 000) are accepted.
  - `test_argument_errors_are_not_parse_errors`: `start > end`, `market_id=""`, `symbol=""`, `untyped(True)` as `start_time_ms`, `limit` 0, 1 001 and `untyped(True)` → `ValueError`/`TypeError` and `type(err) is not FundingRateParseError`.
- [ ] **Step 2: Run** `uv run pytest tests/collector/test_binance_funding_rate.py -v`. Expect: FAIL.
- [ ] **Step 3: Implement** with a private row helper; module docstring cites P-3, P-5, states that the request side (FR-3) never sends a request without `startTime` and this parser only validates the answer, and that hour alignment is a heuristic that is never a rejection.
- [ ] **Step 4: Run** the tests, then `make check` (also checks the import-linter contracts). Expect: PASS; record output.
- [ ] **Step 5: Commit** `feat(collector): strict all-or-nothing funding rate history parser (ADR-0004 P-3, P-5)`.

### Task 6: Invariant register

**Files:**
- Modify: `docs/invariants.md` (two rows after INV-099)

- [ ] **Step 1:** Add, in the existing column style (copy INV-071/072's structure; no literal pipe characters in cells):
  - `INV-100` — Invariant: "A funding rate row is selected as eligible only when its settlement time plus the publication delay is at or before the trusted server-time lower bound, and untrusted time selects nothing"; status `ENFORCED`; Mechanism: `is_funding_rate_eligible`, `forward_end_bound_ms` and `select_eligible_funding_rates` in `domain/funding_rate.py` (ADR-0004 P-4; `make test`). Scope: the selection rule only, nothing persists funding rates yet (FR-2, FR-4), and the 30 s delay is a margin from one settlement of three symbols, not a proven bound; Evidence cell: only `tests/domain/test_funding_rate.py::test_boundary_is_eligible_and_one_millisecond_earlier_is_not`, `tests/domain/test_funding_rate.py::test_forward_end_bound_makes_every_row_up_to_it_eligible`, `tests/domain/test_funding_rate.py::test_untrusted_time_selects_nothing` (no prose in the Evidence cell: `_evidence_errors` rejects it).
  - `INV-101` — Invariant: "The Binance funding rate history parser rejects a malformed response entirely: not a list (the HTTP-200 error object included), more rows than the limit, a row outside the requested range, a repeated key or a decreasing `fundingTime`, a wrong symbol, an unknown `rateType`, a malformed rate or mark price"; status `ENFORCED`; Mechanism: `parse_funding_rate_history` in `collector/binance_funding_rate.py` raises `FundingRateParseError` and returns no row (ADR-0004 P-3, P-5; `make test`). It does not check that the request followed the request rules (FR-3), and hour alignment is never a rejection; Evidence cell: only `tests/collector/test_binance_funding_rate.py::test_malformed_response_is_rejected_entirely`, `tests/collector/test_binance_funding_rate.py::test_non_list_response_is_rejected`, `tests/collector/test_binance_funding_rate.py::test_row_count_is_checked_before_rows`.
- [ ] **Step 2: Run** `uv run pytest tests/test_invariant_register.py -v`, then `make check`. Expect: PASS; record output.
- [ ] **Step 3: Commit** `docs: invariant register rows for funding rate eligibility and parser (INV-100, INV-101)`.

### Task 7: Pull request

- [ ] **Step 1:** `make check` on the branch head; record output.
- [ ] **Step 2:** Independent implementation **code review** (read-only Opus agent or the `code-review` skill on Opus, against ADR-0004 and the branch diff). Apply every finding, re-run `make check`, record what the review found and what was changed (in the PR body and in this plan's Review history).
- [ ] **Step 3:** Commit this plan with its updated Review history (`docs: FR-1 plan, reviewed`) if not committed yet.
- [ ] **Step 4:** Push `fr-1-funding-rate-domain` and open a PR against `main` titled `FR-1: funding rate domain, span arithmetic, capacity and raw parser`, body written as the squash commit message: summary per task, the "Decisions this plan makes" list, verification output, `Closes #16`, ending with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

## Review history

- 2026-11-03, independent plan review (Opus, read-only, against ADR-0004 and the code): every number and formula verified (K, Q, `S_sustained`, thresholds, shares, offsets, page coverage, chunk counts). Applied: the coincident-pair ordering became a user decision (non-decreasing `fundingTime`, unique key, either order of a Regular/Special pair) (M1); `rateType` is validated before the order check (M2); the "does not advance" guard has its own message and a `limit = 2` test, and the plan no longer claims P-5 proves it unreachable (M3); every parser case now names its message word (M4); history-chunk step counts are stated for a seed width of 0 and 24 h (M5); accept/refuse at the boundary for every budget and offset, plus a window-arithmetic test independent of the formula (M6); explicit moving-target tests and a random walk with crash, restart and `backfill_days` changes (M7); implementation code review and plan commit added to Task 7 (M8). Nice-to-have applied: `count_off_hour_rows` and the inclusive 5 000 ms boundary (1), `MIN_TIMESTAMP_MS` edge and `through_ms` validation (2), duplicate parser cases replaced (3), `mark_price` wording (4), stricter `markPrice` sign and exponent decisions stated (5), exact 2 500-row assertions and the 1 001-row case (6), empty answer extends the span (7), invariant cells split into Mechanism prose and an Evidence cell of test paths only (8), `S` renamed to `X` (10). Not applied: the commit trailer stays the one the session's attribution gives (9); `isolated_round_capacity` is kept because FR-4 tests why it was replaced (10).
- 2026-11-03, independent implementation code review (Opus, read-only, `git diff main...HEAD`; reran the new tests, mypy, ruff, import-linter: all clean): no MUST FIX defect; every boundary and number matched ADR-0004. Applied: `sustained_capacity` docstring no longer claims a no-skip guarantee, it is an ideal-run lower bound (1); `_RATE_TYPES` module-level dict removed in favour of `RateType(raw_type)` after an exact-value check, with a list-valued `rateType` case added (3); `is_behind` docstring says the API must reuse it so P-10 and P-13 agree to the millisecond (4); the "every share" capacity test now uses a hand-written `Q` table instead of the code under test, `[error object]` inside a list, `end_time_ms=True`, `warning_threshold(True)`, `history_target_from_ms(True, ...)` cases added, the redundant `funding_rate` type guard removed (`canonical_decimal` raises the same `TypeError`) (5); `warning_threshold` rejects a negative value, `planned_start_offset_ms` says it is an ideal-run plan, the `CapacityError` message names `S_sustained` (6). Not changed in code: the ordering decision is not yet written in ADR-0004 P-5 (finding 2); `docs/adr/` is not edited without the user's approval, so it is stated in the PR body and offered as an erratum.
