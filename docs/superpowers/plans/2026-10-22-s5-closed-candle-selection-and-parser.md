# S5 Closed-Candle Selection and Binance Parser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn raw Binance kline rows into `Candle`s and decide, from a trusted server-time lower bound, which of them may be treated as closed — never storing, fabricating or guessing anything else.

**Architecture:** Three pure pieces. `domain/server_time.py` computes the conservative server-time lower bound from one observation on monotonic time and rejects untrusted time (ADR-0001 P-4). `domain/closed_candles.py` applies the eligibility rule and splits candles into eligible (validated), pending and invalid (ADR-0001 P-3, P-5). `collector/binance_klines.py` parses raw rows strictly and all-or-nothing (ADR-0001 P-1, P-2). No I/O, no clock reads and no `ccxt` import; callers pass monotonic and wall readings in as integers. The shape of a `Clock` abstraction is not introduced here (spec section 12 leaves it to the builder; S13 needs it first).

**Tech Stack:** Python 3.12, stdlib only, pytest, mypy strict, ruff, import-linter.

**Spec:** `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (section 6.2, S5 in section 8, section 11). Accepted decisions: `docs/adr/0001-binance-ingestion-fidelity-and-closed-candles.md` (P-1 to P-6; initial values in P-3 and P-4). Earlier plan: `docs/superpowers/plans/2026-10-20-slices-s1-s4.md` (S3 and S4 interfaces).

## Global Constraints

- Branch `market-data-init`; one commit per task, message in each task; trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Do not push.
- `domain` imports only the stdlib allowlist in `tests/test_domain_purity.py` (`__future__`, `collections`, `dataclasses`, `decimal`, `enum`, `re`, `typing`) and itself; no I/O, no clock reads, no `float` anywhere in `domain` (not even in docstrings). Integer arithmetic only for time.
- `collector/binance_klines.py` imports only the stdlib and `domain`; it must not import `ccxt`, `fastapi`, `talib`, `sqlite3` or `api`. Do not edit `pyproject.toml`, `uv.lock`, the purity allowlist, the import-linter contracts or `docs/adr/`. No new dependencies.
- Canonical numbers are decimal text from raw strings; a JSON float or int in a price or volume slot is an error, never converted (ADR-0001 P-1).
- Fail closed: untrusted time confirms nothing; a malformed batch yields no candles; no value is guessed.
- Initial values come from ADR-0001 and are configuration, not behaviour: publication delay 15 000 ms (P-3); `max_rtt_ms=2000`, `max_uncertainty_ms=2000`, `max_age_ms=60000`, `max_drift_ppm=500`, `precision_margin_ms=50`, `max_clock_divergence_ms=100` (P-4).
- TDD: failing test first, record RED and GREEN output. `tests/` is mypy-strict; deliberately wrong runtime types only through `tests/typing_helpers.py::untyped`. A PreToolUse hook runs `make check` on `git commit`; run `make check` before each commit and report real output.

## Safety argument for `server_lower_bound` (ADR-0001 P-4)

Claim: under the assumptions below, `lower_bound_ms <= true server time at now`, so `end + delay <= lower_bound_ms` implies the server clock has passed `end + delay`. Underestimating server time can only delay confirmation, never cause an early one.

Let `T(t)` be the server clock at local monotonic instant `t`. The response carries `S = floor(T(t_s))` for some `t_s` with `m0 <= t_s <= m1` (A1; Binance returns whole milliseconds, so the reading is truncated, which only lowers `S`).

1. `T` is non-decreasing between `t_s` and `m1` (A5, see below), so `T(m1) >= T(t_s) >= S`. The bound is anchored at `m1`, the later end of the window, not at `m0`, and the round-trip time does not enter the bound at all (it is only a rejection gate).
2. For `age = now - m1 >= 0` the server clock advances by at least `age * (1 - ppm/1e6)` (A3, relative rate error at most `max_drift_ppm`). So `T(now) >= S + age * (1 - ppm/1e6)`.
3. `lower_bound = S + floor(age_ns * (1e6 - ppm) / 1e12) - margin`. Flooring never raises the value, `margin >= 0`, and `age_ns * (1e6 - ppm) / 1e12` is exactly `age_ms * (1 - ppm/1e6)` (units: ns times a dimensionless factor, divided by `1e6` ns per ms and `1e6` for the ppm scale). Hence `lower_bound <= T(now) - margin <= T(now)`.

Assumptions the proof needs, and who checks them: A1 (reading inside the window) holds by construction of a request/response and is consistent with the data (below). A2 (the monotonic clock does not jump forward) is not checkable directly; a jump that also shows on the wall clock trips the divergence gate. A3 (rate error) is covered by `max_drift_ppm` and measured at -124 ppm over 30-minute spans; a system suspend stops the monotonic clock on this platform, so `age` is too small and the bound is lower still (safe direction; the divergence gate also rejects it). **A5 (not in ADR-0001 P-4's list):** the server clock does not step backwards between `t_s` and `m1`; the regression gate only compares successive observations. A5 is an additional assumption of the same kind as W-6; recorded here, not changed in the ADR. **Not covered by the bound:** that Binance's `serverTime` is the clock that defines kline boundaries and publication, and any publication lag. The first rests on ADR-0001 E-1 (documentation) and is unverified beyond it; the second is absorbed only by the empirical 15 s delay (P-3), which the ADR states is a margin and not a proven bound.

Evidence measured on 2026-10-22 from the committed fixtures (139 unique `time_observations`, read-only analysis, not yet a test): server time never decreased in monotonic order (0 regressions); `S` lay inside the local wall window `[wall_before, wall_after]` in 139 of 139 observations (consistent with A1); across all 9591 ordered pairs `(i, j)` the bound computed from `i` evaluated at `m1_j` never exceeded `S_j + rtt_j + 1 ms` (0 violations, smallest slack 316 ms) — a falsifiable ceiling, because `T(m1_j) <= S_j + rtt_j + 1` under A1; server-minus-monotonic rate over pairs at least 30 minutes apart ranged from -124 ppm to +189 552 ppm, the large positive values being a system suspend. Task 4 turns the pair check into a test. **Verdict: no blocker.** The argument holds under A1–A3 and A5; the clock-identity and publication-lag caveats are open limitations that the ADR already frames as unproven margins.

## Review Focus

Failure modes the spec implies that a reasonable user would expect handled, most likely first. Each has a test in the owning task.

1. A raw row carries a JSON float or int where a decimal string belongs → the whole batch is rejected, nothing converted (Task 3).
2. One response contains duplicate or non-increasing `open_time` → whole batch rejected, never "first wins" (Task 3).
3. Time cannot be trusted (no observation, large RTT, old observation, server time going backwards, wall/monotonic divergence after a suspend) → no candle is eligible, all are pending, the reason is reported (Tasks 1, 2).
4. A candle exactly on the eligibility boundary is eligible; one millisecond earlier is pending (Task 2).
5. An exchange-reported closed candle with impossible values (for example `high < low`) → never eligible; reported in `invalid` with its violations (Task 2).

---

### Task 1: Server-time trust model

**Files:**
- Create: `src/quasar_market_data/domain/server_time.py`, `tests/domain/test_server_time.py`

**Interfaces:**
- Consumes: `ensure_timestamp_ms` from `domain/timestamps.py`.
- Produces in `server_time.py`:
  - `NS_PER_MS: Final = 1_000_000`.
  - `@dataclass(frozen=True) ServerTimeObservation(server_time_ms: int, send_monotonic_ns: int, receive_monotonic_ns: int, receive_wall_ms: int)`. Construction: every field `type(...) is int` (`TypeError` otherwise); `server_time_ms` and `receive_wall_ms` pass `ensure_timestamp_ms`; both monotonic values `>= 0`; `receive_monotonic_ns >= send_monotonic_ns` (`ValueError`). `receive_wall_ms` is diagnostic and used only by the divergence gate.
  - `@dataclass(frozen=True) TimeTrustConfig(max_rtt_ms: int, max_uncertainty_ms: int, max_age_ms: int, max_drift_ppm: int, precision_margin_ms: int, max_clock_divergence_ms: int)`: all `int` (not `bool`), all `>= 0` (`ValueError`), `max_drift_ppm < 1_000_000`. `DEFAULT_TIME_TRUST: Final = TimeTrustConfig(2000, 2000, 60000, 500, 50, 100)` (ADR-0001 P-4 initial values).
  - `class TimeRejection(StrEnum)` with, in check order: `NO_OBSERVATION`, `MONOTONIC_REGRESSION`, `RTT_TOO_LARGE`, `AGE_TOO_LARGE`, `UNCERTAINTY_TOO_LARGE`, `SERVER_TIME_REGRESSED`, `CLOCK_DIVERGENCE`.
  - `@dataclass(frozen=True) TrustedTime(lower_bound_ms: int)` (`type(...) is int`, else `TypeError`; the value is not range-checked because a bound near the edge of the timestamp range only matters to comparisons) and `@dataclass(frozen=True) UntrustedTime(reason: TimeRejection)` (`TypeError` unless `TimeRejection`).
  - `server_lower_bound(observation: ServerTimeObservation | None, now_monotonic_ns: int, now_wall_ms: int, previous_accepted_server_time_ms: int | None, config: TimeTrustConfig) -> TrustedTime | UntrustedTime`.

Rule (ADR-0001 P-4), all integer arithmetic: `age_ns = now_monotonic_ns - receive_monotonic_ns`; `rtt_ns = receive_monotonic_ns - send_monotonic_ns`; `uncertainty = rtt`. Checks in the enum order: `None` → `NO_OBSERVATION`; `age_ns < 0` → `MONOTONIC_REGRESSION`; `rtt_ns > max_rtt_ms * NS_PER_MS`; `age_ns > max_age_ms * NS_PER_MS`; `rtt_ns > max_uncertainty_ms * NS_PER_MS`; `previous is not None and server_time_ms < previous` (equal is accepted); `abs((now_wall_ms - receive_wall_ms) * NS_PER_MS - age_ns) > max_clock_divergence_ms * NS_PER_MS` (a rejection gate only, never part of the bound). If none fires: `lower_bound_ms = server_time_ms + age_ns * (1_000_000 - max_drift_ppm) // 1_000_000_000_000 - precision_margin_ms` (floor division; the floor only lowers the bound, which is the conservative side). Argument validation (fail closed, raised before any rule): `now_monotonic_ns` is a non-negative `int`, `now_wall_ms` passes `ensure_timestamp_ms`, `previous_accepted_server_time_ms` is `None` or passes `ensure_timestamp_ms` (`TypeError` for `bool`/non-int, `ValueError` for negative or out-of-range). Units: `*_ns` are nanoseconds, `*_ms` milliseconds, `max_drift_ppm` parts per million; the module docstring states this, that `now_monotonic_ns` and the observation's monotonic values must come from the same monotonic clock of the same process (an observation must not outlive the process), and refers to the safety argument above.

