# S12 `GET /v1/coverage` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only endpoint that, for one series and a window of at most 10 000 slots, answers per state how many slots there are and lists every run of non-present slots, at an `as_of` and with an expectation rule that the response states.

**Architecture:** S11's `classify_span_at` (pure, `domain/timed_coverage.py`) already decides every state and builds the runs. S12 adds only the API layer around it: a strict parser for the new query (sharing the S10 parser), one read transaction that collects the stored facts of the window (`api/coverage_reader.py`), the clock and the rule injected into `create_app`, the JSON body, two `quasar-api serve` options with bounded values, and the contract section. No domain change, no schema change, no new dependency.

**Tech Stack:** Python 3.12, FastAPI/Starlette, sqlite3 (read-only), pytest, mypy strict, ruff, import-linter.

**Spec:** `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (§8 S12, §11.3–11.4), `docs/contracts/api-v1.md` (sections 1–4, 5.6; section 6 reserves `/v1/coverage`), `docs/follow-ups.md` F-8, `docs/invariants.md` (INV-009, INV-010, INV-011, INV-041, INV-042, INV-045). Earlier plans: `2026-10-25-s10-get-v1-candles.md`, `2026-10-25-s11-coverage-calculation.md`.

## Global Constraints

- **The contract is approved before any code** (contract header: "Sections for the reserved endpoints still need their own approval before their slices"; CLAUDE.md: no public contract change without explicit approval). Task 1 is documentation only and ends at a hard stop; Tasks 2–6 start only after the user approves the section.
- Never fetch exchange data in a handler; the API only reads, through `open_reader` (INV-013, INV-014). No new import that `tests/api/test_read_only.py` or the import contracts forbid; `api` must not import `collector` (it therefore does not reuse `collector.fetch_once.Clock`).
- The domain stays untouched and clock-free: `as_of_ms` is read once per request in the `api` layer and passed in (INV-045).
- Never fabricate: a slot without a stored fact is never `present`; an unreadable database is `503`, never an empty `200` (INV-041).
- The window never exceeds `MAX_WINDOW_SLOTS = 10 000` (S10, contract 5.1); every query is bounded to the window plus two per-series aggregates.
- Error order: parameter validation `400`, then readiness `503`, then series lookup `404` (contract section 2). The one error body, `Cache-Control: no-store`, GET only.
- The S10 behavior must not change: `tests/api` and the existing contract example stay green untouched.
- `make check` is the gate for every commit. Do not invent other commands.

## Decisions made in this plan (open to review; D1–D5 are also in the contract section the user approves in Task 1)

1. **`as_of` is the server clock, not a parameter.** The handler reads the clock once per request and the response states it (`as_of_ms`). No `as_of` query parameter in v1. Alternative: an optional `as_of` for replaying history. It makes responses reproducible, but it lets a caller ask for a clock-free-looking answer about a "now" that is not now, and it can be added later without breaking anything (contract section 1, Compatibility). A clock outside the valid time range is a bug: `500`, not a count.
2. **Both `start` and `end` are required.** The coverage window has no defaulting rules (no "newest" special case); an explicit `end`, `start < end`, at most 10 000 slots, aligned, within the valid range: the S10 rules, one parser. "Anything new since T?" stays a `/v1/candles` poll. Alternative: default `end`. It would copy S10's two-branch rule for little gain.
3. **The rule is server configuration, stated in the response.** `quasar-api serve --publication-delay-ms` (default `DEFAULT_PUBLICATION_DELAY_MS`, 15 000, ADR-0001 P-3) and `--lag-threshold-ms` (default `DEFAULT_LAG_THRESHOLD_MS`, 60 000). Bounds (F-8): publication delay `0..300 000` (one `5m` slot), lag threshold `0..900 000` (three slots); a value outside them is refused by argparse (usage error, exit `2`, like `--port`) before `quasar-api serve` binds, and `create_app` refuses an out-of-bound `ExpectationRule` too (it calls `checked_rule`). The numbers are the builder's call, to be confirmed; without a bound a huge lag keeps every slot `due` forever. The API cannot know the delay the Collector used; `stored_ahead_of_as_of` is the one signal of a mismatch (decision 4).
4. **`stored_ahead_of_as_of` is exposed unchanged** (S11 decision 3), next to `series.newest_open_time_ms`, with the S11 wording: a statement about the inputs, never a diagnosis; it changes no count.
5. **Runs are returned in full, not paginated.** Runs split by state as well as at present slots, so the worst case is a 10 000-slot window with no present slot whose slots alternate between two non-present states (for example `conflicted` and `missing`): exactly 10 000 runs of one slot each, about 0.7 MB. A window with a present slot has fewer runs (9 999 with one), so it is not the maximum. Bounded by the window cap, so no cursor and no `limit` in v1. Alternative: paginate runs. A second cursor kind for a case that needs a 34-day window with a different fault in every slot.
6. **Field names follow `/v1/candles`:** counts are `*_slots`, `fully_present` replaces `gap_free` because `gap_free` ignores the clock-dependent states and `fully_present` does not (a window with a `due` slot is not fully present).
7. **F-1 is not settled here.** The echoed-`end` problem belongs to defaulted windows; S12 echoes only an explicit `end`, so `end <= MAX_TIMESTAMP_MS` (the S10 rule) stands and the slot at the maximum stays unreadable. F-1 stays open.

## Review Focus

The inputs the spec implies but the happy path does not exercise. Each has its test in the named task.

1. A series that stopped long ago: every slot after its newest candle is `stale` (not `due`, not `present`), and a window entirely after `as_of` is all `not_yet_expected`; `as_of` exactly on `expected_at` and on `expected_at + lag` over HTTP (Task 5).
2. A reader clock behind the stored data: the newest candle is still `present`, no slot is invented, `stored_ahead_of_as_of` is `true` (Task 5).
3. The true maximum: a 10 000-slot window with no present slot, alternating `conflicted` and `missing`, with the series' newest candle at or after the window's `end` (outside the window), gives exactly 10 000 runs of one slot each whose lengths sum to 10 000, with 5 000 `conflicted` and 5 000 `missing` (Tasks 4 and 5); 10 001 slots is `400` (Tasks 2 and 5). A present slot is never listed (Task 4, separate test).
4. A misconfigured rule (negative, non-integer, or beyond the bound) is refused by `quasar-api serve` before it binds (exit 2), and `create_app` refuses an `ExpectationRule` beyond the bound (Tasks 3, 5 and 6).
5. A recorded conflict or `unavailable` slot after the newest candle keeps its state (never `stale`), a slot before the oldest candle is `missing`, and a write between two statements cannot change one response (one read transaction) (Task 4).

---

## File Structure

- Create: `src/quasar_market_data/api/coverage_reader.py` — the read transaction and `CoverageReport`.
- Create: `src/quasar_market_data/api/rule.py` — bounds and `checked_rule` for the configured expectation rule.
- Modify: `src/quasar_market_data/api/params.py` — shared query parsing, `CoverageRequest`, `parse_coverage_request`.
- Modify: `src/quasar_market_data/api/database.py` — one error-mapping wrapper for both readers, `read_coverage`.
- Modify: `src/quasar_market_data/api/app.py` — route, body, `now_ms` and `rule` arguments of `create_app`.
- Modify: `src/quasar_market_data/api/cli.py` — the two options.
- Modify: `docs/contracts/api-v1.md`, `docs/invariants.md`, `docs/follow-ups.md`, `README.md`.
- Create: `tests/api/test_coverage_reader.py`, `tests/api/test_coverage_endpoint.py`, `tests/api/test_rule.py`; modify `tests/api/test_params.py`, `tests/api/test_cli_serve.py`, `tests/api/test_read_only.py`.

### Task 1: The contract section (documentation only, then a hard stop)

**Files:** Modify `docs/contracts/api-v1.md`.

**Interfaces:** Produces the normative text Tasks 2–6 implement. Nothing in `src` changes.

- [ ] **Step 1:** Add section 6.1 `GET /v1/coverage` (the reserved-endpoints table keeps one row per endpoint and links to it) with these exact decisions:
  - Request: `venue`, `symbol`, `timeframe`, `start`, `end`, all required; the section 1 rules, `end > start`, at most 10 000 slots (`400` with `details.maximum`). No other parameter: `as_of`, `limit` and the rest are `400 invalid_parameter` (decisions 1, 2, 5).
  - Response `200` fields: `venue`, `symbol`, `timeframe`, `start`, `end`; `as_of_ms` (the server clock read once for this request); `rule` = `{"publication_delay_ms", "lag_threshold_ms"}`; `series` = `{"oldest_open_time_ms", "newest_open_time_ms"}`; `stored_ahead_of_as_of` (boolean); `coverage` = `{"slots", "present_slots", "conflicted_slots", "unavailable_slots", "missing_slots", "not_yet_expected_slots", "due_slots", "stale_slots", "fully_present"}`; `runs` = ascending list of `{"state", "start", "end", "slots"}` for the maximal runs of consecutive slots in one non-present state (`end` exclusive). The `coverage` block has nine fields: eight integers (`slots` and the seven `*_slots`) and one boolean, `fully_present`. `slots` equals the sum of the seven `*_slots` (as in section 4, with `beyond_stored` split in three).
  - States: the section 4 table with row 5 `beyond_stored` split by the clock, with `expected_at = slot end + publication_delay_ms`: `as_of_ms < expected_at` → `not_yet_expected`; `expected_at <= as_of_ms <= expected_at + lag_threshold_ms` → `due` (expected, tolerated as ingestion lag, not a fault yet); later → `stale`. `missing`, `conflicted`, `unavailable`, `present` never depend on the clock; a stored candle is `present` even when `as_of_ms` is earlier than its eligibility time. `present` slots are never listed in `runs`.
  - `stored_ahead_of_as_of` is true exactly when `newest_open_time_ms + timeframe + publication_delay_ms > as_of_ms`. It never changes a count; the cause may be a reader clock behind the Collector's, or a publication delay configured larger than the Collector used.
  - Honesty statements: `due` and `stale` are judgments against the stated rule and the server clock, not facts about the market; the API does not know the delay the Collector used; `fully_present: true` means every slot has exactly one stored candle and no recorded disagreement, at that `as_of`.
  - Errors: `400`, `404 unknown_series`, `503`, `500` in the section 2 order. One read transaction (section 5.6 applies). `Cache-Control: no-store`.
  - Configuration: `quasar-api serve --publication-delay-ms` (default 15 000, `0..300 000`) and `--lag-threshold-ms` (default 60 000, `0..900 000`); out-of-bound values stop the process at start-up (decision 3).
  - One example **computed by hand in this task**, not produced by the server: the committed fixture series (100 consecutive candles, newest `1792673100000`), `publication_delay_ms` 15 000, `lag_threshold_ms` 60 000, a fixed `as_of_ms`, a window that runs past the newest candle so it shows `not_yet_expected`, `due` and `stale` slots and their runs. Task 5's test checks the implementation against this approved text.
  - One sentence on the window limit: an explicit `end` above the valid time range is `400` (as in `/v1/candles`), so the slot at the maximum cannot be requested; this is follow-up F-1, deliberately not settled here although the domain accepts `end` up to `MAX + timeframe`.
- [ ] **Step 2:** Update section 0 ("What the API does not promise": staleness is evaluated by 6.1), section 1 (the Time row also lists `as_of_ms`, `rule.*_ms`, `runs[*].start` and `runs[*].end`; this is the only edit to sections 1–3), section 4 (line 80 "the contract uses no clock" becomes "sections 4 and 5 use no clock; 6.1 adds the clock-dependent split"; row 5 points to 6.1), section 5.3 (the sentence "`/v1/coverage` will" becomes present tense with a link), section 8 (traceability rows for `as_of`, the rule and `due`), and add decisions S1–S5 (this plan's 1–5) to section 9 as "S12 decisions, pending". Do not touch sections 5.1, 5.2, 5.4–5.6, 7.
- [ ] **Step 3:** Run `make check` (a docs change must not break the register test). Expected: PASS.
- [ ] **Step 4:** Commit `docs: draft the GET /v1/coverage contract section (S12)`.
- [ ] **Step 5: STOP.** Give the user the section for approval, with decisions 1–5 and the numbers of decision 3. Implement nothing until they answer. On approval change the contract header status (line 3) and the section 6.1 status line to "APPROVED by the user on <date>" in the next commit.

### Task 2: Parse the coverage query

**Files:** Modify `src/quasar_market_data/api/params.py`, `tests/api/test_params.py`.

**Interfaces:**
- Consumes: `parse_candles_request(raw_query: bytes) -> CandlesRequest` (unchanged behavior).
- Existing behavior that both parsers must keep (verified in `parse_candles_request`): `timeframe` is required (missing → `400`, `parameter == "timeframe"`, no `received`), an empty value is rejected by the shared empty-value rule, and any value other than `SERVED_TIMEFRAME.label` (`"5m"`) is rejected with `received` set to the raw value (`"1h"` and `"5M"` are covered by `test_invalid_requests_are_400_with_details`, a missing one by the same test). A rejected value is never replaced by the constant; the `Timeframe` in the request is built only after the check passed.
- Produces: `@dataclass(frozen=True) class CoverageRequest` with `venue: str`, `symbol: str`, `timeframe: Timeframe`, `start: int`, `end: int`; `def parse_coverage_request(raw_query: bytes) -> CoverageRequest`. Both parsers raise `ApiError` (`invalid_parameter`) only.

- [ ] **Step 0: Verify the parser first.** Read `parse_candles_request` and `tests/api/test_params.py`; confirm the timeframe, order and `received` facts above before refactoring. Run `uv run pytest tests/api/test_params.py -q` (expected: PASS) as the baseline.
- [ ] **Step 1: Write the failing tests** in `tests/api/test_params.py`: `test_coverage_request_parses_the_required_fields`; `test_coverage_requires_end` (`details == {"parameter": "end"}`); parametrized `test_coverage_rejects_what_candles_rejects` over unknown, repeated and empty parameters, a misaligned `start`, `end <= start`, `venue=x`, an overlong symbol, `received` cut to 64 characters; `test_coverage_rejects_limit_and_as_of` (both `400`, `details.parameter` is the name); `test_coverage_window_may_be_exactly_10000_slots_and_not_more` (`maximum == 10000` in the second case). Timeframe coverage for both parsers, as one parametrized test over the two parse functions, `test_both_parsers_validate_the_timeframe`: `timeframe=5m` passes; `timeframe=1m`, `timeframe=` (empty) and a missing `timeframe` are `400` with `details.parameter == "timeframe"`, `received` equal to the raw value for `1m` and `""` for the empty one and absent for the missing one. `1h` and `5M` stay in the existing candles test. Add to the candles tests `test_a_bad_limit_wins_over_an_empty_window` (`limit=0&end=<= start` → `details.parameter == "limit"`); it passes before and after the refactor and pins the order. Existing candles tests stay as they are.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_params.py -q`. Expected: the new tests FAIL with `ImportError`/`AttributeError`.
- [ ] **Step 3: Implement.** Extract the body of `parse_candles_request` into private `_parse(raw_query, names, required) -> dict[str, str]` (decode, unknown, repeated, empty, missing), `_series_and_start(values) -> tuple[str, str, int]` (the one place that validates `venue`, `symbol`, `values["timeframe"] != SERVED_TIMEFRAME.label` and then `start`, in today's order; it returns no timeframe, the callers use `SERVED_TIMEFRAME` only after this check has passed), `_end(values) -> int | None` and `_check_window(start, end)` (`end > start`, at most `MAX_WINDOW_SLOTS`). `parse_candles_request` calls them in today's order: parse, series and start, end, limit, window check, so which `400` wins is unchanged; `parse_coverage_request` calls parse, series and start, end (required), window check. The error text and `details` of every existing case stay byte-identical.
- [ ] **Step 4: Run** `make check`. Expected: PASS (S10 params tests untouched and green).
- [ ] **Step 5: Commit** `feat(api): parse the coverage query with the S10 rules (S12)`.

### Task 3: The configured rule and its bounds

**Files:** Create `src/quasar_market_data/api/rule.py`, `tests/api/test_rule.py`.

**Interfaces:**
- Consumes: `ExpectationRule`, `DEFAULT_LAG_THRESHOLD_MS` (`domain/timed_coverage.py`), `DEFAULT_PUBLICATION_DELAY_MS` (`domain/closed_candles.py`).
- Produces: `MAX_PUBLICATION_DELAY_MS: Final = 300_000`, `MAX_LAG_THRESHOLD_MS: Final = 900_000`, `DEFAULT_RULE: Final = ExpectationRule(DEFAULT_PUBLICATION_DELAY_MS, DEFAULT_LAG_THRESHOLD_MS)`, `def check_publication_delay(value: int) -> int` and `def check_lag_threshold(value: int) -> int` (return the value; raise `ValueError` naming the parameter and the bound above it, `TypeError` for a non-`int` or `bool`, `ValueError` for a negative one), and `def checked_rule(publication_delay_ms: int, lag_threshold_ms: int) -> ExpectationRule` composing them and then building the `ExpectationRule`.

- [ ] **Step 1: Write the failing tests:** `test_defaults_are_inside_the_bounds`; `test_the_bounds_themselves_are_accepted`; `test_one_above_a_bound_is_refused` (both parameters, through the single checks and through `checked_rule`; message contains the name and the bound); `test_wrong_types_are_refused` (`True`, `1.5`, `"60"`, `-1`).
- [ ] **Step 2: Run** `uv run pytest tests/api/test_rule.py -q`. Expected: FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** the two checks on top of the domain's `ExpectationRule` rules (construct a one-field-valid rule or repeat only the bound comparison; the type and sign rules stay in the domain) and `checked_rule` from them.
- [ ] **Step 4: Run** `make check`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(api): bound the configured expectation rule (S12, F-8)`.

### Task 4: Read the facts of the window in one transaction

**Files:** Create `src/quasar_market_data/api/coverage_reader.py`, `tests/api/test_coverage_reader.py`; modify `src/quasar_market_data/api/database.py`.

**Interfaces:**
- Consumes: `CoverageRequest` (Task 2); `ExpectationRule` (Task 3); `classify_span_at(*, start, end, timeframe_ms, candles, conflicts, unavailable, newest_open_time_ms, as_of_ms, rule) -> TimedCoverage`; `verify_schema` (from `storage.schema`), `UnknownSeries` (from `candle_reader`, reused, not redefined; `verify_schema` is not re-exported there).
- Produces:
  - `@dataclass(frozen=True) class CoverageReport`: `venue: str`, `symbol: str`, `timeframe: str`, `start: int`, `end: int`, `oldest_open_time_ms: int`, `newest_open_time_ms: int`, `coverage: TimedCoverage`.
  - `def read_coverage_report(conn: sqlite3.Connection, request: CoverageRequest, *, as_of_ms: int, rule: ExpectationRule) -> CoverageReport`: `BEGIN`, `verify_schema`, one `MIN/MAX(open_time_ms)` query for the series (raises `UnknownSeries` when there is none), `SELECT open_time_ms` of the candles in `[start, end)`, `SELECT DISTINCT open_time_ms` of the conflicts and `SELECT open_time_ms` of the unavailable records in the same range, then `classify_span_at`, then `ROLLBACK` in `finally`, as `read_candles_page` does. The candle query selects only the open time (no value columns).
  - `database.py`: `def read_coverage(db_path: Path, request: CoverageRequest, *, as_of_ms: int, rule: ExpectationRule, open_connection: ConnectionFactory = open_reader) -> CoverageReport`. The open/close and the exception-to-`503`/`404` mapping of `read_candles` move into one private function used by both (`_with_reader(db_path, open_connection, read: Callable[[sqlite3.Connection], T], request: CandlesRequest | CoverageRequest) -> T`); `read_candles` keeps its signature and every mapping. The callers pass a closure that looks `read_candles_page` / `read_coverage_report` up in `database.py`'s namespace at call time, and `os` stays a module attribute, because `tests/api/test_read_only.py` and `tests/api/test_database.py` monkeypatch `database.read_candles_page` and `database.os.stat`.

- [ ] **Step 1: Write the failing tests** (seed with `tests/api/seed.make_database`; slot helper as in `test_app.py`; `TF = 300_000`):
  - `test_each_state_is_counted_from_stored_facts`: candles at slots 0, 1, 4 (newest = slot 4); unavailable at 2 and at 7 (after the newest); conflict at 3 and at 6 (after the newest); window slots 0–9; `ExpectationRule(15_000, 60_000)`; `as_of = slot(5) + TF + 15_000 + 60_000 + 1` (`slot(5) + TF + 15_000` is slot 5's `expected_at`, so `as_of` is one millisecond past its stale threshold: slot 5 is `stale`; slots 8 and 9 have a later `expected_at`, so they are `not_yet_expected`). Expected: `present 3, conflicted 2 (3, 6), unavailable 2 (2, 7), missing 0, stale 1 (5), not_yet_expected 2 (8, 9), due 0`; runs in order `unavailable [2,3)`, `conflicted [3,4)`, `stale [5,6)`, `conflicted [6,7)`, `unavailable [7,8)`, `not_yet_expected [8,10)`. The executor recomputes the arithmetic once before writing the test and keeps these slot numbers.
  - `test_slots_before_the_oldest_candle_are_missing`.
  - `test_a_window_after_the_newest_candle_is_all_stale_for_a_dead_series`.
  - `test_unknown_series_raises_unknown_series` and `test_other_series_do_not_leak_in` (a second symbol's candles, conflicts and unavailable records are ignored).
  - `test_all_statements_run_in_one_read_transaction` and `test_a_write_between_statements_cannot_change_the_report` using `tests/api/probe.InterleavingConnection` exactly as `test_candle_reader.py` does for `read_candles_page`.
  - `test_the_true_maximum_is_10000_runs_of_one_slot`: window `[start, end)` of exactly 10 000 slots; the series' only candle is at `request.end` (so the newest is outside the window, every window slot is before it and no slot is `present`); conflict records on the even-indexed slots (0, 2, …, 9 998), none on the odd ones → `conflicted == 5000`, `missing == 5000`, `present == 0`, `len(runs) == 10000`, every run has `end_ms - start_ms == TF`, the lengths sum to `10_000 * TF`, runs are ascending and alternate `conflicted`/`missing`, the counts sum to `slots`.
  - `test_a_present_slot_is_never_listed_in_runs`: the same layout, but a candle on the last slot of the window (it is then the newest and inside the window) → `present == 1`, `len(runs) == 9999`, no run has state `present`. This is a different property from the maximum and is not claimed to be it.
  - `test_database_errors_are_mapped_like_candles` (missing file → `not_ready("database_missing")`, a replaced file → `schema_mismatch`, via `read_coverage`), ; `tests/api/test_database.py` and `tests/api/test_read_only.py` must pass unchanged (gate in step 4).
- [ ] **Step 2: Run** `uv run pytest tests/api/test_coverage_reader.py -q`. Expected: FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** the reader and refactor `database.py`. The window passed to `classify_span_at` is `[request.start, request.end)`; `newest_open_time_ms` is the series' newest, not the window's.
- [ ] **Step 4: Run** `make check`. Expected: PASS, including `tests/api/test_database.py` and `tests/api/test_read_only.py` unchanged.
- [ ] **Step 5: Commit** `feat(api): read the stored facts of a coverage window in one transaction (S12)`.

### Task 5: The endpoint

**Files:** Modify `src/quasar_market_data/api/app.py`; create `tests/api/test_coverage_endpoint.py`.

**Interfaces:**
- Consumes: `parse_coverage_request`, `read_coverage`, `DEFAULT_RULE`, `ExpectationRule`.
- Produces (`create_app` calls `checked_rule(rule.publication_delay_ms, rule.lag_threshold_ms)` first, so an out-of-bound rule raises `ValueError` before any route exists): `create_app(db_path: Path, *, open_connection: ConnectionFactory = open_reader, now_ms: Callable[[], int] = system_now_ms, rule: ExpectationRule = DEFAULT_RULE) -> FastAPI` where `system_now_ms() -> int` is `time.time_ns() // 1_000_000` defined in `app.py`; route `GET /v1/coverage`; `_coverage_body(report: CoverageReport) -> dict[str, object]` with the field order of the contract. `now_ms()` is called once per request, after parsing succeeds and before the database is touched, and its result passes through `ensure_timestamp_ms` (a clock outside the valid range ends as `500 internal_error`: a wrong host clock is a bug in the deployment, not a database condition, so it is not `not_ready`; the explicit check only moves the failure ahead of the database read, `classify_span_at` would raise the same error).

- [ ] **Step 1: Write the failing tests** (`client_for(path, now_ms=lambda: AS_OF, rule=...)`):
  - `test_the_contract_example_is_what_the_server_returns` (fixture database, fixed clock, the parameters of the example; the expected dict is the JSON of the hand-computed example already in the approved contract, copied into the test; a mismatch means the implementation or the contract is wrong, never "paste the output").
  - `test_the_response_states_as_of_and_the_rule`.
  - `test_clock_boundaries_over_http`: one stored candle at `newest`; window `[newest + TF, newest + 2*TF)`; `expected_at = newest + 2*TF + delay`; `as_of` set to `expected_at - 1`, `expected_at`, `expected_at + lag`, `expected_at + lag + 1` → `not_yet_expected`, `due`, `due`, `stale`.
  - `test_a_dead_series_is_stale_not_present`, `test_a_window_entirely_in_the_future_is_not_yet_expected`.
  - `test_a_reader_clock_behind_the_data_sets_stored_ahead_of_as_of_and_keeps_the_candle_present`.
  - `test_runs_list_exactly_the_non_present_slots_with_their_slot_counts` (a seeded `random.Random(21)` database, 300 slots; sum of `runs[*].slots` per state equals the `*_slots` counts; runs ascending, none `present`).
  - `test_the_true_maximum_window_answers_200` (the Task 4 maximum layout over HTTP: newest candle at the window's `end`, `len(runs) == 10000`, every run `slots == 1`, `sum(runs[*].slots) == 10000`, `conflicted_slots == 5000`, `missing_slots == 5000`, `fully_present` false), `test_a_window_of_10001_slots_is_400` (`details.maximum == 10000`).
  - `test_create_app_refuses_an_out_of_bound_rule` (`ExpectationRule(0, MAX_LAG_THRESHOLD_MS + 1)` → `ValueError`).
  - Errors: missing `end` → `400` (`parameter == "end"`), `as_of=1` and `limit=1` → `400`, unknown series → `404 unknown_series`, missing database → `503` with `Retry-After`, `HEAD` and `POST` → `405` with `Allow: GET`, `/v1/coverage/` → `404 not_found`, `400` precedes `503` for a malformed request on a missing database.
  - `test_a_clock_outside_the_valid_range_is_a_500_with_the_standard_body` (`now_ms=lambda: 0`).
  - `test_the_clock_is_read_once_per_request` (a counting `now_ms`: exactly one call on a `200`, zero on a `400`).
  - `test_no_store_header`.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_coverage_endpoint.py -q`. Expected: FAIL (`404` for the route).
- [ ] **Step 3: Implement** the route as the `candles` handler is: parse, read the clock, `run_in_threadpool(read_coverage, ...)`, `_json(200, _coverage_body(report))`. `fully_present` is `coverage.fully_present`; each run's `slots` is `(end_ms - start_ms) // timeframe_ms`.
- [ ] **Step 4: Run** `make check`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(api): GET /v1/coverage (S12)`.

### Task 6: Serve options, register, README, follow-up

**Files:** Modify `src/quasar_market_data/api/cli.py`, `tests/api/test_cli_serve.py`, `tests/api/test_read_only.py`, `docs/invariants.md`, `docs/follow-ups.md`, `README.md`.

**Interfaces:**
- Consumes: `checked_rule`, `DEFAULT_RULE`, `create_app(..., rule=...)`.
- Produces: `quasar-api serve --publication-delay-ms N --lag-threshold-ms N`; an out-of-bound or non-integer value is an argparse usage error (exit `2`) before `uvicorn.run` (per-option `type` functions that match `[0-9]+` and call `check_publication_delay` / `check_lag_threshold`, converting `ValueError` to `argparse.ArgumentTypeError`, so the message names the option and `" 5"` or `"1_0"` are refused).

- [ ] **Step 1: Write the failing tests:** in `test_cli_serve.py`: `test_serve_passes_the_configured_rule_to_the_app` (`monkeypatch.setattr(cli, "create_app", recorder)` besides the `uvicorn.run` patch the existing tests use), `test_defaults_equal_default_rule`, `test_an_out_of_bound_value_exits_2_and_never_binds` for both options, `test_a_negative_a_non_integer_and_a_padded_value_are_refused` (`-1`, `1.5`, `" 5"`, `1_0`); in `test_read_only.py`: extend `test_every_statement_the_api_issues_is_read_only` and `test_the_main_database_file_is_unchanged_by_requests` with a coverage request.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_cli_serve.py tests/api/test_read_only.py -q`. Expected: the new CLI tests FAIL (`unrecognized arguments`).
- [ ] **Step 3: Implement** the options (help text states the default and the bound).
- [ ] **Step 4: Register and docs.** `docs/invariants.md`: add `INV-046` (`ENFORCED`), limited to what S12 adds beyond INV-045 and INV-042: the response states the `as_of` and the rule it was computed with, and the configured publication delay and lag threshold are bounded at start-up and in `create_app` (evidence: `tests/api/test_coverage_endpoint.py::test_the_response_states_as_of_and_the_rule`, `tests/api/test_coverage_endpoint.py::test_create_app_refuses_an_out_of_bound_rule`, `tests/api/test_rule.py::test_one_above_a_bound_is_refused`, `tests/api/test_cli_serve.py::test_an_out_of_bound_value_exits_2_and_never_binds`); update `INV-042` (mechanism and evidence also name `read_coverage_report` and `tests/api/test_coverage_reader.py::test_all_statements_run_in_one_read_transaction`), `INV-045` (last sentence: reporting to readers is S12, now done; keep the status), and `INV-010`/`INV-011`/`INV-009` (their "S12" targets become mechanisms, per-slot listing exists; leave indicators to S18; statuses do not change). `docs/follow-ups.md`: delete F-8 (fixed by S12) and leave F-1 as it is. `README.md`: the `quasar-api serve` paragraph names `GET /v1/coverage` and the two options. The row is added after INV-045 with five cells, no `|` inside a cell, and evidence names that exist by then.
- [ ] **Step 5: Run** `make check`. Expected: PASS, nothing skipped.
- [ ] **Step 6: Commit** `feat(api): configure the expectation rule and register S12 invariants`.

## Self-review

- **Spec coverage:** "S12 `GET /v1/coverage`" and the contract's intent ("per-slot states ... `as_of` and ingestion-lag threshold stated in the response ... where individual gaps are listed") → Tasks 1, 4, 5; F-8 (list `due`, bound the lag, state `as_of` and the rule) → Tasks 1, 3, 5, 6; spec §11.4 states → S11 reuse; INV-009/010/011 targets → Task 6.
- **Types:** `CoverageRequest` (Task 2) is consumed by `read_coverage_report` and `read_coverage` (Task 4); `ExpectationRule` and `DEFAULT_RULE` (Task 3) by Tasks 4–6; `CoverageReport` (Task 4) by `_coverage_body` (Task 5); `UnknownSeries` is the S10 class.
- **Maximum:** the 10 000-run case is tested with the newest candle outside the window and no present slot (Tasks 4 and 5); the 9 999-run layout with a present last slot is kept as a separate test of "present is never listed", not as the maximum.
- **Not in scope:** an `as_of` parameter, paging of runs, a `limit`, other venues or timeframes, F-1 and F-2 to F-7, health (S17), any domain, schema or ADR change, the scheduler's use of the rule (S13).
- **Review:** the independent review of 2026-10-25 (no P1; ten P2, several P3) is worked into this text: the true worst case of exactly 10 000 runs (newest candle outside the window, no present slot), the parser split that keeps the S10 error order, `create_app` enforcing the bound, exit code 2, per-option checks, the `verify_schema` import, the contract text fixes, a hand-computed contract example, and concrete test values.
- **Remaining risk:** the Collector's publication delay and the API's are two separate settings; only `stored_ahead_of_as_of` hints at a mismatch (one direction).
