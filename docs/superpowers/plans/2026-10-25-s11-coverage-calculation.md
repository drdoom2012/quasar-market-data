# S11 Coverage Calculation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pure domain function that, for a span of slots, stored facts and a given `as_of`, gives every slot exactly one state (`present`, `conflicted`, `unavailable`, `missing`, `not_yet_expected`, `due`, `stale`), the counts of each, and the runs of consecutive non-present slots. It is the logic `GET /v1/coverage` (S12), the scheduler (S13) and the indicators (S18) will share.

**Architecture:** S10's `domain/span_coverage.py` already decides the five clock-free states (contract section 4, ADR-0002 P-7). S11 keeps that as the single source of truth for precedence by extracting its per-slot loop into a generator, then adds a new module `domain/timed_coverage.py` that splits the one clock-dependent state (`beyond_stored`) by `as_of`, the publication delay and the lag threshold. No I/O, no clock read, no endpoint, no schema or contract change.

**Tech Stack:** Python 3.12 stdlib only; pytest, mypy strict, ruff, import-linter. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (§8 S11, §11.4), `docs/adr/0002-sqlite-schema-v1.md` (P-7 item 5), `docs/contracts/api-v1.md` (section 4; section 6 reserves `/v1/coverage` for S12), `docs/invariants.md` (INV-009, INV-010, INV-011). Earlier plan: `2026-10-25-s10-get-v1-candles.md`.

## Global Constraints

- The domain never reads a clock; `as_of_ms` is an `int` supplied by the caller (epoch milliseconds, `domain/server_time.py` convention). Domain imports nothing from `collector`, `api`, `storage`, ccxt, fastapi, talib or `sqlite3` (enforced by `tests/test_domain_purity.py` and import-linter).
- Never fabricate: a slot with no stored fact is never reported `present`; stored facts always win over the clock.
- Fail closed: wrong types, out-of-range or misaligned inputs raise; they never produce a count. `classify_span_at` validates with the project's `ensure_timestamp_ms` (`domain/timestamps.py`: plain `int`, within [2000, 2100]); `bool` and `float` are rejected.
- The domain does not bound the span; the caller does (S10 caps a window at `MAX_WINDOW_SLOTS = 10_000`, S12 must do the same). Cost and `runs` size are linear in the slots.
- The S10 behavior of `classify_span` and the contract's `coverage` block must not change (`tests/domain/test_span_coverage.py` and `tests/api` stay green untouched).
- Do not modify public contracts or the schema; S11 adds no endpoint and does not edit `docs/contracts/api-v1.md`.
- `make check` is the gate for every commit. Do not invent other commands.

## Decisions made in this plan (1 and 2 approved by the user on 2026-10-25; the rest open to review)

