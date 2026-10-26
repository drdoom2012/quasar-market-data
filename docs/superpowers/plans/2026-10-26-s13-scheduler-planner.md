# S13 Scheduler Planner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pure domain function that, for a timeframe, a publication delay and a time `now_ms`, says which candle slot is the newest one that is expected and when the collector should next wake up to look for the following one.

**Architecture:** One new module `domain/schedule.py` with integer arithmetic only, built on `Timeframe` and `ensure_timestamp_ms`. It uses the same expectation instant as `is_closed_eligible` (ADR-0001 P-3) and `timed_coverage` (S11): `expected_at = slot_end + publication_delay`. Valid slots start at `MIN_TIMESTAMP_MS` (the first valid open time), so the planner has an explicit start-of-range regime. The domain never reads a clock; the `Clock` protocol stays in `collector/fetch_once.py` and S14 passes `clock.wall_ms()` in. No I/O, no loop, no sleeping, no concurrency, no schema or contract change.

**Tech Stack:** Python 3.12 stdlib only; pytest, mypy strict, ruff, import-linter. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (§8 S13, D5, §11.2), `docs/architecture.md` (Scheduler, section 3), `docs/adr/0001-binance-ingestion-fidelity-and-closed-candles.md` (P-3, P-4, P-6). Earlier plans: `2026-10-22-s5-closed-candle-selection-and-parser.md`, `2026-10-25-s11-coverage-calculation.md`.

## Facts checked in the repository (2026-10-26)

- `MIN_TIMESTAMP_MS = 946_684_800_000` (2000-01-01T00:00:00Z), `MAX_TIMESTAMP_MS = 4_102_444_800_000` (`domain/timestamps.py`). **`MIN_TIMESTAMP_MS` is not 0.**
- `Timeframe` aligns on `ts % milliseconds == 0` (epoch alignment); every supported interval divides one UTC day. `MIN_TIMESTAMP_MS % ms == 0` was computed for `1m`, `5m`, `1h` and `1d` and is `0` for each, and `MAX_TIMESTAMP_MS % 86_400_000 == 0`. Therefore **the first valid aligned open time is `MIN_TIMESTAMP_MS` for every supported timeframe** (the other labels divide a day too, so the same holds; the implementer should assert it in a test for all twelve labels).
- `classify_span_at` is keyword-only: `start, end, timeframe_ms, candles, conflicts, unavailable, newest_open_time_ms, as_of_ms, rule`. `candles`, `conflicts` and `unavailable` members must lie inside `[start, end)` (`span_slot_states` raises otherwise); `newest_open_time_ms` may lie before the span without being a member (the existing tests in `tests/domain/test_timed_coverage.py` do this with `candles=set()`, `newest_open_time_ms=slot(0)`, window `slot(1)` to `slot(2)`). A slot after `newest_open_time_ms` is `not_yet_expected` iff `as_of_ms < slot_open + timeframe_ms + publication_delay_ms`, `due` up to `+ lag_threshold_ms` inclusive (default 60 000), then `stale`.
- `ExpectationRule` and `_ensure_delay` accept any non-negative `int` delay; there is no upper bound in the existing contract, so S13 adds none. Only `MAX_TIMESTAMP_MS` limits the resulting times.
- Not verified: nothing in this plan has been run; the planned tests and formulas were checked by hand against the code only. The CI workflow has not been observed running (`docs/invariants.md`).

## Global Constraints