- [ ] **Step 1: Write failing tests** in `tests/domain/test_server_time.py`. Fixture-derived constant `OBS = ServerTimeObservation(1_792_677_032_674, 18_050_152_743_666, 18_050_421_880_083, 1_792_677_032_811)` (rtt 269.136 417 ms) with `DEFAULT_TIME_TRUST`:
  - `test_lower_bound_at_receive_time`: `now = receive_monotonic_ns`, wall `1_792_677_032_811` → `TrustedTime(1_792_677_032_624)` (server time minus the 50 ms margin).
  - `test_lower_bound_after_ten_seconds`: `now = receive + 10_000_000_000`, wall `+10_000` → `TrustedTime(1_792_677_042_619)` (age term `9_995` after the 500 ppm reserve, minus 50).
  - `test_lower_bound_satisfies_the_exact_inequality`: over a grid of ages `{0, 1, 999_999, 1_000_000, 12_345_678_901, 60_000_000_000}` ns, drift values `{0, 1, 500, 999_999}` ppm and margins `{0, 50}`, in exact integer arithmetic `lower_bound * 10**12 <= (server_time_ms - margin) * 10**12 + age_ns * (10**6 - ppm)` (the step-3 claim without rounding), with wall readings kept consistent and limits lifted so no gate fires.
  - `test_lower_bound_never_exceeds_modelled_true_server_time`: a deterministic model of the argument. For server rates `{1 - 500e-6, 1, 1 + 500e-6}` expressed as integer ppm offsets, reading instants `t_s` in `{m0, midpoint, m1}`, and ages `{0, 1 s, 10 s, 60 s}`, define the true server time as `T(m1) = S_true_at_ts + (m1 - t_s) * rate` and `T(now) = T(m1) + age * rate` in exact integer ns arithmetic (with `S = floor` of the millisecond value at `t_s`), and assert `lower_bound * 1_000_000 <= T(now)` in ns. This checks the algebra and the truncation, not the assumptions A1–A5.
  - `test_lower_bound_is_non_decreasing_in_age`: over the same ages the bound never decreases.
  - **Mutation check (evidence, not committed):** temporarily change the formula to `(1_000_000 + max_drift_ppm)` or drop the margin and show that the two tests above fail, then restore it; the report records both outputs.
  - one test per `TimeRejection`, as `test_untrusted_time_is_rejected` parametrized with ids equal to the lower-case member names, with an exact outcome: `None` observation; `now = receive - 1`; `max_rtt_ms=269` (rejected) vs `270` (trusted); an observation with rtt exactly 2000 ms trusted and with 2000 ms + 1 ns `RTT_TOO_LARGE`; age exactly `60_000 ms` trusted and `+1 ns` `AGE_TOO_LARGE` (wall kept consistent); `max_uncertainty_ms=200` with default RTT limit → `UNCERTAINTY_TOO_LARGE`; `previous = server_time_ms + 1` → `SERVER_TIME_REGRESSED`, `previous = server_time_ms` trusted; wall elapsed `1_000 + 100` ms against 1 s monotonic → trusted (divergence exactly 100 ms), `+101` → `CLOCK_DIVERGENCE`, and a 376 s wall jump against 1 s monotonic (the suspend case from ADR-0001 E-4) → `CLOCK_DIVERGENCE`.
  - `test_check_order`: an observation violating RTT and age reports `RTT_TOO_LARGE` (first in order).
  - argument tests: `server_lower_bound` with `untyped(True)` / `untyped(1.0)` for `now_monotonic_ns`, `now_wall_ms` and `previous_accepted_server_time_ms`, a negative `now_monotonic_ns` and an out-of-range `now_wall_ms` raise `TypeError` / `ValueError` and never return a bound.
  - construction tests: `untyped(True)`, `untyped(1.0)`, `untyped("1")` for each int field → `TypeError`; `receive < send` → `ValueError`; negative monotonic → `ValueError`; `server_time_ms` outside the timestamp range → `ValueError`; config with a negative value, `max_drift_ppm=1_000_000`, or a `bool` → error; `DEFAULT_TIME_TRUST` equals the ADR values above.
