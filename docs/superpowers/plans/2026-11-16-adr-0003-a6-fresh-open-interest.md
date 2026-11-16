# ADR-0003 A-6 (fresh open interest) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ask a configured subset of 200 open interest symbols every period (`c = 1`) within the unchanged 400 budget, and let consumers see that subset and its health without changing any existing field.

**Architecture:**
- The pure cadence function gets a shared retry reserve of `min(50, n)`.
- A required `[open_interest].symbols` key (`"all"` or a list) is resolved by the settings loader and fed to both the Collector task and the API.
- Each round hands out retry tickets atomically on its event loop.
- The backfill gate counts a late or slow previous round in full.
- The API gains two additive fields: `collected` in `/health/ingest.open_interest` and `open_interest_period` in each `/v1/instruments` entry.
- The overview tool reads that field.

**Tech Stack:** Python 3.12, asyncio, FastAPI, SQLite WAL, pytest, uv, ruff, mypy, import-linter.

**Spec:** `docs/superpowers/specs/2026-11-16-adr-0003-a6-fresh-open-interest-design.md` (revision 4). Read it with this plan; § numbers refer to it.

## Decisions (all cleared, 2026-11-16)

D-1, D-3 and D-4 were approved as proposed, and D-2 was revised to the additive `collected` object. D-5 uses the API field (Task 9). D-6 keeps the 10 s default and measures request durations (Task 4). D-7 adopts the pace rule (Task 4).

## Global Constraints

- `budget_per_5min` stays an integer in `[100, 400]` (40 % of the documented 1 000; 60 % left to other users of the IP).
- `steady(b) = floor(3b / 4)` unchanged; the allocated backfill share is `b − round_cost(S, c)` ≥ `b − steady(b)` in every accepted case.
- `R = 50`; `reserve(n) = min(50, n)`; `round_cost(S, c) = n + reserve(n) + 2`, `n = ceil(S / c)`; `MAX_CADENCE = 12`.
- Largest accepted `S`: `12 × (steady − 52)` when `steady ≥ 102`, otherwise `12 × ((steady − 2) // 2)`. That is 2 976 at 400, 1 176 at 200 and 432 at 100.
- `pace_limit_ms(n) = ceil(n / MAX_IN_FLIGHT) × 600`. That is 60 000 at `n = 200` and 39 600 at `n = 131`.
- No schema change, no migration, no new table; no stored row is touched.
- Every existing response field keeps its meaning. With `symbols = "all"` the values equal a pre-A-6 build reading the same file without the key. The only value change is `behind[].configured` with a list (spec §3).
- The symbol list comes only from the file and is never derived at run time.
- Integer arithmetic only in the domain; the domain imports no CCXT, FastAPI or SQLite (`make imports`).
- One branch, one PR (squash-merged; the PR title and body are the squash commit message). No release tag or version bump.
- The oi-round log field is spelled `retry_reserve_exhausted=N`, as `line()` emits one field per enum value. The same spelling is used in the code, the tests, the README and `docs/reference.md`.
- `tools/symbol_overview/refresh.py` already has an unrelated uncommitted change (`concurrency` 4→2, `min_interval_s` 15→5). Never stage that hunk; use `git add -p` for that file.

## Review Focus

- A file whose `symbols` lists a symbol in a different case (`btc/usdt:usdt`) must be refused, not matched. Task 2 tests it.
- `"all"` must behave like the pre-A-6 build: the same `c`, the same outer health fields, and `collected` equal to the outer counts. Tasks 2 and 6 test it.
- A round in which every symbol fails must never start more than `n + 50 + 1` budget starts. Task 3 tests it.
- `open_interest_period` must be `null` everywhere when `[open_interest]` is absent or `enabled = false`, even if `symbols` lists the symbol. Task 7 tests it.
- A listed symbol whose only stored series has another period counts as `never_stored`. Task 6 tests it.

---

### Task 0: Branch and documents