- The domain never reads a clock; every time is an `int` of epoch milliseconds supplied by the caller. Domain imports nothing from `collector`, `api`, `storage`, ccxt, fastapi, talib or `sqlite3` (`tests/test_domain_purity.py`, import-linter).
- UTC boundaries only: slots are epoch-aligned through `Timeframe`. No `datetime`, no timezone handling.
- Fail closed: wrong types, out-of-range times and negative delays raise (`ensure_timestamp_ms`, same checks as `ExpectationRule`); `bool` and `float` are rejected. Nothing is clamped or guessed. No upper limit on `publication_delay_ms` is introduced.
- The planner only plans. It does not decide that a candle is closed: that remains `select_closed_candles` against the trusted server lower bound (ADR-0001 P-3, P-4). A wake-up never makes a candle eligible.
- Do not modify public contracts, the schema or any ADR; do not change S5 or S11 behavior (their tests stay green untouched; the S11 test for `timed_coverage` is written to fit S11, never the reverse).
- Verification commands: `make check` (lint, typecheck, import contracts, tests) is the gate before **every** commit. During a task, targeted runs with `uv run pytest <path>` are allowed; that is the command behind the Makefile's `test` target. No other commands are invented.

## Decisions made in this plan (open to review)

1. **Pure planner in the domain; the `Clock` stays where it is.** The roadmap says "using the injected `Clock`". The `Clock` protocol (`monotonic_ns`, `wall_ms`) lives in `collector/fetch_once.py` and the domain cannot import it. The planner takes `now_ms: int`, and S14 calls it with `clock.wall_ms()`. Moving `Clock` into its own collector module is S14's refactor, when a second consumer exists.
2. **Wake time is the smallest `expected_at` of a valid slot strictly greater than `now`.** `expected_at = slot_end + publication_delay`; valid slots are the aligned open times in `[MIN_TIMESTAMP_MS, MAX_TIMESTAMP_MS]`. Strictly greater, so a caller that wakes exactly on the instant and asks again gets the next boundary and cannot spin. Before the first expected candle (`now < MIN_TIMESTAMP_MS + timeframe + delay`) the answer is that first `expected_at`, not an earlier boundary that belongs to no valid slot.
3. **One timeframe per call.** A caller with several timeframes takes the minimum of the wake times. Staggering, jitter and rate limits belong to S14.
4. **Waking on time is not being eligible, and pending needs its own retry.** The trusted lower bound deliberately lags the wall clock (uncertainty, drift, precision margin of ADR-0001 P-4), so the first look at a fresh slot can find it still pending. A pending result therefore requires a separate, bounded retry in S14; the next regular boundary wake of this planner does not replace it (it can be a whole timeframe away). The S11 tolerance window (`due`, default 60 s) only classifies lateness for readers; it schedules nothing. S13 implements no retry mechanism; the planner's module docstring states this.
5. **No gap logic.** Which slots are missing in the database, backfill and catch-up after downtime are S15/S16. `newest_expected_open_ms` names only the newest expected slot.

## Review Focus

Each line has its test in the named task.

1. Start of range: `now = MIN_TIMESTAMP_MS` with a delay larger than the timeframe (`1m`, 90 000 ms) gives no expected slot and wakes at `MIN + 150 000`, not at an earlier instant that belongs to no valid slot (Task 1).
2. `now` exactly on the first and on later `expected_at` and one millisecond either side, for delay 0, smaller than, equal to and larger than the timeframe (Task 1).
3. A first `expected_at` that already passes `MAX_TIMESTAMP_MS`: no slot, and `next_wake_ms` and `plan_collection` raise (Task 1).
4. Agreement with the existing rules: for random `now_ms` a slot is expected by the planner exactly when `is_closed_eligible` is true with `lower_bound_ms = now_ms`, and for fixed concrete windows exactly when `classify_span_at` stops calling the slot `not_yet_expected` (Task 2).
5. Invalid input: `bool`, `float` (also an aligned one), out-of-range times and negative delay raise with the exact exception, never a plan; `now_ms = MIN_TIMESTAMP_MS - 1` is `ValueError` (Task 1).

---

## File Structure