- [ ] **Step 2: Run** `uv run pytest tests/domain/test_server_time.py -v`. Expect: FAIL (module missing).
- [ ] **Step 3: Implement** the signatures above in `server_time.py`, with a module docstring stating that the domain never reads a clock and that readings are supplied by the caller.
- [ ] **Step 4: Run** the tests, then `make check`. Expect: pass; record output.
- [ ] **Step 5: Commit** `feat(domain): conservative server-time lower bound with fail-closed rejection`.

### Task 2: Closed-candle eligibility and selection

**Files:**
- Create: `src/quasar_market_data/domain/closed_candles.py`, `tests/domain/test_closed_candles.py`

**Interfaces:**
- Consumes: `Candle`, `Timeframe`, `Violation`, `validate_candle`, `require_valid`, `CandleValidationError`, `ValidatedCandle`, `TrustedTime`, `UntrustedTime`, `TimeRejection`.
- Produces in `closed_candles.py`:
  - `DEFAULT_PUBLICATION_DELAY_MS: Final = 15_000` (ADR-0001 P-3).
  - `is_closed_eligible(open_time_ms: int, timeframe: Timeframe, publication_delay_ms: int, lower_bound_ms: int) -> bool`: `timeframe.end_exclusive_ms(open_time_ms) + publication_delay_ms <= lower_bound_ms` (equality is eligible). `publication_delay_ms` must be a non-negative `int` and `lower_bound_ms` an `int` (`type(...) is int`; `TypeError` / `ValueError`). All values are milliseconds.
  - `@dataclass(frozen=True) InvalidCandle(candle: Candle, violations: tuple[Violation, ...])`.
  - `@dataclass(frozen=True) ClosedCandleSelection(eligible: tuple[ValidatedCandle, ...], pending: tuple[Candle, ...], invalid: tuple[InvalidCandle, ...], untrusted_reason: TimeRejection | None)`.
  - `select_closed_candles(candles: Sequence[Candle], publication_delay_ms: int, time: TrustedTime | UntrustedTime) -> ClosedCandleSelection`.