**Files:**
- Modify `docs/adr/0003-open-interest.md`:
  - the header line (A-6 summary);
  - P-10: the formula; the allocation row "Retry reserve" becomes `≤ min(50, ceil(S / c))`; the cadence tables; Timing "Planned latency" with `p`; the ideal-run clause on late starts; the pace rule;
  - P-12: the share range stays "100–378"; 600 symbols at the 148 share take about 5.7 h;
  - P-13: the `symbols` key; "no second symbol list" is replaced; the largest `S` is 2 976;
  - P-15: the rows of spec §5; Consequences; O-5 answered by A-6;
  - revision history "Amendment A-6" with the user's decisions of 2026-11-16 (D-1, D-3 and D-4 approved; D-2 revised; D-5, D-6 and D-7 as decided at the gate);
  - P-10 latency text keeps the "measured" and "estimated" labels and the table of spec §2.6, including the missed and unbounded lines.
- Modify `docs/contracts/api-v1.md`:
  - 6.4.2: `open_interest_period`;
  - 6.5.7: `S` is the number of open interest symbols; the latency sentence gains `+ p`;
  - 6.5.8: the `configured` wording, the `collected` object and its definitions, the masking sentence, the health `stale` edge of spec §2.6;
  - the status line (additive amendment A-6).
- Modify `docs/reference.md`:
  - the `[open_interest]` keys;
  - "1788 at 400" becomes 2 976;
  - the `/v1/instruments` shape;
  - the fields of the health open interest object.
- Modify `docs/invariants.md`:
  - new PLANNED rows INV-146 to INV-151, in the order of the spec §5 list (reserve; listed symbols only; atomic tickets; `collected`; `open_interest_period`; pace rule);
  - each row's Target is `ADR-0003 A-6, Task N`, and the task that adds its test flips it to ENFORCED.
- Modify `README.md`:
  - `[open_interest]` gets `symbols`;
  - the oi-round line gets `retry_reserve_exhausted=`;
  - the hourly measurement line (if D-6 is as proposed);
  - the overview section ("not collected", if D-5 is confirmed).

- [ ] **Step 1:** Run `git log origin/main..main`. If it lists commits, stop and ask the user. Otherwise run `git switch -c feat/oi-a6-fresh-open-interest`.
- [ ] **Step 2:** Write the documents, copying the numbers from spec §2.1, §2.4, §2.6 and §2.7 verbatim. Write the parts behind an uncleared gate only once the gate is cleared.
- [ ] **Step 3:** Run `uv run pytest tests/test_invariant_register.py -q`. Expected: PASS.
- [ ] **Step 4:** Commit `docs: ADR-0003 A-6 shared retry reserve, open interest symbols, collected health and instruments field`.

### Task 1: Cadence with the shared retry reserve (domain)

**Files:**
- Modify: `src/quasar_market_data/domain/open_interest_cadence.py`
- Test: `tests/domain/test_open_interest_cadence.py`

**Interfaces:**
- Produces:
  - `RETRY_RESERVE: Final = 50`;
  - `retry_reserve(round_size: int) -> int`, returning `min(50, round_size)` for `round_size ≥ 1`;
  - `round_cost(symbol_count, c)`, now using the new formula;
  - `largest_symbol_count(budget_per_5min: int) -> int`;
  - `pace_limit_ms(round_size: int) -> int`;
  - `open_interest_cadence` keeps its signature, and its `CadenceError` message names `largest_symbol_count(b)`.

- [ ] **Step 1: Write the failing tests**
  - `test_fixed_points`:
    - `c(10,100)=1`, `c(36,100)=1`, `c(37,100)=2`, `c(432,100)=12`; `c(433,100)` raises;
    - `c(200,400)=1` with share 148; `c(248,400)=1`; `c(249,400)=2`;
    - `c(522,400)=3` with share 174; `c(600,400)=3` with share 148;
    - `c(200,200)=3` with share 81; `c(522,200)=6` with share 61; `c(600,200)=7` with share 62;
    - `c(200,100)=6` with share 30; `c(2976,400)=12` with share 100.
  - `test_largest_symbol_count`: 2 976 at 400, 1 176 at 200, 432 at 100; the `CadenceError` text for `largest + 1` contains the number.
  - `test_retry_reserve`:
    - values: `(1)=1`, `(50)=50`, `(51)=50`, `(200)=50`;
    - `0` and `-1` raise `ValueError`; `True` and `1.0` raise `TypeError`.
  - `test_pace_limit`: `(200)=60_000`, `(131)=39_600`, `(1)=600`.
  - `test_every_budget_and_symbol_count`, the exhaustive check of spec §5:
    - every `b` in `range(100, 401)`, every `S` in `range(1, largest_symbol_count(b) + 2)`;
    - checks properties (1) to (6);
    - the legacy oracle is a local `_legacy_cadence(S, b)` using `2 × ceil(S / c) + 2`;
    - computes `c` once per pair and checks minimality with `round_cost` directly.