Plan review of 2026-10-25 (independent): no P1; six P2 and several P3 findings are worked into this text (clock behind the data, validation, `GapRun` ordering, register evidence and targets, approval wording). A second round (the user's findings) unified the `as_of_ms` validation on `ensure_timestamp_ms`, added type and range checks of the set members and the candle-newer-than-newest check to `classify_span_at` only, renamed `clock_behind_stored` to `stored_ahead_of_as_of` with an exact meaning, and removed the stale "needs approval" wording.

1. **A third clock state `due` (approved).** Spec §11.4 names "not yet expected until boundary + publication delay" and "stale if still absent after the ingestion-lag threshold" but leaves the interval between them without a name. With `expected_at = slot_end + publication_delay` and `L = lag_threshold_ms`: `as_of < expected_at` → `not_yet_expected`; `expected_at <= as_of <= expected_at + L` → `due` (expected, tolerated as ingestion lag, not a fault yet); `as_of > expected_at + L` → `stale`. Alternative: fold `due` into `not_yet_expected`, which would call a candle "not yet expected" after the system already expects it, or into `stale`, which contradicts "after the threshold". The boundaries mirror `is_closed_eligible` (`expected_at <= as_of` is eligible) and use a strict `>` for stale ("after"). Approved by the user on 2026-10-25. The S12 contract section must list `due` (contract §6 names only stale and not-yet-expected today).
2. **Default lag threshold 60 000 ms** (approved; spec §11.4 "suggested default 60 s"), a constant `DEFAULT_LAG_THRESHOLD_MS`, overridable per call; the publication delay has no default here (the caller passes `DEFAULT_PUBLICATION_DELAY_MS` from `closed_candles.py` or its configured value). It is the project's first configurable staleness value; configuration plumbing (CLI/env) is S12/S13, not here. A very large `lag_threshold_ms` would keep every slot `due` and never `stale`; the domain only requires a non-negative `int`, so S12/S13 configuration must bound it.
3. **Only `beyond_stored` is refined by the clock.** `missing` (a hole before the newest stored candle) stays `missing` regardless of `as_of`; a recorded `unavailable` or `conflicted` slot keeps that state even after the newest candle. A stored candle is `present` even if `as_of` is earlier than its end plus delay: stored data wins over the reader's clock and nothing is raised (raising would turn clock skew into a `500`). `TimedCoverage.stored_ahead_of_as_of` is `True` exactly when `newest_open_time_ms + timeframe_ms + publication_delay_ms > as_of_ms`, i.e. when the newest stored candle would not yet be closed-eligible under this `as_of` and this delay. It is a statement about the inputs, not a diagnosis: the cause may be a reader clock behind the Collector's trusted time, a deliberately historical `as_of`, or a publication delay larger than the Collector used. It never changes a classification and does not claim that any `beyond_stored` slot is really `stale`; it tells S12 that the clock-derived states of the slots after the newest candle were computed against an `as_of` earlier than the stored data's own eligibility time, and S12 decides how to surface it.
4. **Precedence lives once.** `span_slot_states` (new, in `span_coverage.py`) is the only place that implements the order of contract section 4; `classify_span` and `classify_span_at` both consume it.
5. **Runs are included.** `TimedCoverage.runs` lists consecutive slots of the same non-present state as `(state, start_ms, end_ms)`; S12 would otherwise have to put grouping logic in the API layer. `present` slots are never listed.
6. **Unknown series is the caller's problem.** `newest_open_time_ms` is required, as in `classify_span`; a series with no stored candle is `UnknownSeries` in S10 and S11 does not widen that.

## Review Focus

Each line has its test in the named task.

1. `as_of` exactly on `expected_at` and on `expected_at + L`, and one millisecond either side (Task 2).
2. A series that stopped long ago: every slot after the newest candle is `stale`, and a window entirely in the future of `as_of` is all `not_yet_expected` (Task 2).
3. `unavailable` recorded after the newest candle stays `unavailable`, never `stale` (Task 2).
4. `bool`, `float` (also an aligned one), out-of-range, misaligned or out-of-span inputs, set members of the wrong type, a `candles` member newer than `newest_open_time_ms`, a `newest_open_time_ms` inside the span without a candle, and a negative delay or lag raise instead of counting (Task 2; Task 1 is a pure refactor and keeps the S10 validation as it is).
5. An `as_of` earlier than the stored data's eligibility time (a lagging clock, a historical `as_of`, or a different delay): the candle is still `present`, classification is unchanged and `stored_ahead_of_as_of` is `True`, so a reader can tell the clock-derived states were computed against such an `as_of` (Task 2).

---

## File Structure

- Modify: `src/quasar_market_data/domain/span_coverage.py` — add `span_slot_states`, `StoredState`; `classify_span` consumes it.
- Create: `src/quasar_market_data/domain/timed_coverage.py` — `SlotState`, `ExpectationRule`, `GapRun`, `TimedCoverage`, `classify_span_at`.
- Modify: `tests/domain/test_span_coverage.py` — tests for `span_slot_states`.
- Create: `tests/domain/test_timed_coverage.py`.
- Modify: `docs/invariants.md`, `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (§11 resolution line), `docs/follow-ups.md` only if a new open item appears.

### Task 1: Extract the per-slot precedence

**Files:** Modify `src/quasar_market_data/domain/span_coverage.py`, `tests/domain/test_span_coverage.py`.

**Interfaces:**
- Produces:
  - `StoredState = Literal["present", "conflicted", "unavailable", "missing", "beyond_stored"]`
  - `span_slot_states(*, start: int, end: int, timeframe_ms: int, candles: AbstractSet[int], conflicts: AbstractSet[int], unavailable: AbstractSet[int], newest_open_time_ms: int) -> Iterator[tuple[int, StoredState]]` — validates exactly as `classify_span` does today and nothing more (alignment, non-empty span, members aligned and inside the span; `ValueError` before the first item, so it is a plain function that validates and returns an inner generator). Type and range checks are added only in `classify_span_at` (Task 2); Task 1 is a pure refactor, then yields `(open_time_ms, state)` for every slot of `[start, end)` in ascending order.
- `classify_span` keeps its signature and result.

- [ ] **Step 1: Write failing tests** in `tests/domain/test_span_coverage.py`: `test_span_slot_states_yields_every_slot_in_order` (6 slots, candles `{slot(0), slot(4)}`, conflicts `{slot(1)}`, unavailable `{slot(2)}`, newest `slot(4)` → `[(slot(0), "present"), (slot(1), "conflicted"), (slot(2), "unavailable"), (slot(3), "missing"), (slot(4), "present"), (slot(5), "beyond_stored")]`); `test_span_slot_states_validates_before_the_first_item` (misaligned `start`, empty span, element outside the span: `pytest.raises(ValueError)` at the call, not at the first `next`).
- [ ] **Step 2: Run** `uv run pytest tests/domain/test_span_coverage.py -v`. Expected: FAIL (`ImportError` for `span_slot_states`).
- [ ] **Step 3: Implement** `span_slot_states` by moving the validation block and the per-slot `if` chain out of `classify_span`; `classify_span` becomes a counting loop over the generator and builds `conflicted_open_times` from it. The order and conditions are unchanged.
- [ ] **Step 4: Run** `make check`. Expected: PASS, with every pre-existing S10 test (`tests/domain/test_span_coverage.py`, `tests/api`) unchanged and green; they, not a new equivalence test, are the guard that `classify_span` behaves as before.
- [ ] **Step 5: Commit** `refactor(domain): one place for the slot-state precedence (S11)`.

### Task 2: `classify_span_at` counts and the clock boundaries

**Files:** Create `src/quasar_market_data/domain/timed_coverage.py`, `tests/domain/test_timed_coverage.py`.

**Interfaces:**
- Consumes: `span_slot_states`, `StoredState` (Task 1).
- Produces:
  - `DEFAULT_LAG_THRESHOLD_MS: Final = 60_000`
  - `class SlotState(StrEnum)`: `PRESENT = "present"`, `CONFLICTED = "conflicted"`, `UNAVAILABLE = "unavailable"`, `MISSING = "missing"`, `NOT_YET_EXPECTED = "not_yet_expected"`, `DUE = "due"`, `STALE = "stale"`.
  - `@dataclass(frozen=True) class ExpectationRule`: `publication_delay_ms: int`, `lag_threshold_ms: int = DEFAULT_LAG_THRESHOLD_MS`; `__post_init__` raises `TypeError` for a non-`int` (including `bool`) and `ValueError` for a negative value, same wording style as `closed_candles._ensure_delay` (which is private; do not import it).
  - `@dataclass(frozen=True) class GapRun`: `state: SlotState`, `start_ms: int`, `end_ms: int` (exclusive); declared here so that `TimedCoverage` type-checks, filled by Task 3.
  - `@dataclass(frozen=True) class TimedCoverage`: `as_of_ms: int`, `rule: ExpectationRule` (S12 must state `as_of` and the lag threshold, contract §6), `slots`, `present`, `conflicted`, `unavailable`, `missing`, `not_yet_expected`, `due`, `stale` (all `int`), `stored_ahead_of_as_of: bool`, `runs: tuple[GapRun, ...]` (always `()` until Task 3), property `fully_present -> bool` (`present == slots`).
  - `classify_span_at(*, start: int, end: int, timeframe_ms: int, candles: AbstractSet[int], conflicts: AbstractSet[int], unavailable: AbstractSet[int], newest_open_time_ms: int, as_of_ms: int, rule: ExpectationRule) -> TimedCoverage`.

- [ ] **Step 1: Write failing tests** (`TF = 300_000`, `S` aligned, `rule = ExpectationRule(15_000, 60_000)`, one slot after a newest candle so `expected_at = slot_end + 15_000`):
  - `test_clock_boundaries_of_a_slot_after_the_newest_candle` parametrised over `as_of = expected_at - 1 → not_yet_expected`, `expected_at → due`, `expected_at + 60_000 → due`, `expected_at + 60_001 → stale`.
  - `test_a_series_that_stopped_long_ago_is_stale_after_its_newest_candle` (newest candle at `slot(0)`, `as_of` a day later, span of 4 slots: `present == 1`, `stale == 3`, `not_yet_expected == due == 0`).
  - `test_a_window_in_the_future_of_as_of_is_all_not_yet_expected` (no candles, `newest` before the span, `as_of` before the first `expected_at`).
  - `test_unavailable_after_the_newest_candle_is_not_stale`; `test_missing_before_the_newest_candle_ignores_the_clock` (same counts for `as_of = S` and for `as_of = S + 86_400_000`; both inside the valid timestamp range).
  - `test_a_candle_is_present_even_when_as_of_is_before_its_eligibility` (`as_of` before the candle's end plus delay: `present == 1`, no exception, `stored_ahead_of_as_of is True`); `test_stored_ahead_of_as_of_boundary` (`False` at `as_of == newest_end + delay`, `True` one millisecond earlier); `test_stored_ahead_of_as_of_does_not_change_the_classification` (a historical `as_of`, and the same `as_of` with `publication_delay_ms` 0 versus 15 000: the flag differs, `False` versus `True`, while the counts equal those computed independently from decision 1 for each rule, and the flag is not an input of any state).
  - `test_zero_lag_threshold`: `lag_threshold_ms = 0` → `due` only at `as_of == expected_at`, `stale` one millisecond later.
  - `test_counts_always_sum_to_slots_and_refine_classify_span` (seeded `random.Random(12)`, 300 cases with random sets, `as_of` and rule; `newest` is the largest candle open time or a time before the span: `slots == sum of the seven counts`; `present`, `conflicted`, `unavailable`, `missing` equal those of `classify_span`; `not_yet_expected + due + stale == beyond_stored`).
  - Validation, parametrised with the exact exception per case (never a `(TypeError, ValueError)` tuple):
    - `TypeError`: `as_of_ms`, `start`, `end`, `newest_open_time_ms` or `timeframe_ms` being `True` or `1.5`; `as_of_ms` being the aligned float `float(S)`; a member of `candles`, of `conflicts` and of `unavailable` (one case each) being `True`, `1.5` or the aligned float `float(slot(1))`; `ExpectationRule(True)`, `ExpectationRule(0, True)`.
    - `ValueError`: `as_of_ms` below `MIN_TIMESTAMP_MS` and above `MAX_TIMESTAMP_MS` (so `0` and `-1` are rejected by the range, not by a separate `>= 0` check); `start` or `newest_open_time_ms` out of range; a member of each set out of the timestamp range; `ExpectationRule(-1)`, `ExpectationRule(0, -1)`; a misaligned span; a member outside `[start, end)` or misaligned; `end` above `MAX_TIMESTAMP_MS + timeframe_ms`; a `newest_open_time_ms` inside the span that is not in `candles`; a `candles` member newer than `newest_open_time_ms`.
    - No error: `conflicts` or `unavailable` members after `newest_open_time_ms` (`test_unavailable_after_the_newest_candle_is_not_stale` and a conflict twin).
    - `span_slot_states` and `classify_span` (Task 1) are not given these type and range checks; `test_span_coverage.py` is untouched by them.
- [ ] **Step 2: Run** `uv run pytest tests/domain/test_timed_coverage.py -v`. Expected: FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** the module. `classify_span_at` calls `ensure_timestamp_ms` (exact `int`, `bool` and `float` rejected, within `MIN_TIMESTAMP_MS`..`MAX_TIMESTAMP_MS`) for `as_of_ms`, `start`, `newest_open_time_ms` and for every member of `candles`, `conflicts` and `unavailable` (before anything else touches them, so an aligned `float` such as `1792643400000.0`, which passes the `%` alignment test in `span_slot_states`, is rejected here), `type(...) is int` for `end` and `timeframe_ms`, `end <= MAX_TIMESTAMP_MS + timeframe_ms` (the exclusive end of the last valid slot; this also settles for the domain what follow-up F-1 notes for the API), the newest-in-span check, a `ValueError` for any `candles` member newer than `newest_open_time_ms` (`conflicts` and `unavailable` may lie after it), then loops over `span_slot_states`; for `beyond_stored` it computes `expected_at = open_time + timeframe_ms + rule.publication_delay_ms` and applies decision 1. Map the four other stored states one to one.
- [ ] **Step 4: Run** `make check`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(domain): classify a span at as_of with not_yet_expected, due and stale (S11)`.

### Task 3: Runs of non-present slots

**Files:** Modify `src/quasar_market_data/domain/timed_coverage.py`, `tests/domain/test_timed_coverage.py`.

**Interfaces:**
- Consumes: `GapRun` (declared in Task 2; no slot-count property, the caller divides by the timeframe). Produces: filled `TimedCoverage.runs`. Invariants of a run: `start_ms < end_ms`, never `SlotState.PRESENT`, adjacent runs differ in state or are separated by a present slot. `TimedCoverage.runs` is ordered by `start_ms`.

- [ ] **Step 1: Write the tests.** Failing: `test_adjacent_slots_of_one_state_merge_into_one_run` (3 missing in a row between two present → one `GapRun(MISSING, slot(1), slot(4))`); `test_a_state_change_starts_a_new_run` (missing then unavailable, no present between → two runs, contiguous); `test_runs_cover_exactly_the_non_present_slots` (seeded `random.Random(13)`, 200 cases: summing `(end_ms - start_ms) // TF` per state equals the counts, runs sorted, non-overlapping and maximal, i.e. no two contiguous runs share a state). Guards that already pass against `runs=()` and must stay green: `test_present_slots_are_not_listed`, `test_a_fully_present_span_has_no_runs`.
- [ ] **Step 2: Run** the new tests. Expected: the three failing tests FAIL with `AssertionError` (`runs` is `()`), the two guards pass.
- [ ] **Step 3: Implement** run building in the same loop of `classify_span_at`.
- [ ] **Step 4: Run** `make check`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(domain): list the runs of non-present slots (S11)`.

### Task 4: Register and documentation

**Files:** Modify `docs/invariants.md`, `docs/superpowers/specs/2026-10-20-market-data-v1-design.md`; check `tests/test_invariant_register.py` for the row format rules before editing.

- [ ] **Step 1:** Add `INV-045` (`ENFORCED`): every slot of a span has exactly one state; states are derived only from stored facts and the caller's `as_of`; stored facts win over the clock; the counts always sum to `slots`; a slot without a stored fact is never `present`. Evidence (each reference in the full `tests/...py::name` form that `tests/test_invariant_register.py` requires): `tests/domain/test_timed_coverage.py::test_counts_always_sum_to_slots_and_refine_classify_span`, `tests/domain/test_timed_coverage.py::test_a_candle_is_present_even_when_as_of_is_behind_it`, `tests/domain/test_timed_coverage.py::test_runs_cover_exactly_the_non_present_slots`, and for "no clock read" `tests/test_domain_purity.py::test_domain_package_has_no_violations`.
- [ ] **Step 2:** Update `INV-009`, `INV-010` and `INV-011`: mechanism text says the classification exists in the domain (INV-045); the targets that said "S11" become "S12 (exposed to readers per slot), S18 (indicators)"; INV-009's sentence "reporting it as conflicted to readers is S11" becomes "S10 counts it in the `coverage` block and flags the candle; per-slot listing is S12". Statuses do not change. Do not touch any other row.
- [ ] **Step 3:** In spec §11.4 append one sentence: S11 fixed the semantics as in this plan's decisions 1 and 2 (state `due`, strict `>` for stale, default 60 000 ms), approved by the user on 2026-10-25.
- [ ] **Step 4: Run** `make check`. Expected: PASS, including `tests/test_invariant_register.py`.
- [ ] **Step 5: Commit** `docs: register INV-045 and fix the S11 coverage semantics`.

## Self-review

- **Spec coverage:** S11 "pure; present / missing / stale / not-yet-expected for a given `as_of`" → Tasks 2, 3; conflicted and unavailable (spec §11.3–11.4, ADR-0002 P-7) → Task 1 reuse, checked by the refinement property; "exact semantics fixed in the S11 plan" → Decisions 1–2.
- **Not in scope:** `GET /v1/coverage` and its contract section (S12), the source of `as_of` and configuration of the delay and lag (S12/S13), reading the database, indicator refusal (S18), any schema, ADR or S10 contract change.
- **Types:** `StoredState` and `span_slot_states` (Task 1) are consumed unchanged in Task 2; `GapRun` and `TimedCoverage.runs` are declared in Task 2 (`runs` is `()` until Task 3 fills it).