Semantics: `UntrustedTime` → `eligible=()`, `invalid=()`, `pending` = all candles, `untrusted_reason` = the reason. `TrustedTime` → each candle in input order, using its own `key.timeframe` and `key.open_time_ms`: not eligible → `pending`; eligible → call `require_valid(candle)` (the only sanctioned way to a `ValidatedCandle`, INV-021) and catch `CandleValidationError`: its `.violations` go to `invalid`, never to `eligible`; otherwise the returned `ValidatedCandle` goes to `eligible`. Callers and tests rely on value equality of the returned candles, not on object identity (the S4 contract happens to return the same object, S5 does not depend on it). Input order is preserved inside each group. `untrusted_reason` is `None` when time is trusted. The function never drops a candle: `len(eligible) + len(pending) + len(invalid) == len(candles)`.

- [ ] **Step 1: Write failing tests** using 5m candles (`open_time_ms=1_700_000_100_000`, end `1_700_000_400_000`; build them with the helper style of `tests/domain/test_validation.py`):
  - `test_boundary_is_eligible_and_one_millisecond_earlier_is_pending`: with delay `15_000`, `lower_bound_ms=1_700_000_415_000` → eligible, `1_700_000_414_999` → pending (`is_closed_eligible` and `select_closed_candles`); delay `0` makes `1_700_000_400_000` the boundary; a 1h candle uses its own timeframe (`end = open + 3_600_000`).
  - `test_mixed_batch_is_split_in_order`: `[closed, boundary, forming]` with a lower bound that makes the first two eligible → `[c for c in eligible] == [closed, boundary]` (value equality), `pending == (forming,)`, `untrusted_reason is None`, `len(eligible) + len(pending) + len(invalid) == 3`.
  - `test_untrusted_time_selects_nothing`, parametrized over every `TimeRejection` member: `UntrustedTime(reason)` → `eligible == ()`, `invalid == ()`, `pending` equals the whole input, `untrusted_reason == reason`.
  - `test_invalid_eligible_candle_is_reported_not_selected`: old enough with `high < low` → `invalid[0].candle == candle`, `invalid[0].violations == validate_candle(candle)`, in neither `eligible` nor `pending`; `test_invalid_candle_that_is_not_yet_eligible_is_pending` puts the same candle in `pending`, not `invalid`.
  - `test_empty_input_gives_empty_selection`: three empty tuples; both time outcomes.
  - argument tests: `publication_delay_ms=-1` → `ValueError`, `untyped(True)` / `untyped(1.5)` → `TypeError`; `untyped(1.5)` as `lower_bound_ms` → `TypeError`; `DEFAULT_PUBLICATION_DELAY_MS == 15_000`.