- [ ] **Step 2:** Run `uv run pytest tests/domain/test_open_interest_cadence.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement, and update the module docstring.
- [ ] **Step 4:** Run the file again with `--durations=3`. Expected: PASS, and the exhaustive test under about 5 s. If it is slower, optimise the loop (for example by iterating `c` incrementally per `b`). Do not move it out of the default suite: it is the evidence for an ENFORCED invariant.
- [ ] **Step 5:** Run `uv run pytest tests/domain tests/collector tests/api/test_open_interest_rule.py -q`. Change hard-coded `c` values only where the spec table says they change. Any other failure is a bug.
- [ ] **Step 6:** Add the exhaustive test to INV-090's evidence. Commit `feat(domain): shared open interest retry reserve min(50, n) (ADR-0003 A-6)`.

### Task 2: `[open_interest].symbols` (configuration)

**Files:**
- Modify: `src/quasar_market_data/config/settings.py` (`OpenInterestSettings`, `_open_interest`, the `_check_keys` set)
- Modify: `config/quasar.toml`, `config/quasar.compose.toml` (`symbols = "all"` until Task 8), `tests/config_files.py` and every test TOML with `[open_interest]`
- Test: `tests/config/test_open_interest.py`

**Interfaces:**
- Produces:
  - `OpenInterestSettings.symbols: tuple[str, ...]`, sorted and resolved (`"all"` becomes the venue's symbols);
  - `_open_interest(root, venue_symbols: tuple[str, ...])` replaces the `symbol_count` parameter;
  - the cadence is checked with `len(symbols)`.

- [ ] **Step 1: Write the failing tests**
  - `test_symbols_all_resolves_to_the_venue_symbols` (same `c` as before A-6)
  - `test_symbols_list_is_sorted_and_kept`
  - `test_symbols_invalid_values_are_rejected_by_key`, parametrized; each case raises `ConfigError` keyed `open_interest.symbols`:
    - the key missing; `""`; `"ALL"`; `[]`; `[1]`;
    - a duplicate;
    - a symbol that is not a venue symbol;
    - `["btc/usdt:usdt"]` when only `BTC/USDT:USDT` is configured.
  - `test_symbols_checked_when_disabled` (`enabled = false`, `symbols = []`)
  - `test_the_cadence_limit_uses_the_listed_count`: a 3 000-symbol venue with a list of 200 at 400 is accepted; `"all"` is refused, naming 2 976.
- [ ] **Step 2:** Run `uv run pytest tests/config -q`. Expected: FAIL.
- [ ] **Step 3:** Implement, and update all fixtures and the shipped files.
- [ ] **Step 4:** Run `uv run pytest tests/config tests/test_entrypoints.py tests/api tests/collector -q`. Expected: PASS.
- [ ] **Step 5:** Add the tests to INV-087's evidence. Commit `feat(config): required [open_interest].symbols, "all" or a list (ADR-0003 A-6)`.

### Task 3: Collector — listed symbols and atomic retry tickets

**Files:**
- Modify: `src/quasar_market_data/collector/cli.py` (`_open_interest_run`: `symbols=table.symbols`)
- Modify: `src/quasar_market_data/collector/open_interest_task.py` (`_RoundState`, `_round`, `_symbol`, `OpenInterestSymbolOutcome`)
- Modify: `tests/collector/open_interest_task_fakes.py`. Add `fail_symbols: set[str]`, which makes the first request of each listed symbol per range end raise `SourceError`. Add `fail_delay_ms: int = 0`, the time a failing request holds its slot before raising.
- Test: new `tests/collector/test_open_interest_retry_reserve.py`; `tests/collector/test_cli_run_open_interest.py`

**Interfaces:**
- Consumes: `retry_reserve` (Task 1) and `OpenInterestSettings.symbols` (Task 2).
- Produces:
  - `OpenInterestSymbolOutcome.RETRY_RESERVE_EXHAUSTED = "retry_reserve_exhausted"`;
  - `_RoundState` gains `retry_limit: int`, `retries_taken: int = 0` and the method `try_take_retry() -> bool`. The method is a plain synchronous function: if `retries_taken < retry_limit`, it increments and returns `True`; otherwise it returns `False`. It never decrements.
  - `_round` (where the state is built, `open_interest_task.py:638-641`) passes `retry_limit = retry_reserve(len(symbols of the round))`.
  - Placement in `_symbol`, on `attempt == 2`: inside `async with semaphore`, after the paused/stop check and before `_collect`.
  - When `try_take_retry()` returns `False`: set the outcome to `RETRY_RESERVE_EXHAUSTED`, emit `oi-warning symbol-not-retried boundary=… symbol=… <problem>`, and return.

- [ ] **Step 1: Write the failing tests.** Use the rig with `count_budget_in_source=True`.
  - `test_sixty_failures_take_fifty_tickets`: `n = 200` and `fail_symbols` holds 60 symbols. Expect exactly 50 retry calls, 10 `RETRY_RESERVE_EXHAUSTED` outcomes, and `retry_reserve_exhausted=10` in the oi-round line.
  - `test_ten_failures_are_all_retried`
  - `test_tickets_are_atomic_under_concurrency`: all 200 symbols fail at the same instant. The source does `await asyncio.sleep(0)` after the limiter grant and before raising. Expect `retries_taken == 50`, 50 retry calls, 150 exhausted outcomes, and at most `200 + 50 + 1` budget starts in the window.
  - `test_try_take_retry_is_synchronous`: `not inspect.iscoroutinefunction(_RoundState.try_take_retry)`; it returns `True` 50 times, then `False`.
  - `test_pause_refusal_and_absence_take_no_ticket`
  - `test_a_busy_write_takes_a_ticket`
  - `test_a_ticket_taken_before_a_deadline_cancel_stays_taken`
  - `test_a_new_round_starts_with_no_tickets_taken`
  - In `test_cli_run_open_interest.py`: `test_the_task_gets_the_listed_symbols`. A venue of 10 symbols with a list of 3 must build `OpenInterestTaskConfig.symbols` equal to the list. This is INV-147's negative evidence.
- [ ] **Step 2:** Run `uv run pytest tests/collector/test_open_interest_retry_reserve.py tests/collector/test_cli_run_open_interest.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/collector -q`. Expected: PASS.
- [ ] **Step 5:** Flip INV-146, INV-147 and INV-148 to ENFORCED. Commit `feat(collector): listed open interest symbols and an atomic shared retry reserve (ADR-0003 A-6)`.

### Task 4: Pace rule and request durations (D-7, D-6)

**Files:**
- Modify: `src/quasar_market_data/collector/open_interest_task.py` (`_previous_round_if_on_schedule`)
- Modify: `src/quasar_market_data/collector/open_interest_measurements.py` (per-request duration)
- Test: `tests/collector/test_open_interest_task_catchup.py`, `tests/collector/test_open_interest_measurements.py`

**Interfaces:**
- Consumes: `pace_limit_ms` (Task 1).
- Produces:
  - `_previous_round_if_on_schedule(wake_ns)` returns the span only when the round began on schedule (as today) **and** `span[1] − span[0] ≤ pace_limit_ms(n_prev) × 1e6`. `n_prev` is the previous round's symbol count, stored next to `_last_round_span`.
  - The hourly measurement line gains `request-ok-ms=p50/p99/max` and `request-failed-ms=p50/p99/max`. The durations are measured around `fetch_open_interest_hist` on the monotonic clock, for rounds and pages.

- [ ] **Step 1: Write the failing tests.** Use `count_budget_in_source=True` and `round_delay_ms=480`.
  - `test_an_on_pace_previous_round_delays_the_next_by_at_most_5_s`: saturate the backfill with prefilled holes. Expect the next round's last start at no more than its natural time plus 5 s.
  - `test_a_slow_previous_round_is_counted_in_full`: the previous round gets 6 failures at `fail_delay_ms=10_000`, making its span longer than 60 s. Expect the following idle window to leave `round_cost` free and the next round's starts never to wait.
  - `test_a_slow_round_does_not_propagate`: after one slow round, the next two rounds run on a healthy network. Expect both spans to be at most 60 s.
  - `test_measurement_line_reports_request_durations`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/collector -q`. Expected: PASS.