- Create: `src/quasar_market_data/domain/schedule.py` — `expected_at_ms`, `newest_expected_open_ms`, `next_wake_ms`, `CollectionPlan`, `plan_collection`.
- Create: `tests/domain/test_schedule.py`.
- Modify: `docs/invariants.md` (new row INV-047), `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (§11 item 5).

### Task 1: Boundary and wake-time planner

**Files:** Create `src/quasar_market_data/domain/schedule.py`, `tests/domain/test_schedule.py`.

**Interfaces:**
- Consumes: `Timeframe` (`milliseconds`, `end_exclusive_ms`) from `domain/timeframe.py`; `ensure_timestamp_ms`, `MIN_TIMESTAMP_MS`, `MAX_TIMESTAMP_MS` from `domain/timestamps.py`.
- Produces (all positional, in the order `(time, timeframe, publication_delay_ms)`; the time is `open_time_ms` for `expected_at_ms` and `now_ms` for the others; a `timeframe` that is not a `Timeframe` raises `TypeError`):
  - `expected_at_ms(open_time_ms: int, timeframe: Timeframe, publication_delay_ms: int) -> int` — `ensure_timestamp_ms(timeframe.end_exclusive_ms(open_time_ms) + publication_delay_ms, "expected_at_ms")`; `ValueError` when the sum passes `MAX_TIMESTAMP_MS`.
  - `newest_expected_open_ms(now_ms: int, timeframe: Timeframe, publication_delay_ms: int) -> int | None` — the largest valid aligned open time with `expected_at <= now_ms`, or `None` when none exists yet. It never raises because a first `expected_at` is out of range; it returns `None`.
  - `next_wake_ms(now_ms: int, timeframe: Timeframe, publication_delay_ms: int) -> int` — the smallest `expected_at` of a valid slot that is strictly greater than `now_ms`; `ensure_timestamp_ms(value, "next_wake_ms")` makes it `ValueError` when it would pass `MAX_TIMESTAMP_MS`.
  - `@dataclass(frozen=True) class CollectionPlan`: `now_ms: int`, `newest_expected_open_ms: int | None`, `next_wake_ms: int`.
  - `plan_collection(now_ms: int, timeframe: Timeframe, publication_delay_ms: int) -> CollectionPlan`. It fails closed as a whole: it raises whenever `next_wake_ms` does.

**Algorithm (both `newest_expected_open_ms` and `next_wake_ms` follow it; write it once in a private helper).** Let `tf = timeframe.milliseconds`, `first_expected_at = MIN_TIMESTAMP_MS + tf + delay` (the first valid open time is `MIN_TIMESTAMP_MS`, see Facts).
- If `now_ms < first_expected_at`: newest is `None`, wake is `first_expected_at`.
- Otherwise: `slot_end = (now_ms - delay) // tf * tf` (the largest slot end with `slot_end + delay <= now_ms`; it is at least `MIN_TIMESTAMP_MS + tf`), newest is `slot_end - tf`, wake is `slot_end + tf + delay`.
- Validate first: the time argument and the delay with the checks listed under the validation test; the `ValueError` on the wake comes from `ensure_timestamp_ms` in `next_wake_ms` only.