- [ ] **Step 2: Run** `uv run pytest tests/domain/test_closed_candles.py -v`. Expect: FAIL.
- [ ] **Step 3: Implement** per the semantics above; module docstring states that eligibility is not immutability (ADR-0001 P-6) and that a pending candle is retried, never stored.
- [ ] **Step 4: Run** the tests, then `make check`. Expect: pass; record output.
- [ ] **Step 5: Commit** `feat(domain): closed-candle eligibility and fail-closed selection`.

### Task 3: Binance kline row parser

**Files:**
- Create: `src/quasar_market_data/collector/binance_klines.py`, `tests/collector/__init__.py`, `tests/collector/test_binance_klines.py`

**Interfaces:**
- Consumes: `Candle`, `CandleKey`, `Timeframe`.
- Produces in `binance_klines.py`:
  - `VENUE_BINANCE_USDM: Final = "binance-usdm"`.
  - `class KlineParseError(ValueError)` with `.row_index: int | None`.
  - `parse_klines(rows: Sequence[object], symbol: str, timeframe: Timeframe) -> tuple[Candle, ...]`.

Mapping (ADR-0001 P-2): each row is a `list` or `tuple` of exactly 12 items; index 0 `open_time` and index 6 `closeTime` are `int` (`type(x) is int`, so `bool` is rejected); indices 1–5 and 7 are `str` and become open, high, low, close, volume and turnover through the `Candle` constructor; indices 8–11 are not interpreted. `closeTime == open_time + timeframe.milliseconds - 1` is required (ADR-0001 P-3). `open_time` must be strictly increasing across the batch. The result is all-or-nothing: any problem raises `KlineParseError` (chaining the underlying `ValueError`/`TypeError` from `Candle`/`CandleKey` construction and naming the row index) and no candle is returned. The parser does not validate content (`validate_candle` does that), does not look at the clock and does not decide closure.