- [ ] **Step 5:** Flip INV-151 to ENFORCED. Commit `fix(collector): a slow previous open interest round keeps its budget starts; request durations measured (ADR-0003 A-6)`.

### Task 5: Latency simulation (spec §2.6, verification 1)

**Files:**
- Create: `tests/collector/test_open_interest_latency.py`

**Interfaces:**
- Consumes: the rig with `count_budget_in_source=True`, `round_delay_ms=480`, `fail_symbols`, `fail_delay_ms` and `late` (from Task 3 and the existing fakes).
- Each test's docstring says: "simulation on a fake clock: a measurement of the code on a model of the network, not of Binance".

Model: `S = 200`, `b = 400`, `round_offset_ms = 200 000`. "Readable" means the writer's stamp plus `READABLE_BOUND_MS`. Late-path cases publish the **last symbol in sorted order** at a time between `e + 260 s` and `e + 490 s`, and assert on that symbol.

- [ ] **Step 1: Write the tests**, one per line of the spec §2.6 table:
  - `test_ideal_early_readable_by_250_s`
  - `test_ideal_late_readable_by_550_s`
  - `test_fast_failures_any_count_readable_by_265_and_565_s` (`fail_delay_ms=500`, `F = 60` and `F = 200`)
  - `test_five_slow_failures_keep_the_late_path_by_600_s` (`F = 5`, `fail_delay_ms=10_000`)
  - `test_ten_slow_failures_miss_the_late_path` (row absent before `e + 600 s`, present by `e + 660 s`)
  - `test_sixty_slow_failures_reach_the_deadline_and_the_next_round_serves_early_rows`
  - `test_reserve_exhausted_row_comes_from_the_next_round_by_567_s`
  - `test_exhausted_and_next_request_failed_is_absent_before_800_s`
  - `test_saturated_backfill_after_an_on_pace_round_readable_by_567_s` (gated with Task 4)
  - `test_storing_round_starting_30_s_late_readable_by_600_s`
  - `test_untrusted_round_time_defers_rows_to_the_next_round`