- [ ] **Step 1: Write failing tests.** Constants: `TF = Timeframe("5m")` (300 000 ms), `MIN = MIN_TIMESTAMP_MS`, `D_DAY = 86_400_000`.
  - `test_the_first_valid_open_time_is_min_for_every_timeframe`: for every label of the supported timeframes (take them from `Timeframe`'s table by trying the twelve labels `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d`), `Timeframe(label).is_aligned(MIN)` and `not Timeframe(label).is_aligned(MIN - 1)`.
  - `test_exact_values_at_the_start_of_the_range`, parametrised over `delay ∈ {0, 15_000, 300_000, 450_000}` (zero, smaller than, equal to and larger than `TF`), with `F = MIN + 300_000 + delay` and these exact rows (`now → (newest, next_wake)`):
    - `MIN → (None, F)`
    - `F - 1 → (None, F)`
    - `F → (MIN, F + 300_000)`
    - `F + 1 → (MIN, F + 300_000)`
    - `F + 300_000 - 1 → (MIN, F + 300_000)`
    - `F + 300_000 → (MIN + 300_000, F + 600_000)`
    Each row asserts `newest_expected_open_ms(...)`, `next_wake_ms(...)` and `plan_collection(...) == CollectionPlan(now, newest, wake)`.
  - `test_delay_larger_than_the_timeframe_at_the_start_of_the_range` (explicit regression): `Timeframe("1m")`, delay `90_000`, `F = MIN + 150_000`: `now = MIN → (None, MIN + 150_000)`; `now = F - 1 → (None, F)`; `now = F → (MIN, F + 60_000)`. A formula that floors `now - delay` and ignores the start of the range returns `MIN + 30_000` for the first row, which belongs to no valid slot.
  - `test_delay_larger_than_the_timeframe_in_the_middle_of_the_range`: `Timeframe("1m")`, delay `90_000`, `E = MIN + 10 * 60_000`: `now = E + 90_000 → newest == E - 60_000`, `now = E + 90_000 - 1 → newest == E - 120_000`.
  - `test_one_day_timeframe_aligns_to_utc_midnight`: `Timeframe("1d")`, delay `15_000`, `now = MIN + 3 * D_DAY + 20_000`: `newest == MIN + 2 * D_DAY`, `next_wake_ms == MIN + 4 * D_DAY + 15_000`.
  - `test_a_first_expected_at_beyond_the_maximum_has_no_slot_and_no_plan`: `Timeframe("1d")`; with `delay = MAX_TIMESTAMP_MS - MIN` the first `expected_at` is `MAX + D_DAY`: for `now = MIN` and `now = MAX_TIMESTAMP_MS`, `newest_expected_open_ms` is `None`, `next_wake_ms` raises `ValueError` and `plan_collection` raises `ValueError`. Boundary: with `delay = MAX_TIMESTAMP_MS - MIN - D_DAY` the first `expected_at` equals `MAX_TIMESTAMP_MS` and `next_wake_ms(MIN, …) == MAX_TIMESTAMP_MS` (no error).
  - `test_next_wake_past_the_maximum_raises` (`now_ms = MAX_TIMESTAMP_MS`, `TF`, `delay = 15_000`: `ValueError` for `next_wake_ms` and `plan_collection`, while `newest_expected_open_ms` is not `None`).
  - Validation, parametrised with the exact exception per case (never a tuple), run for each of `expected_at_ms` (time argument `open_time_ms`), `newest_expected_open_ms`, `next_wake_ms` and `plan_collection` (time argument `now_ms`): `TypeError` for the time argument and `publication_delay_ms` being `True` or `1.5`, for the time argument being `float(MIN)`, and for `timeframe` being the string `"5m"`; `ValueError` for the time argument `MIN - 1` and `MAX_TIMESTAMP_MS + 1`, and for `publication_delay_ms = -1`. In addition `expected_at_ms` raises `ValueError` for `open_time_ms = MAX_TIMESTAMP_MS` (the end passes the maximum).
- [ ] **Step 2: Run** `uv run pytest tests/domain/test_schedule.py -v`. Expected: FAIL with `ModuleNotFoundError: quasar_market_data.domain.schedule`.
- [ ] **Step 3: Implement** the signatures and the algorithm above in `domain/schedule.py`. The module docstring states decisions 2 and 4 (including that S13 implements no retry). Restate the delay check rather than import the private `_ensure_delay` from `closed_candles.py`.
- [ ] **Step 4: Run** `uv run pytest tests/domain/test_schedule.py -v`. Expected: PASS.
- [ ] **Step 5: Run** `make check`. Expected: PASS (lint, typecheck, import contracts, tests).
- [ ] **Step 6: Commit** `feat(domain): UTC boundary and publication-delay planner (S13)` (stage only `schedule.py` and `test_schedule.py`).

### Task 2: Properties and agreement with the closed-candle and coverage rules

**Files:** Modify `tests/domain/test_schedule.py` only. No lasting production change; Step 3 makes temporary mutations that are restored.

**Interfaces:**
- Consumes: Task 1 functions; `is_closed_eligible(open_time_ms, timeframe, publication_delay_ms, lower_bound_ms) -> bool` from `domain/closed_candles.py`; `classify_span_at`, `ExpectationRule` from `domain/timed_coverage.py` (signature in Facts).

- [ ] **Step 1: Write the tests.** They are written after the implementation and pass at once (intentional: they pin Task 1 to existing rules; Step 3 proves they can fail).
  - **Random generator** (shared helper, seeded `random.Random(13)`, 300 cases per test): timeframe random from `5m`, `1h`, `1d`, delay random in `[0, 120_000]`. `now_ms` is drawn from a random aligned slot end `E` in `[MIN + 6 * D_DAY, MAX_TIMESTAMP_MS - 6 * D_DAY]` as `E + delay + offset`, with `offset` from `{-1, 0, 1}` in two thirds of the cases and uniform in `[-tf, tf]` in the rest, so the `expected_at` boundary is hit on purpose. Reserve, checked by hand: `first_expected_at <= MIN + D_DAY + 120_000`, far below `MIN + 6 * D_DAY`, so every case is in the steady regime and `newest` is not `None`; the lowest slot used, `newest - 2*tf`, is above `MIN`; the highest end used, `newest + 3*tf`, and `next_wake <= now + tf` stay at least `3 * D_DAY` below `MAX_TIMESTAMP_MS`, also for `1d`.
  - `test_expected_slot_agrees_with_is_closed_eligible`: for the slots `newest - 2*tf` through `newest + 2*tf` (step `tf`), `is_closed_eligible(open, tf, delay, now) == (open <= newest_expected_open_ms)`; and `is_closed_eligible(newest + tf, …, now)` is false while `is_closed_eligible(newest + tf, …, next_wake_ms(now))` is true.
  - `test_next_wake_is_a_valid_slot_instant_after_now_and_at_most_one_timeframe_away`: `next_wake > now`; `slot_open = next_wake - delay - tf` is aligned and `>= MIN`; and, in these steady-regime cases only, `next_wake <= now + tf`.
  - `test_before_the_first_expected_at_the_wake_is_the_first_expected_at`: separate generator, `now` uniform in `[MIN, first_expected_at - 1]` for random timeframe and delay (including delays above the timeframe), same seed style: `newest is None`, `next_wake == MIN + tf + delay`, `next_wake > now`, and `next_wake - delay - tf == MIN`. The bound `next_wake <= now + tf` is deliberately not asserted here.
  - `test_expected_slot_agrees_with_classify_span_at`, deterministic. `tf = 300_000`, stored candle open time `S0 = MIN + 1000 * tf`, the window is the single empty slot after it, `slot = S0 + tf`, `E = slot + tf + delay = S0 + 2*tf + delay`. For each `delay ∈ {0, 15_000, 300_000, 450_000}` and each `as_of ∈ {E - 1, E, E + 1}` call `classify_span_at(start=slot, end=slot + tf, timeframe_ms=tf, candles=set(), conflicts=set(), unavailable=set(), newest_open_time_ms=S0, as_of_ms=as_of, rule=ExpectationRule(delay))` (default lag 60 000) and assert the exact counts: `slots == 1`; for `as_of = E - 1`: `not_yet_expected == 1`, `due == 0`, `stale == 0`; for `as_of = E` and `E + 1`: `not_yet_expected == 0`, `due == 1`, `stale == 0`; `present == conflicted == unavailable == missing == 0` in all rows. Then assert the planner agrees: `newest_expected_open_ms(as_of, Timeframe("5m"), delay) < slot` exactly when `not_yet_expected == 1` (it equals `S0` at `E - 1` and `slot` at `E` and `E + 1`), and `next_wake_ms(E - 1, …) == E`, `next_wake_ms(E, …) == E + tf`.
- [ ] **Step 2: Run** `uv run pytest tests/domain/test_schedule.py -v`. Expected: PASS.
- [ ] **Step 3: Prove the tests can fail.** First run `git status --short src/quasar_market_data/domain/schedule.py`; it must print nothing (Task 1 is committed). If it prints anything, stop and ask: that means someone else's work is in the file. Apply each mutation alone, run `uv run pytest tests/domain/test_schedule.py -v`, note the failing tests, and restore with `git checkout -- src/quasar_market_data/domain/schedule.py` (this file only; never `git checkout .` or `git stash`):
  - (a) in the steady branch drop `+ delay` from the wake → `test_exact_values_at_the_start_of_the_range`, `test_expected_slot_agrees_with_is_closed_eligible` fail;
  - (b) replace `(now_ms - delay) // tf * tf` by `(now_ms - delay) // tf * tf - 1` → the exact-value rows and `test_one_day_timeframe_aligns_to_utc_midnight` fail;
  - (c) return `slot_end` instead of `slot_end - tf` as newest → both agreement tests fail;
  - (d) delete the `now_ms < first_expected_at` branch so the steady formula is always used → `test_delay_larger_than_the_timeframe_at_the_start_of_the_range` and `test_before_the_first_expected_at_the_wake_is_the_first_expected_at` fail.
  Report the four outcomes. Confirm with `git status --short` that only the intended test change remains.
- [ ] **Step 4: Run** `make check`. Expected: PASS.
- [ ] **Step 5: Commit** `test(domain): pin the planner to the closed-candle and coverage rules (S13)`.

### Task 3: Register and documentation

**Files:** Modify `docs/invariants.md`, `docs/superpowers/specs/2026-10-20-market-data-v1-design.md`.

- [ ] **Step 1:** Add row `INV-047` after `INV-046`: "The planner's expected slot is the same instant as in the closed-candle and coverage rules, it never reads a clock, and its wake time is a valid slot instant strictly after `now`". Status `ENFORCED`. Mechanism: `schedule.py` is pure (`tests/test_domain_purity.py`) and the agreement tests run in `make test`; state the limit: the planner says when to look, not that a candle is closed, the trusted lower bound still decides, and a pending candle needs a separate retry (S14). Evidence: `tests/domain/test_schedule.py::test_exact_values_at_the_start_of_the_range`, `tests/domain/test_schedule.py::test_expected_slot_agrees_with_is_closed_eligible`, `tests/domain/test_schedule.py::test_expected_slot_agrees_with_classify_span_at`, `tests/domain/test_schedule.py::test_next_wake_is_a_valid_slot_instant_after_now_and_at_most_one_timeframe_away`, `tests/test_domain_purity.py::test_domain_package_has_no_violations`. No literal pipe characters in cells.
- [ ] **Step 2:** In spec §11 append to item 5 (it names the `Clock` abstraction shape) one sentence: S13 is a pure planner taking `now_ms`; the `Clock` protocol stays in the collector and S14 passes `clock.wall_ms()` (this plan's decision 1).
- [ ] **Step 3: Run** `make check`. Expected: PASS, including `tests/test_invariant_register.py` and `tests/test_domain_purity.py`.
- [ ] **Step 4: Commit** `docs: register INV-047 and record the S13 planner decision`.

## Self-review

- **Spec coverage:** "pure UTC boundary + publication-delay planner" → Task 1 (including the start of the supported range); "using the injected `Clock`" → decision 1 (the caller injects the time, the domain stays clock-free); D5 publication delay is a parameter, its configuration plumbing is S14.
- **Not in scope:** the collection loop, sleeping, retry of pending candles, concurrency and rate limits (S14), checkpoints (S15), gap detection and backfill (S16), moving `Clock` out of `fetch_once.py`, multi-timeframe scheduling, configuration or an upper bound of the delay, any schema, ADR, contract or S5/S11 change.
- **Types:** `CollectionPlan` fields are produced by the Task 1 functions and consumed unchanged in Task 2; `Timeframe`, `ensure_timestamp_ms`, `is_closed_eligible`, `classify_span_at` and `ExpectationRule` are existing.
- **Risks:** a wake on the wall clock may precede the trusted lower bound, so S14 needs its own bounded retry (decision 4); `now_ms` must come from a wall clock, and a wrong wall clock only shifts wake times, because eligibility still rests on the server-time lower bound. Everything above is unexecuted; the first real run is Task 1 Step 2.