- [ ] **Step 1: Write failing tests** with `ROW = [1792676700000, "81700.80", "81700.80", "81454.70", "81555.90", "2123.997", 1792676999999, "173139160.77130", 34093, "811.912", "66183389.55020", "0"]`, symbol `"BTC/USDT:USDT"`, `Timeframe("5m")`:
  - `parse_klines([ROW], ...)` returns one candle with key `("binance-usdm", "BTC/USDT:USDT", Timeframe("5m"), 1792676700000)`, open `"81700.8"`, high `"81700.8"`, low `"81454.7"`, close `"81555.9"`, volume `"2123.997"`, turnover `"173139160.7713"`; `parse_klines([], ...) == ()`.
  - `test_malformed_batch_is_rejected_entirely`, parametrized (ids describe the defect); each case raises `KlineParseError` (assert `.row_index` and that nothing is returned, using a batch whose second row is bad): 11 and 13 items; row is a `dict` or `str`; `open_time` as `float` (`1792676700000.0`), as `bool`, as `str`; `closeTime` `1792676999998` and `1792677000000`; price slot as `float` (`81700.8`), as `int`, as `None`; volume as `float`; turnover slot as `float`; text `"abc"` and `"1e5x"` in a price slot; misaligned `open_time` (`1792676700001` with matching `closeTime`).
  - `test_non_increasing_open_time_is_rejected`: two rows with the same `open_time` and two rows in descending order → `KlineParseError` naming the second row.
  - a valid batch of 3 consecutive rows keeps input order; rows with gaps between them (missing interval) are accepted (gaps are coverage, not parsing).
  - `symbol` problems surface as `KlineParseError` with the `ValueError` chained (for example `symbol=""` with a non-empty batch). For an empty batch nothing is constructed or checked and `()` is returned; the test documents this.
- [ ] **Step 2: Run** `uv run pytest tests/collector/test_binance_klines.py -v`. Expect: FAIL.
- [ ] **Step 3: Implement** the function with a small private row helper; no `try/except` that swallows an error without re-raising it as `KlineParseError`.
- [ ] **Step 4: Run** the tests, then `make check` (also confirms the import-linter contracts still hold). Expect: pass; record output.
- [ ] **Step 5: Commit** `feat(collector): strict all-or-nothing Binance kline row parser`.