- [ ] **Step 2:** Run `uv run pytest tests/collector/test_open_interest_latency.py -q`. If a bound fails, the estimate is wrong. **Do not loosen the bound.** Record the simulated value, stop, and report it to the user with the spec table line. The spec changes only with the user's agreement.
- [ ] **Step 3:** If the file takes more than 10 s, mark it `@pytest.mark.simulation` and run it in `make simulate`. Commit `test(collector): open interest latency simulation at S = 200, b = 400 (ADR-0003 A-6)`.

### Task 6: API — rule, `configured` and `collected`

**Files:**
- Modify: `src/quasar_market_data/api/open_interest_rule.py` (`len(table.symbols)`)
- Modify: `src/quasar_market_data/api/app.py` (the `create_app` parameter, `_open_interest_health`, a shared private judge, `_open_interest_collected`)
- Modify: `src/quasar_market_data/api/cli.py`, `tests/api/bench_server.py`, `tests/api/bench_health_open_interest.py`, and the `oi_object` / app helpers in `tests/api` that call `create_app`
- Test: `tests/api/test_open_interest_rule.py`, `tests/api/test_open_interest_health.py`; golden under `tests/api/golden/`

**Interfaces:**
- Consumes: `OpenInterestSettings.symbols`.
- Produces:
  - `create_app(..., open_interest_symbols: frozenset[tuple[str, str]])` is **required**, with no default. It holds the `(venue, symbol)` pairs of the file and is empty when open interest is disabled.
  - `_open_interest_health(facts, as_of_ms, rule, open_interest_symbols)` sets `configured = (venue, symbol) in open_interest_symbols`, with no period check (spec §3).
  - `_open_interest_collected(...) -> dict[str, object]` returns exactly the keys of the spec §2.4 example, in that order.
  - `collected` is the **last** key of `open_interest`.
  - The counting, the status and the `behind` order live in one private function shared by both objects.

