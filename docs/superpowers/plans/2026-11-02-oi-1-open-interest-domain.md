# OI-1 Open Interest Domain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pure building blocks for open interest: the `OpenInterest` value object, eligibility and selection, the cadence and round assignment, and a strict all-or-nothing parser for raw `openInterestHist` rows. No I/O, no storage, no network (issue #1).

**Architecture:** Two domain modules and one collector module, mirroring the candle slices S4/S5. `domain/open_interest.py` holds identity, values and eligibility (ADR-0003 P-2, P-3, P-4). `domain/open_interest_cadence.py` holds the budget arithmetic `c(S, b)` and the deterministic symbol-to-round assignment (P-10). `collector/binance_open_interest.py` parses one raw response strictly, like `collector/binance_klines.py` (P-5); it imports only the stdlib and `domain`. Venue and symbol checks move out of `CandleKey` into two small functions in `domain/candle.py` so both keys share them.

**Tech Stack:** Python 3.12, stdlib only, pytest, mypy strict, ruff, import-linter.

**Spec:** `docs/adr/0003-open-interest.md` (P-2, P-3, P-4, P-5, P-10, P-15 rows marked OI-1, P-16). Related: `docs/adr/0001-binance-ingestion-fidelity-and-closed-candles.md` (P-4 time trust, P-7 decimal grammar), `docs/adr/0002-sqlite-schema-v1.md` (decimal-shape check, which forbids a sign). Patterns: `docs/superpowers/plans/2026-10-22-s5-closed-candle-selection-and-parser.md`.

## Global Constraints