### Task 4: Real-fixture integration verification

This task is verification, not test-driven development: the code under test already exists, so no RED run is expected or manufactured. The predicted outcome is PASS; a failure is a finding to investigate and report, not something to fix by changing the test.

**Files:**
- Create: `tests/collector/test_closed_candle_fixtures.py`

**Interfaces:**
- Consumes: `parse_klines`, `server_lower_bound`, `ServerTimeObservation`, `DEFAULT_TIME_TRUST`, `select_closed_candles`, `DEFAULT_PUBLICATION_DELAY_MS`; fixtures `tests/fixtures/binance/*_raw.json` and the `time_observations` entries in `*.meta.json` (keys `server_time_ms`, `monotonic_before_ns`, `monotonic_after_ns`, `wall_after_ms`).

Structural and temporal checks only: no network, no new fixtures. A helper maps one meta observation to `ServerTimeObservation(server_time_ms, monotonic_before_ns, monotonic_after_ns, wall_after_ms)` and evaluates it at `now = receive` (age 0).

- [ ] **Step 1: Write the verification tests**:
  - `test_forming_fixture_splits_closed_from_forming`: parse `btcusdt_5m_forming_raw.json` (2 rows) with the first observation of `btcusdt_5m_forming.meta.json`; expected bound `1_792_677_032_624`; row 1 (`open 1792676700000`, end + 15 s `= 1_792_677_015_000`) is eligible and row 2 (`open 1792677000000`, end + 15 s `= 1_792_677_315_000`) is pending; `invalid == ()`.
  - `test_closed_fixtures_are_all_eligible`: both closed fixtures (BTC, small-price instrument; symbols from their metas) parse to 100 candles each and, evaluated with the first observation of the matching meta, all 100 are eligible, none pending or invalid.
  - `test_all_fixture_time_observations_are_trusted`: every entry under `time_observations` in every `*.meta.json` that has the key yields `TrustedTime` at age 0 with `DEFAULT_TIME_TRUST` (recorded maximum RTT is 760 ms); the test asserts the total is greater than 0 and the implementer reports the count.
  - `test_lower_bound_never_exceeds_later_observation_ceiling`: the falsifiable real-data check from the safety argument. Collect the unique observations of all metas (unique by `(monotonic_before_ns, monotonic_after_ns, server_time_ms)`), sort by `monotonic_after_ns`; for every ordered pair `(i, j)`, `i < j`, compute `server_lower_bound(obs_i, now_monotonic_ns=m1_j, ...)` with a config that lifts `max_age_ms` and `max_clock_divergence_ms` (the pairs include a system suspend) and the ADR drift and margin, and assert `lower_bound_ms <= S_j + rtt_ms_j + 1` where `rtt_ms_j` is the integer-ms floor of `(m1_j - m0_j)`. Also assert server time is non-decreasing in this order (no W-6 regression). The implementer reports the observation count (139 expected from the 2026-10-22 analysis) and the pair count (9591), and the smallest slack.
  - `test_regressing_server_time_is_rejected_on_real_data`: evaluating the first forming observation (server time `1_792_677_032_674`) with the second observation's server time (`1_792_677_033_587`) as `previous_accepted_server_time_ms` yields `SERVER_TIME_REGRESSED`, while the second observation with the first as previous is trusted.
- [ ] **Step 2: Run** `uv run pytest tests/collector/test_closed_candle_fixtures.py -v`. Expect (prediction, not a RED/GREEN cycle): PASS. If a real row or observation fails, do not loosen any rule: investigate in this order — the test's field mapping, fixture integrity, then the rule against ADR-0001 — and report to the user.
- [ ] **Step 3: Run** `make check`. Expect: pass; record output.
- [ ] **Step 4: Commit** `test(collector): closed-candle selection on real Binance fixtures`.

### Task 5: Register update

**Files:**
- Modify: `docs/invariants.md`