- [ ] **Step 1: Write the failing tests**
  - `test_the_api_and_the_collector_derive_the_same_cadence_from_a_list` (INV-090)
  - `test_an_unlisted_venue_symbol_is_not_configured`
  - `test_all_keeps_configured_as_before`. Update `test_a_retired_series_is_listed_as_not_configured_and_counted` only by passing the set explicitly; its expectations stay.
  - `test_retired_series_fill_the_outer_object_but_not_collected` (200 listed and current, 322 retired and stale)
  - `test_a_stale_listed_series_leads_collected_behind`
  - `test_never_stored_counts_listed_symbols_without_a_configured_period_row`. Include a listed symbol whose only series is `15m`. Assert `series_count + never_stored == symbol_count` and `series_count == current + due + stale + unjudged_series`.
  - `test_a_conflict_on_a_retired_series_counts_only_outside`
  - `test_outer_fields_with_all_equal_the_pre_a6_golden`
  - `test_all_makes_collected_equal_the_outer_counts` (all series with the venue, a venue symbol and the period)
  - `test_collected_is_absent_when_open_interest_is_null`
- [ ] **Step 2: Capture the golden** before changing `app.py`.
  - Run `git worktree add ../qmd-main main` and use the seeding of `tests/api/test_health_universe.py` (the `golden_database` pattern) with a fixed seed and `as_of_ms`. Use only venue symbols with the configured period, and no `symbols` key.
  - Write `/health/ingest`'s `open_interest` object to `tests/api/golden/open_interest_health_pre_a6.json`.
  - Then run `git worktree remove ../qmd-main`.
- [ ] **Step 3:** Run the test files. Expected: FAIL.
- [ ] **Step 4:** Implement.
- [ ] **Step 5:** Run `uv run pytest tests/api -q`, then `make bench-health-oi`. Expected: PASS. If the bench is more than about 10 % slower than the number recorded in the OI-6 design §14, report it to the user.
- [ ] **Step 6:** Flip INV-149 to ENFORCED. Commit `feat(api): open interest rule, configured and collected from the listed symbols (ADR-0003 A-6)`.

### Task 7: `open_interest_period` in `/v1/instruments`

**Files:**
- Modify: `src/quasar_market_data/api/app.py` (the `instruments` handler, `_instrument_body`)
- Test: `tests/api/test_instruments_endpoint.py`

**Interfaces:**
- Consumes: Task 6's `open_interest_symbols` and `rule` (`None` when disabled).
- Produces: `_instrument_body(item, open_interest_period: str | None)`. The value is `rule.period.label` when `rule is not None` and the pair is in the set, and `None` otherwise. It is the last key of each entry.

- [ ] **Step 1: Write the failing tests:**
  - `test_listed_symbols_carry_the_period_on_every_timeframe`
  - `test_unlisted_symbols_carry_null`
  - `test_disabled_or_absent_open_interest_gives_null_everywhere`
  - `test_order_and_other_fields_unchanged`
- [ ] **Step 2:** Run the file. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the file. Expected: PASS. Flip INV-150 to ENFORCED.
- [ ] **Step 5:** Commit `feat(api): open_interest_period per instrument (ADR-0003 A-6)`.

### Task 8: The shipped list of 200 symbols (D-3)

**Files:**
- Modify: `config/quasar.toml`, `config/quasar.compose.toml`
- Test: `tests/config/test_compose_config.py`