- Branch `oi-1-open-interest-domain` from `main`; one commit per task, message given in each task, trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Push and open a PR only at the end (closes #1).
- `domain` imports only the stdlib allowlist in `tests/test_domain_purity.py` (`__future__`, `collections`, `dataclasses`, `decimal`, `enum`, `re`, `typing`) and itself. No I/O, no clock reads, no `float`. Integer arithmetic only.
- `collector/binance_open_interest.py` imports only the stdlib, `domain` and `collector/binance_klines.py` (for `VENUE_BINANCE_USDM`); never `ccxt`, `fastapi`, `talib`, `sqlite3` or `api`.
- Do not edit `pyproject.toml`, `uv.lock`, the purity allowlist, the import-linter contracts, `docs/adr/` or any storage, API or config module. No new dependencies. No schema change.
- Values are the strings Binance sends, canonicalized by `canonical_decimal` (ADR-0001 P-7) and never computed; a JSON number in a value slot is an error, never converted (P-1, P-2).
- `time_ms` is the **end** of the period (P-3). Nothing in this slice converts it to a candle open time.
- Fail closed: a malformed response yields no rows; untrusted time makes nothing eligible; missing rows are not an error (P-5, P-7).
- Initial values (ADR-0003): publication delay `15_000` ms (P-4); budget `100 ≤ b ≤ 400`, `steady(b) = floor(3b / 4)`, cadence cap `12` (P-10).
- TDD: failing test first; record RED and GREEN output. Deliberately wrong runtime types only through `tests/typing_helpers.py::untyped`. A PreToolUse hook runs `make check` on `git commit`; run `make check` before each commit and report the real output.

## Decisions this plan makes (not fixed by the ADR)

- **Negative values are a construction error**, not a separate validation step. The candle split (construct vs `require_valid`) exists for relationships between fields (`high < low`); open interest has none, and the storage shape check rejects a sign anyway. Consequence: one negative value makes the parser reject the whole response (fail closed, consistent with P-5).
- **Any sign character is rejected**, also `"-0"` and `"+5"`, before canonicalization: `canonical_decimal("-0")` returns `"0"`, which would hide a sign Binance never sends. The check is `value.startswith(("+", "-"))` after the `str` type check and raises `InvalidDecimalError`, so every value-text error has one type.
- **Exponent forms are accepted** (`"1e-5"` → `"0.00001"`) because the value goes through the shared grammar of ADR-0001 P-7, as for candles. Binance sends plain decimals [E-2]; the canonical output is plain either way and fits the ADR-0002 shape.
- **Rows must be strictly increasing in `timestamp`**, as for klines. This also covers "duplicated" (P-5). Binance returns ascending rows [ADR-0003 E-2].
- **`period` is a `Timeframe`.** Binance's documented OI periods are all `Timeframe` labels; `Timeframe` also admits labels Binance OI does not support (`1m`, `3m`, `8h`), which config restricts to `5m` (OI-4).
- **`symbols_for_round` is brought forward from OI-4** (its P-15 row is OI-4) to keep the P-10 arithmetic in one pure module; OI-4 adds its scheduling tests.
- **`period_end_count` is shared.** The parser's "more rows than slots" check and OI-3's "at most `limit` period ends" request rule (P-5) use the same pure function, so the two cannot drift.
- **Invariant register:** this slice adds ENFORCED rows for the two OI-1 guards (eligibility/selection, strict parser). The rows that mirror INV-007/008/009/012 (persistence, conflicts, immutability) need the writer and the task and are added in OI-2 and OI-4.
- **Argument errors are not parse errors.** A bad `start_time_ms`, `end_time_ms`, `market_id` or `symbol` raises `TypeError`/`ValueError` before the response is read: it is a caller bug, not a bad response.
- **Extra keys in a row are ignored**, including a missing or present `CMCCirculatingSupply` (P-2: not stored). Missing required keys are an error.

## Review Focus

1. A value with a sign (`"-1"`, `"-0"`, `"+5"`) or with whitespace (`" 1.5"`) → whole response rejected; nothing is canonicalized into an acceptable value (Task 1 for the value object, Task 4 for the parser).
2. A row whose `timestamp` equals `start_time_ms` or `end_time_ms` is accepted (inclusive bounds, P-5 point 4); one period outside either bound is rejected, as is the mid-period-start row before `start_time_ms` (Task 4).
3. An empty list is a valid response and returns `()`; a non-list (`dict`, `None`, `str`) is rejected (Task 4).
4. `bool` passed where an `int` belongs (`timestamp`, budget, symbol count, time bounds) → `TypeError`, never treated as `1` (Tasks 1, 3, 4).
5. Untrusted time with eligible-looking rows → nothing eligible, all pending, the reason reported (Task 2).

---

### Task 1: Shared identity checks and the `OpenInterest` value object

**Files:**
- Modify: `src/quasar_market_data/domain/candle.py` (move the venue and symbol checks out of `CandleKey.__post_init__` into two functions; `CandleKey` calls them; messages unchanged)
- Create: `src/quasar_market_data/domain/open_interest.py`, `tests/domain/test_open_interest.py`

**Interfaces:**
- Produces in `candle.py`: `ensure_venue(value: str) -> str`, `ensure_symbol(value: str) -> str` (same rules and messages as today; return the value).
- Produces in `open_interest.py`:
  - `@dataclass(frozen=True) OpenInterestKey(venue: str, symbol: str, period: Timeframe, time_ms: int)`: `ensure_venue`, `ensure_symbol`, `type(period) is Timeframe` (`TypeError`), `ensure_timestamp_ms(time_ms, "time_ms")`, `period.is_aligned(time_ms)` (`ValueError` naming the period). Module docstring: `time_ms` is the end of the period `[time_ms − period, time_ms)` and must not be joined to a candle `open_time_ms` by equality (P-3).
  - `@dataclass(frozen=True) OpenInterest(key: OpenInterestKey, open_interest: str, open_interest_value: str)`: `type(key) is OpenInterestKey` (`TypeError`); each value `type(...) is str` (`TypeError`), `value.startswith(("+", "-"))` → `InvalidDecimalError`, then replaced by `canonical_decimal(...)`.
  - `period_end_count(start_ms: int, end_ms: int, period: Timeframe) -> int`: number of `period`-aligned instants in `[start_ms, end_ms]`, i.e. `end_ms // p − ceil(start_ms / p) + 1` with integer ceiling, `0` when there is none. Both bounds pass `ensure_timestamp_ms`; `start_ms <= end_ms` (`ValueError`); `type(period) is Timeframe`.

- [ ] **Step 1: Write failing tests** in `tests/domain/test_open_interest.py` with `KEY = OpenInterestKey("binance-usdm", "BTC/USDT:USDT", Timeframe("5m"), 1_793_620_800_000)`:
  - `test_values_are_canonicalized`: `OpenInterest(KEY, "95677.77900000", "7446123150.67500000")` → `"95677.779"`, `"7446123150.675"`; `"0.00000000"` → `"0"`.
  - `test_signed_values_are_rejected`, parametrized over `"-1"`, `"-0"`, `"-0.0"`, `"+5"` in each value slot → `InvalidDecimalError`.
  - `test_exponent_form_is_canonicalized`: `"1e-5"` → `"0.00001"`.
  - `test_period_end_count`: `[1793620200000, 1793621100000]` → 4; `[1793620500001, 1793621100000]` → 2; `[1793620800001, 1793621099999]` → 0; `[T, T]` → 1; `start > end` → `ValueError`; `untyped(True)` as a bound → `TypeError`.
  - `test_malformed_values_are_rejected`: `" 1.5"`, `"1.5 "`, `""`, `"abc"`, `"1,5"` → `InvalidDecimalError`; `untyped(1.5)`, `untyped(1)`, `untyped(None)` → `TypeError`.
  - `test_key_rejects_unaligned_time`: `time_ms = 1_793_620_800_001` → `ValueError`; with `Timeframe("1h")` and `1_793_620_800_000 + 300_000` → `ValueError`.
  - `test_key_type_checks`: `untyped(True)` and `untyped(1.0)` as `time_ms` → `TypeError`; `untyped("5m")` as `period` → `TypeError`; out-of-range `time_ms` → `ValueError`; venue `"Binance"` and symbol `""` → `ValueError` with the same messages `CandleKey` gives.
  - `test_key_and_value_equality`: two equal constructions are equal and hash equal (frozen).
- [ ] **Step 2: Run** `uv run pytest tests/domain/test_open_interest.py -v`. Expect: FAIL (module missing).
- [ ] **Step 3: Refactor** `candle.py` to `ensure_venue` / `ensure_symbol`, then run `uv run pytest tests/domain/test_candle.py -v`. Expect: PASS unchanged.
- [ ] **Step 4: Implement** `open_interest.py` per the interfaces.
- [ ] **Step 5: Run** the new tests, then `make check`. Expect: PASS; record output.
- [ ] **Step 6: Commit** `feat(domain): open interest value object (ADR-0003 P-2, P-3)`.

### Task 2: Eligibility and selection

**Files:**
- Modify: `src/quasar_market_data/domain/open_interest.py`
- Test: `tests/domain/test_open_interest.py`

**Interfaces:**
- Consumes: `TrustedTime`, `UntrustedTime`, `TimeRejection` from `domain/server_time.py`; `OpenInterest` from Task 1.
- Produces in `open_interest.py`:
  - `DEFAULT_OPEN_INTEREST_PUBLICATION_DELAY_MS: Final = 15_000` (P-4).
  - `is_open_interest_eligible(time_ms: int, publication_delay_ms: int, lower_bound_ms: int) -> bool`: `time_ms + publication_delay_ms <= lower_bound_ms` (equality is eligible). `time_ms` passes `ensure_timestamp_ms`; `publication_delay_ms` a non-negative `int`; `lower_bound_ms` an `int` (`type(...) is int`).
  - `@dataclass(frozen=True) OpenInterestSelection(eligible: tuple[OpenInterest, ...], pending: tuple[OpenInterest, ...], untrusted_reason: TimeRejection | None)`.
  - `select_eligible_open_interest(rows: Sequence[OpenInterest], publication_delay_ms: int, time: TrustedTime | UntrustedTime) -> OpenInterestSelection`: the delay is validated **first**, before the trust branch; `UntrustedTime` → all pending with its reason; `TrustedTime` → split in input order; `len(eligible) + len(pending) == len(rows)`; any other `time` type → `TypeError`.

- [ ] **Step 1: Write failing tests** (row at `T = 1_793_620_800_000`):
  - `test_boundary_is_eligible_and_one_millisecond_earlier_is_not`: delay `15_000`, lower bound `T + 15_000` → eligible, `T + 14_999` → not (both functions); delay `0` makes `T` the boundary.
  - `test_time_ms_is_the_period_end` (the P-3 guard): delay `15_000`, lower bound exactly `T + 15_000` → eligible. An implementation that treats `time_ms` as an open time and adds the period (`T + 300_000 + 15_000`) rejects it, so this test fails for that bug.
  - `test_mixed_rows_are_split_in_order`: rows at `T − 300_000`, `T`, `T + 300_000`, lower bound `T + 15_000` → `eligible` the first two in order, `pending` the third, `untrusted_reason is None`.
  - `test_untrusted_time_selects_nothing`, parametrized over every `TimeRejection` member → `eligible == ()`, `pending == tuple(rows)`, `untrusted_reason == reason`.
  - `test_empty_rows`: both time outcomes give empty tuples.
  - argument tests: delay `-1` → `ValueError`, also with `UntrustedTime` (validated before the trust branch); `untyped(True)` / `untyped(1.5)` as delay or lower bound → `TypeError`; out-of-range `time_ms` in `is_open_interest_eligible` → `ValueError`; `untyped(object())` as `time` → `TypeError`; `DEFAULT_OPEN_INTEREST_PUBLICATION_DELAY_MS == 15_000`.
- [ ] **Step 2: Run** the new tests. Expect: FAIL.
- [ ] **Step 3: Implement** per the interfaces; docstring states that eligibility is separate from when to ask (P-4, P-10) and that an absent row is never inferred.
- [ ] **Step 4: Run** the tests, then `make check`. Expect: PASS; record output.
- [ ] **Step 5: Commit** `feat(domain): open interest eligibility and fail-closed selection (ADR-0003 P-4)`.

### Task 3: Cadence, budget shares and round assignment

**Files:**
- Create: `src/quasar_market_data/domain/open_interest_cadence.py`, `tests/domain/test_open_interest_cadence.py`

**Interfaces:**
- Produces in `open_interest_cadence.py`:
  - `MIN_BUDGET_PER_5MIN: Final = 100`, `MAX_BUDGET_PER_5MIN: Final = 400`, `MAX_CADENCE: Final = 12`.
  - `class CadenceError(ValueError)`: raised when no `c ≤ MAX_CADENCE` fits; the message names `symbol_count`, `budget_per_5min` and the largest accepted symbol count `12 × floor((steady(b) − 2) / 2)`.
  - `steady_budget(budget_per_5min: int) -> int`: `budget * 3 // 4`. `budget` an `int` (not `bool`, `TypeError`) in `[100, 400]` (`ValueError`).
  - `open_interest_cadence(symbol_count: int, budget_per_5min: int) -> int`: smallest `c ≥ 1` with `2 × ceil(S / c) + 2 ≤ steady(b)`; `CadenceError` if `c > 12`. `symbol_count` an `int ≥ 1` (plain `ValueError`, not `CadenceError`). Integer ceiling only (`-(-S // c)`).
  - `backfill_share(symbol_count: int, budget_per_5min: int) -> int`: `b − 2 × ceil(S / c) − 2` with `c = open_interest_cadence(S, b)`.
  - `symbols_for_round(symbols: Sequence[str], boundary_ms: int, c: int, period: Timeframe) -> tuple[str, ...]`: each symbol passes `ensure_symbol`; sort (Python `str` order), keep index `i` iff `(boundary_ms // period.milliseconds) % c == i % c`, return in sorted order. `boundary_ms` must be aligned to `period` (`ValueError`); `c` an `int` in `[1, 12]`; duplicate symbols → `ValueError`; empty `symbols` → `()`.

- [ ] **Step 1: Write failing tests**:
  - `test_cadence_at_default_budget`: `c(S, 400)` for S = 1, 149, 150, 300, 600, 1 788 is 1, 1, 2, 3, 5, 12; `c(1_789, 400)` raises `CadenceError` naming 1 788.
  - `test_cadence_at_lower_budgets`: `c(10, 100) = 1`, `c(36, 100) = 1`, `c(37, 100) = 2`, `c(432, 100) = 12`, `c(433, 100)` raises; `c(600, 200) = 9`.
  - `test_backfill_share_is_at_least_a_quarter`: for every `b` in `{100, 101, 199, 200, 300, 399, 400}` and every `S` from 1 to the largest accepted, `backfill_share(S, b) >= b − steady_budget(b)`; spot values `backfill_share(10, 400) == 378`, `backfill_share(600, 400) == 158`, `backfill_share(10, 100) == 78`, `backfill_share(600, 200) == 64` (ADR P-10 tables).
  - `test_budget_bounds`: 99, 401, 0, -1 → `ValueError`; `untyped(250.0)`, `untyped("400")`, `untyped(True)` → `TypeError`; 100 and 400 accepted. `symbol_count` 0 → `ValueError` with `type(err) is not CadenceError`; `untyped(True)` → `TypeError`.
  - `test_every_symbol_once_per_cadence_rounds`: 600 generated symbols, `c = 5`, five consecutive aligned boundaries starting at `T = 1_793_620_800_000` → each symbol appears exactly once in total, each round has 120 symbols; with 7 symbols and `c = 3`, starting at `T` (residue `(T // 300_000) % 3 == 0`), the rounds at `T`, `T + 5m`, `T + 10m` have sizes 3, 2, 2 and the first is the sorted symbols at indices 0, 3, 6.
  - `test_assignment_ignores_input_order`: a shuffled copy gives the same tuples.
  - `test_round_argument_checks`: unaligned boundary, duplicate symbol, `c` 0 and 13, symbol `""` → `ValueError`; `untyped(True)` as `c` and `untyped(1)` as a symbol → `TypeError`.
- [ ] **Step 2: Run** `uv run pytest tests/domain/test_open_interest_cadence.py -v`. Expect: FAIL.
- [ ] **Step 3: Implement** per the interfaces; module docstring cites P-10 and states that the Collector and the API both derive `c` from this function so they agree.
- [ ] **Step 4: Run** the tests, then `make check`. Expect: PASS; record output.
- [ ] **Step 5: Commit** `feat(domain): open interest cadence and round assignment (ADR-0003 P-10)`.

### Task 4: Raw `openInterestHist` parser

**Files:**
- Create: `src/quasar_market_data/collector/binance_open_interest.py`, `tests/collector/test_binance_open_interest.py`

**Interfaces:**
- Consumes: `OpenInterest`, `OpenInterestKey`, `period_end_count` (Task 1), `Timeframe`, `ensure_timestamp_ms`, `ensure_symbol`, `VENUE_BINANCE_USDM` from `collector/binance_klines.py`.
- Produces in `binance_open_interest.py`:
  - `class OpenInterestParseError(ValueError)` with `.row_index: int | None` (same shape as `KlineParseError`).
  - `parse_open_interest_hist(response: object, *, market_id: str, symbol: str, period: Timeframe, start_time_ms: int, end_time_ms: int) -> tuple[OpenInterest, ...]`.

Argument checks first (plain `TypeError`/`ValueError`): `market_id` a non-empty `str`; `ensure_symbol(symbol)`; `type(period) is Timeframe`; both bounds pass `ensure_timestamp_ms`; `start_time_ms <= end_time_ms`.

Response checks, each an `OpenInterestParseError` and nothing returned: not a `list` (row_index `None`); more rows than `period_end_count(start, end, period)` (row_index `None`, checked before any row is parsed); per row: not a `dict`; missing `symbol`, `timestamp`, `sumOpenInterest` or `sumOpenInterestValue`; `symbol` not equal to `market_id`; `timestamp` not `type(...) is int`; `timestamp` outside `[start_time_ms, end_time_ms]`; `timestamp` not strictly greater than the previous row's; value slot not a `str`; any error from `OpenInterestKey` / `OpenInterest` construction (unaligned, sign, grammar), chained. Other keys are ignored. An empty list returns `()`.

- [ ] **Step 1: Write failing tests** with `ROW = {"symbol": "BTCUSDT", "sumOpenInterest": "95677.77900000", "sumOpenInterestValue": "7446123150.67500000", "CMCCirculatingSupply": "19951234.00000000", "timestamp": 1793620800000}`, `market_id="BTCUSDT"`, `symbol="BTC/USDT:USDT"`, `period=Timeframe("5m")`, range `[1793620200000, 1793621100000]` (period ends −2 … +1 around `ROW`):
  - `test_parses_a_row`: one `OpenInterest` with key `("binance-usdm", "BTC/USDT:USDT", Timeframe("5m"), 1793620800000)`, values `"95677.779"`, `"7446123150.675"`; without `CMCCirculatingSupply` and with an extra unknown key the result is the same.
  - `test_empty_response_is_valid`: `[]` → `()`.
  - `test_inclusive_bounds`: rows at `start` and at `end` are accepted; four rows filling the range in order are accepted; gaps (only `start` and `end`) are accepted.
  - `test_malformed_response_is_rejected_entirely`, parametrized (ids name the defect), asserting `OpenInterestParseError` and the given `.row_index`. Each case must trip **only its own guard**: the bad row's `timestamp` stays inside the range, aligned and after the previous row unless that is the defect under test.
    - At index 0 (no previous row, so the ordering check cannot mask the guard): row before `start` (`start − 300_000`); mid-period start (range `[1793620500001, 1793621100000]` with a row at `1793620500000`).
    - At index 1 after a valid first row at `start` (`1793620200000`), with the bad row at `1793620800000` unless stated: row after `end` (`end + 300_000`); unaligned `timestamp` (`1793620800001`, still after row 0 and inside the range); duplicated `timestamp` (`1793620200000`); descending (first row at `1793620800000`, second at `1793620500000`, both inside the range and aligned); wrong `symbol` (`"ETHUSDT"`); `symbol` missing; `timestamp` as `str`, `float`, `bool`; value as `float` (`95677.779`), `int`, `None`; value `"-1"`, `"+5"`, `" 1"`, `"abc"`; row as `list` or `str`; `sumOpenInterestValue` missing.
    - Each case also asserts a distinctive word of the error message (for example `"before"`, `"after"`, `"aligned"`, `"increasing"`, `"symbol"`), so a different guard firing fails the test.
  - `test_non_list_response_is_rejected`: `{}`, `None`, `"[]"` → `OpenInterestParseError` with `row_index is None`.
  - `test_row_count_is_checked_before_rows`: range of 2 period ends with 3 rows whose row 0 is malformed (`timestamp` as `str`) → `OpenInterestParseError` with `row_index is None`, which proves the count check runs first.
  - `test_argument_errors_are_not_parse_errors`: `start > end`, `market_id=""`, `symbol=""`, `untyped(True)` as `start_time_ms` → `ValueError`/`TypeError` and **not** `OpenInterestParseError` (assert `type(err) is not OpenInterestParseError`).
- [ ] **Step 2: Run** `uv run pytest tests/collector/test_binance_open_interest.py -v`. Expect: FAIL.
- [ ] **Step 3: Implement** with a private row helper; module docstring cites P-5 and states the newest-`limit` truncation rule is enforced by the request side (OI-3) and only detected here as "more rows than slots".
- [ ] **Step 4: Run** the tests, then `make check` (also checks the import-linter contracts). Expect: PASS; record output.
- [ ] **Step 5: Commit** `feat(collector): strict all-or-nothing open interest history parser (ADR-0003 P-5)`.

### Task 5: Invariant register

**Files:**
- Modify: `docs/invariants.md` (two rows after INV-070)

- [ ] **Step 1:** Add, in the existing column style:
  - `INV-071` — "An open interest row is selected as eligible only when its period end plus the publication delay is at or before the trusted server-time lower bound, and untrusted time selects nothing" — `ENFORCED` — mechanism `is_open_interest_eligible` and `select_eligible_open_interest` in `domain/open_interest.py` (ADR-0003 P-3, P-4); scope: selection rule only, nothing persists open interest yet (OI-2, OI-4) — evidence `tests/domain/test_open_interest.py::test_boundary_is_eligible_and_one_millisecond_earlier_is_not`, `tests/domain/test_open_interest.py::test_time_ms_is_the_period_end`, `tests/domain/test_open_interest.py::test_untrusted_time_selects_nothing`.
  - `INV-072` — "The Binance open interest history parser rejects a malformed response entirely: not a list, more rows than period ends, a row outside the requested range, an unaligned, duplicated or non-increasing timestamp, a wrong symbol, a non-string, signed or malformed value" — `ENFORCED` — mechanism `parse_open_interest_hist` in `collector/binance_open_interest.py` raises `OpenInterestParseError` and returns no row (ADR-0003 P-5); it does not check that the request respected the request rules (OI-3) — evidence `tests/collector/test_binance_open_interest.py::test_malformed_response_is_rejected_entirely`, `tests/collector/test_binance_open_interest.py::test_non_list_response_is_rejected`, `tests/collector/test_binance_open_interest.py::test_row_count_is_checked_before_rows`.
  - No literal pipe characters in the cells.
- [ ] **Step 2: Run** `uv run pytest tests/test_invariant_register.py -v`, then `make check`. Expect: PASS; record output.
- [ ] **Step 3: Commit** `docs: invariant register rows for open interest eligibility and parser (INV-071, INV-072)`.

### Task 6: Pull request

- [ ] **Step 1:** `make check` on the branch head; record output.
- [ ] **Step 2:** Push `oi-1-open-interest-domain` and open a PR against `main` titled `OI-1: open interest domain, cadence and raw parser`, body: summary per task, the "Decisions this plan makes" list, verification output, `Closes #1`, ending with the attribution line.

## Review history

- 2026-11-02, independent review (fresh agent, read-only, against ADR-0003 and the code): all numbers verified. Applied: parser cases arranged so only the guard under test fires, plus message checks (finding 1); P-3 test replaced by `test_time_ms_is_the_period_end` (2); Task 5 invariant register (3); shared `period_end_count` for OI-3 (4); row-count test with a malformed row 0 (5); `symbols_for_round` noted as brought forward, parameter `c`, function `open_interest_cadence`, symbols checked, round order named (6); `period` wording (7); sign check as `startswith`, `InvalidDecimalError`, exponents accepted (8); `CadenceError` excluded from the `symbol_count` test (9); delay validated before the trust branch, out-of-range `time_ms` (10).