- [ ] **Step 1: Update the register.** Every ENFORCED row below cites a test that fails when the invariant is violated; a passing positive test, a control and the Task 4 fixture checks are not evidence. Test names are fixed by Tasks 1–3.
  - `INV-022` (ENFORCED): a candle is selected as closed only when `end + publication delay <= trusted server lower bound`, and untrusted time selects nothing. Evidence: `tests/domain/test_closed_candles.py::test_boundary_is_eligible_and_one_millisecond_earlier_is_pending`, `tests/domain/test_closed_candles.py::test_untrusted_time_selects_nothing`, `tests/domain/test_server_time.py::test_untrusted_time_is_rejected`.
  - `INV-023` (ENFORCED): the Binance parser rejects a malformed batch entirely (wrong shape, float or int in a text slot, inconsistent `closeTime`, misalignment, duplicate or non-increasing `open_time`). Evidence: `tests/collector/test_binance_klines.py::test_malformed_batch_is_rejected_entirely`, `tests/collector/test_binance_klines.py::test_non_increasing_open_time_is_rejected`.
  - `INV-024` (ENFORCED, scope stated in the row): `server_lower_bound` does not exceed the modelled true server time under assumptions A1–A3 and A5; the row says the assumptions themselves, the clock identity of Binance `serverTime` and publication lag are not tested. Evidence: `tests/domain/test_server_time.py::test_lower_bound_never_exceeds_modelled_true_server_time`, `tests/domain/test_server_time.py::test_lower_bound_satisfies_the_exact_inequality` (the Task 1 report shows these fail under the mutated formula).
  - Reword `INV-007` to say that selection exists (INV-022) and that persistence of only eligible candles is still enforced by the S7 writer. Keep INV-008 and INV-020 unchanged. Do not change any other status.
- [ ] **Step 2: Run** `uv run pytest tests/test_invariant_register.py -v`, then `make check`. Expect: pass; record output.
- [ ] **Step 3: Commit** `docs: record S5 invariants in the register`.

**S5 Definition of Done:** `make check` ran green; the three new modules are covered by the tests above; no `float` in `domain`; no `ccxt` import anywhere in `src/`; real fixtures split into eligible and pending as predicted; the register states only what the named negative tests prove.

---

## Self-review notes

- **Spec coverage:** S5 "fixtures → Candle, applying the closed-candle rule" → Tasks 3, 2, 1, 4. ADR-0001: P-1/P-2 (Task 3), P-3 (Tasks 2–3), P-4 (Task 1), P-5 (pending, never stored: Task 2), P-6 (docstring). P-9 request-scope checks (endpoint, `symbol`, market flags) belong to the request side and are left to S9.
- **Type consistency:** `TrustedTime`/`UntrustedTime`/`TimeRejection` (Task 1) are consumed unchanged in Task 2; `Candle`, `Violation`, `ValidatedCandle`, `require_valid`, `validate_candle`, `Timeframe` are the S3 and S4 names.
- **Not in scope:** a `Clock` protocol, configuration loading, retry logic, coverage, persistence, conflict handling, any `ccxt` call. Defaults are module constants that mirror ADR-0001 until configuration exists.
- **Revision after review:** safety argument added with measured fixture evidence (no blocker found; assumption A5 and the clock-identity caveat recorded, ADR-0001 untouched); `now_*` and `previous` arguments validated; Task 2 compares candles by value and obtains `ValidatedCandle` only through `require_valid`; Task 4 is labelled integration verification and gains the falsifiable pair check; ENFORCED rows name fixed negative tests. Units: all `*_ns` are nanoseconds, `*_ms` milliseconds, ppm is dimensionless; each boundary (RTT, age, divergence, publication delay) has an equality-accepted and an off-by-one-rejected test.
- **Known judgement calls (open to review):** the parser lives in `collector` because normalization is a collector responsibility and it needs no exchange library; the divergence gate needs a wall reading, so `ServerTimeObservation` carries `receive_wall_ms` and the caller passes `now_wall_ms`.