- [ ] **Step 1: Write the failing test** `test_shipped_open_interest_list`. Both files must list the same 200 symbols, sorted, each a venue symbol, with `c = 1` at `b = 400`.
- [ ] **Step 2:** Choose the list with a throwaway script in the session scratchpad, not in the repo.
  - Load the markets with CCXT `binanceusdm().load_markets()`. Keep the venue symbols of `config/quasar.toml` that are linear USDT perpetual swaps.
  - Fetch the public 24 h tickers once (`fetch_tickers`, no credentials).
  - Map each ticker to its symbol through the CCXT market (`id` ↔ `symbol`, which handles ids like `1000PEPEUSDT`), not by string slicing.
  - Take the top 200 by `quoteVolume` as `Decimal`, breaking ties by symbol.
  - Write the list into both files. Record the snapshot time and the rule in the ADR's A-6 entry, without the volumes.
- [ ] **Step 3:** Run `uv run pytest tests/config -q`. Expected: PASS.
- [ ] **Step 4:** Commit `feat(config): shipped open interest subset of 200 symbols by 24 h quote volume (ADR-0003 A-6)`.

### Task 9: Overview tool (D-5: the API field)

**Files:**
- Modify: `tools/symbol_overview/model.py`:
  - `InstrumentRow.open_interest_period: str | None = None`;
  - a separate `OI_STATE_ORDER` that includes `not_collected`. `STATE_ORDER` stays unchanged for the candle summary (`judge.py:31`, `:144`).
- Modify: `tools/symbol_overview/validate.py`. In `instruments_result`, an absent key means `None`, so the tool still works with a pre-A-6 API. A present key must be `None` or a string, otherwise the result is `schema`.
- Modify: `tools/symbol_overview/refresh.py` (no open interest coverage request for `None`) and `tools/symbol_overview/judge.py` (state `not_collected`).
- Modify: `tools/symbol_overview/page.html`. Add `not_collected` to `isProblem` (false), `RANK_ORDER` and `LEVELS` (neutral), and give it the label "not collected" (`:343-398`).
- Modify: `tools/symbol_overview/detail_refresh.py`. The detail view uses `open_interest_period` to show "not collected" instead of the `unknown_series` path (`:59`).
- Test: `tests/tools/symbol_overview/test_oi_validate.py`, `test_oi_refresh.py`, `test_oi_judge.py`, `test_oi_page.py`, `test_detail_refresh.py`, `test_page_dashboard.py`

- [ ] **Step 1: Write the failing tests:**
  - validate: an absent key gives `None`; `"5m"` is kept; `5` gives `schema`;
  - refresh: no coverage request is made for `None`;
  - judge: the state is `not_collected`, and the candle summary keys are unchanged;
  - page: the state is not a problem and not red;
  - detail: shows "not collected".
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement. Stage `refresh.py` with `git add -p`, A-6 hunks only.
- [ ] **Step 4:** Run `uv run pytest tests/tools -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(overview): open interest not collected for unlisted symbols (ADR-0003 A-6)`.

### Task 10: Whole-branch verification, review, PR

- [ ] **Step 1:** Run `make check`. Expected: lint, typecheck, imports and test all pass. If Task 5 is marked `simulation`, also run `make simulate`. Expected: PASS.
- [ ] **Step 2:** Run the independent implementation review: the `code-review` skill, or a read-only Opus agent reviewing `git diff main...HEAD` against the spec and ADR-0003 A-6. Apply every finding, then re-run `make check`.
- [ ] **Step 3:** Check for leftovers:
  - `rg -n "venue\.symbols|len\(settings\.venues\[0\]\.symbols\)" src` shows no open interest use;
  - `rg -n "1 788|1788" docs src` shows no stale figure;
  - `rg -n "retry-reserve-exhausted" .` shows nothing.
- [ ] **Step 4:** Push the branch and open the PR. The title and body are the squash commit message. The body states which reviews ran and what they found, the gates and how they were decided, and what the soak measurement will check after deployment (spec §2.6, verification 2). No tag, no version bump.
- [ ] **Step 5:** Offer the user the reply to Sentaris, from spec §7, as a separate step after the PR.
