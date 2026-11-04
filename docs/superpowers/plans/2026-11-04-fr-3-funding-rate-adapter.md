# FR-3 Funding Rate Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Binance USDT-M funding rate history source on its **own** CCXT client with the request rules of ADR-0004 P-5, a local limiter (305 s window, class caps), `429`/`418` handling through one shared `ExtensionPause`, and the `fetch-funding-once` command (issue #18).

**Architecture:** Three small collector modules and one orchestration module, mirroring the open interest ones (OI-3): `extension_pause.py` (the object P-9 names), `funding_rate_limiter.py` (the sliding window with class caps R and C+H), `binance_usdm_funding_rate.py` (the adapter: pause check → in-flight slot → limiter → pause check → raw `fapiPublicGetFundingRate`), `fetch_funding_rate_once.py` (server time → range → raw page → FR-1 parser → FR-1 eligibility → FR-2 writer without a `merge`). The CLI wires them. No scheduler, no task, no config table (FR-4).

**Tech Stack:** Python 3.12, CCXT 4.5.89 async (`binanceusdm`, implicit endpoint `fapiPublicGetFundingRate`), asyncio, pytest, mypy strict, ruff, import-linter.

**Spec:** `docs/adr/0004-funding-rate.md` (P-1, P-3, P-4, P-5, P-9, P-15 rows marked FR-3 or FR-3/FR-4, P-16), FR-1 (`domain/funding_rate.py`, `domain/funding_rate_capacity.py`, `collector/binance_funding_rate.py`), FR-2 (`collector/async_funding_rate_writer.py`). Pattern to mirror: `collector/binance_usdm_open_interest.py`, `open_interest_gate.py`, `open_interest_budget.py`, `fetch_open_interest_once.py`, `tests/collector/test_binance_usdm_open_interest.py`, `test_open_interest_gate.py`, `test_open_interest_budget.py`, `test_fetch_open_interest_once.py`, `test_cli_fetch_oi_once.py`, `docs/superpowers/plans/2026-11-02-oi-3-open-interest-adapter.md`.

## Global Constraints

- Branch `fr-3-funding-rate-adapter` from `main`; one commit per task with the attribution trailer of the executing agent; the PR is squash-merged (title = conventional commit subject, body = squash message); never merge without the user's word.
- **Candle and open interest behaviour and code do not change.** `binance_usdm.py`, `rate_limiter.py`, `shared_server_time.py`, `collection_loop.py`, `fetch_once.py`, the candle writers, `binance_usdm_open_interest.py`, `open_interest_*.py` and the stdout of `fetch-once`, `fetch-oi-once` and `run` stay as they are. The only changes to existing files are `cli.py` (a new subcommand and an injectable opener, additive), `pyproject.toml` (import contracts), `README.md`, `docs/reference.md`, `docs/invariants.md`.
- Funding code never imports the candle `RateLimiter`, `SharedServerTime` or the open interest modules; candle modules never import `ExtensionPause` or any funding module (import-linter, Task 6).
- `ccxt` is imported by `binance_usdm_funding_rate.py` only (added to the allowlist of the existing contract).
- No schema, contract, config-key or API change. FR-3 touches no database except through the existing FR-2 writer in `fetch-funding-once`.
- Fail closed; a request that breaks a P-5 rule is refused before the pause, the limiter and the network are touched; an unknown or non-perpetual symbol is never requested.
- TDD: failing test first; record RED and GREEN output; `make check` before each commit (a PreToolUse hook also runs it). Run `uv run ruff format src tests tools spikes` (never a bare `ruff format`, which reformats unrelated markdown). Tests use fakes, `tmp_path` and a fake clock; no network.

## Decisions this plan makes (not fixed by the ADR)

- **`ExtensionPause` is the object P-9 names and is a new class, not the open interest gate renamed.** It has the same semantics as `OpenInterestGate` (429: pause `max(Retry-After, min(300 s · 2^(n−1), 1 h))`, a success after the pause resets the doubling, a `Retry-After` above one hour is honoured; 418: stop for the process run; a request in flight when a 429 arrived cannot reset the doubling; `check()` raises at once instead of waiting), but it exposes the two facts P-9 lists (`paused_until`, `stopped`) and is dataset-neutral (`ExtensionPaused`, `ExtensionStopped`, both `SourceError`). The open interest task keeps its own gate until amendment A-3 (#21) moves it onto this class; FR-3 does not edit open interest.
- **The limiter is a new class, `FundingRateLimiter`, not the FIFO `OpenInterestBudget`.** P-9 needs the 305 s left-open window (a start at `s` counts while `t − 305 s < s`, so request `k + b` of a saturated stream starts exactly 305 s after request `k`) and two class caps that are never lent: R ≤ `m = floor(b / 2)`, C and H together ≤ `b − m`. `RequestClass` has three members (`R`, `C`, `H`); C and H share the second cap (their alternation is the FR-4 scheduler's rule). Each cap has its own FIFO waiter, so a saturated class never blocks the other. `b` is validated with the FR-1 `round_share` / `history_catchup_share` (50..250). The sleep and clock are injected.
- **The adapter's request methods take the class.** `fetch_server_time(request_class=RequestClass.R)` and `fetch_funding_rate_history(symbol, start_time_ms, end_time_ms, *, request_class)`. The server-time call and seed/round requests are R (P-9); catch-up is C, history H. FR-3 only passes them through to the limiter.
- **Request validation is in the adapter, before any pause, slot, limiter or network use:** both bounds `ensure_timestamp_ms`; `start ≤ end` (Binance answers `200 []` for the reverse, P-5 rule 5, which would be read as an empty answer); the symbol must pass `usdm_market_id` (active USDT-margined linear perpetual, ADR-0001 P-9) so a typo or a delisted contract never reaches `200 []`; `limit` is always `FUNDING_RATE_PAGE_LIMIT` and `startTime` is always sent. A violation raises `FundingRateRequestRefused(SourceError)`.
- **The response is returned untouched** (a `list`; anything else is a `SourceError`, as the OI adapter) for the FR-1 parser. The adapter does not parse.
- **CCXT retry check (P-9 [P]):** CCXT 4.5.89 `fetch2` retries only when the option `maxRetriesOnFailure` is set (default 0). A test drives a failing raw call through a fake transport and asserts exactly one transport call, and a second test asserts the freshly opened client carries no such option.
- **`fetch-funding-once` stores rows and claims no coverage.** It never calls `merge`: a row outside or inside the span claims nothing (P-7). It observes server time on the funding source, takes the lower bound at the instant the request is sent, requests `[lower_bound − hours·1h, forward_end_bound_ms(delay, lower_bound)]` (`forward_end_bound_ms`, FR-1) as **one** page of class R, parses all-or-nothing, selects eligible rows (FR-1), and writes them through the FR-2 async writer. `--hours` is 1..500 (default 24); a full page (`n == 1000`) is reported as `full_page=yes` and is still valid (the command never says the range is complete). With untrusted time no request is made (as OI-3), exit `1`. A defensive `pending` row (which a valid response cannot contain) makes the exit code `3`. Exit codes otherwise as `fetch-oi-once` (`0`, `3` on conflicts, `1`, `4` for an unknown or partial write, `130`, `2`).
- **The command prints** `symbol=… start_ms=… end_ms=… fetched=…`, `time=trusted|untrusted reason=…`, `inserted=… duplicates=… conflicts=… pending=… off_hour_rows=… full_page=…` (the off-hour count is FR-1's heuristic, counted only).
- **`budget_per_5min` for the command is the default 150** (`DEFAULT_BUDGET_PER_5MIN` from `domain/funding_rate_capacity`); the configuration key is FR-4. A fresh limiter has an empty window, so it never delays a one-shot command.
- **README states the operational requirement** (nothing else on the IP may call `fundingRate` or `fundingInfo` more than `500 − b` times per 5 minutes) and the new command; `docs/reference.md` gets one paragraph on the modules.

## Review Focus

1. A saturated stream at `b = 50, 150, 250` never starts more than `b` requests in any 305 s interval, with R, C and H competing; request `k + b` starts at exactly `305 000 ms` after request `k` (not `304 999`); R never exceeds `m`, C+H never exceed `b − m`; a saturated class does not delay the other (Task 2).
2. After a `429` no request starts until `max(Retry-After, 300 s)` has passed, repeated `429`s double up to 1 h, earlier starts still count after the pause, a waiting request sends nothing, and a `418` stops the run; candle requests (a separate client) continue (Tasks 1, 3).
3. No request is made without `startTime`, with `limit` other than 1000, with `start > end`, or for a symbol that is not a verified USDT-M perpetual; a refused request uses no pause check, slot, limiter start or network call (Task 3).
4. A failing raw call is issued once (CCXT does not retry), a `429` on the server-time request counts as the same IP limit, and a Retry-After header is only read for an HTTP 429 (Task 3).
5. `fetch-funding-once` with untrusted time makes no request; a `200 []` answer stores nothing and is not an error; a row outside the range, a duplicate key or an error object is a parse error with nothing stored; a repeated run is idempotent; the stored rows never include one with `T + delay > lower_bound` (Task 4).

---

### Task 1: `ExtensionPause`

**Files:**
- Create: `src/quasar_market_data/collector/extension_pause.py`, `tests/collector/test_extension_pause.py`

**Interfaces:**
- `class ExtensionPaused(SourceError)` with `.remaining_ms: int` (rounded up) and `.until_monotonic_ns: int`; `class ExtensionStopped(SourceError)`.
- `class ExtensionPause(clock: Clock, logger: logging.Logger | None = None)`: `paused_until_ns: int | None` and `stopped: bool` (read-only properties, the two facts of P-9), `generation: int`, `check() -> None` (raises `ExtensionStopped`, else `ExtensionPaused` while a pause runs), `paused() -> ExtensionPaused`, `record_rate_limited(retry_after_ms: int) -> int` (ms of pause in force from now), `record_success(started_generation: int) -> None`, `record_banned() -> None` (logs one error line).

Semantics are those of `OpenInterestGate` (the module docstring says so and names it); the constants `_BASE_PAUSE_MS = 300_000`, `_MAX_DOUBLED_PAUSE_MS = 3_600_000`, `_MAX_DOUBLINGS = 10` are copied.

- [ ] **Step 1: Write failing tests** (adapt `test_open_interest_gate.py`, fake clock): both signals are `SourceError`; an untouched pause lets requests through; first 429 pauses `max(Retry-After, 300 s)` for Retry-After 0, 10 s, 300 s, 900 s; repeated 429 double 300, 600, 1 200, 2 400, 3 600, 3 600 s; a 429 inside a running pause is the same pause (the doubling advances once; a longer Retry-After extends it, a shorter one does not shorten it); a success of a request that started in the current generation after the pause resets the doubling, one that started earlier does not; `check` raises `ExtensionPaused` with `remaining_ms` rounded up and the end on the gate's clock until the pause has passed, then passes; `record_banned` stops for good, logs once, and `check` raises `ExtensionStopped` even after a would-be pause expired; `paused_until_ns` and `stopped` reflect the state; argument checks (`bool`, negative, float → `TypeError`/`ValueError`).
- [ ] **Step 2: Run** `uv run pytest tests/collector/test_extension_pause.py -q`. Expect FAIL.
- [ ] **Step 3: Implement.** **Step 4: Run** the tests and `make check`. Expect PASS.
- [ ] **Step 5: Commit** `feat(collector): ExtensionPause, the shared stop after 429/418 for extension datasets (ADR-0004 P-9)`.

### Task 2: `FundingRateLimiter`

**Files:**
- Create: `src/quasar_market_data/collector/funding_rate_limiter.py`, `tests/collector/test_funding_rate_limiter.py`

**Interfaces:**
- `class RequestClass(Enum)`: `R`, `C`, `H`.
- `class FundingRateLimiter(budget_per_5min: int, clock: Clock, sleep: Sleep)`: validates `b` (`TypeError` for a non-`int`/`bool`, `ValueError` outside 50..250, via `round_share`); `m = round_share(b)`, `other = history_catchup_share(b)`; `async acquire(request_class: RequestClass) -> None`; `starts_in_window(at_ns: int) -> int` (read-only, for the hourly line of FR-4 and for tests); constants `WINDOW_MS = LIMITER_WINDOW_MS` (305 000).

Semantics: a start of class X may be granted at `t` iff fewer than the class cap of X's starts lie in `(t − 305 s, t]`; the cap of R is `m`, the cap of C and of H together is `b − m`. Starts are recorded at grant time from `clock.monotonic_ns()`. Two waiters (one per cap group) so a full group never blocks the other; within a group a FIFO `asyncio.Lock`; a sleeper sleeps until the oldest counted start of its group leaves the window (`s + 305 s`, rounded up to a whole millisecond), re-reads the clock and re-checks, so an early or late wake-up cannot over-grant; a cancelled waiter consumes nothing.

- [ ] **Step 1: Write failing tests** (`tests/collector/manual_time.py` `ManualTime`, as `test_open_interest_budget.py`):
  - `test_boundary_request_k_plus_b_starts_exactly_305_s_after_request_k` for `b` 50, 150, 250 and class R (m starts at 0 ms, the next at `305 000 ms`, not `304 999`); the same for C and for H; for a mixed C and H stream (together `b − m`).
  - `test_r_never_exceeds_m_and_c_plus_h_never_exceed_the_rest` (saturating each group alone and both together).
  - `test_the_two_groups_do_not_borrow_from_each_other` (R saturated: a C request still starts at once; C+H saturated: an R request still starts at once; each cap is its own).
  - `test_property_no_interval_of_305_s_holds_more_than_b_starts` (seeded `random.Random`, 6 seeds, 300 requests with random classes, random inter-arrival gaps including 0 and gaps of exactly 305 000 ms and 304 999 ms; after the run every window `(t − 305 s, t]` over the recorded starts holds ≤ `b`, R ≤ `m`, C+H ≤ `b − m`).
  - `test_a_cancelled_waiter_consumes_nothing`; `test_fifo_within_a_group`; `test_an_early_wakeup_cannot_over_grant`; `test_starts_in_window_counts_left_open` (`at_ns` exactly 305 s after a start no longer counts it).
  - `test_budget_validation`: 49, 251, `"150"`, `150.0`, `True` refused at construction; `test_request_class_is_checked`.
- [ ] **Step 2: Run.** Expect FAIL. **Step 3: Implement.** **Step 4: Run** the tests and `make check`. Expect PASS.
- [ ] **Step 5: Commit** `feat(collector): funding rate limiter with the 305 s window and class caps (ADR-0004 P-9)`.

### Task 3: `BinanceUsdmFundingRateSource`

**Files:**
- Create: `src/quasar_market_data/collector/binance_usdm_funding_rate.py`, `tests/collector/test_binance_usdm_funding_rate.py`

**Interfaces:**
- `MAX_IN_FLIGHT: Final = 2`; `class FundingRateRequestRefused(SourceError)`.
- `class BinanceUsdmFundingRateSource(client, inner: BinanceUsdmSource, limiter: FundingRateLimiter, pause: ExtensionPause)`: `@classmethod async open(limiter, pause, clock=None, exchange=None)` (a **new** `binanceusdm` unless `exchange` is passed; `BinanceUsdmSource.open` checks `fapiPublic` and loads markets and closes on failure); `market_id(symbol) -> str`; `async fetch_server_time(request_class: RequestClass = RequestClass.R) -> ServerTimeObservation`; `async fetch_funding_rate_history(symbol: str, start_time_ms: int, end_time_ms: int, *, request_class: RequestClass) -> list[object]`; `async close()`.
- Re-exports `ExtensionPaused`, `ExtensionStopped` in `__all__`.

Semantics: every request passes `pause.check()` → one of at most 2 in-flight slots → `limiter.acquire(class)` → `pause.check()` → the CCXT call, as `BinanceUsdmOpenInterestSource._request` (slot before the limiter; a call that waited sends nothing into a pause). `fetch_funding_rate_history` first validates (see Decisions), calls `self._client.fapiPublicGetFundingRate({"symbol": market_id, "startTime": start, "endTime": end, "limit": 1000})` and returns the list untouched (`SourceError` otherwise). 429/418 handling is copied from the OI adapter (`<id> 418 ` → `record_banned`; any other `DDoSProtection`/`RateLimitExceeded` → `record_rate_limited`, Retry-After only for `<id> 429 `, case-insensitive header, strict base-10 seconds); a 429 or 418 on the time request counts; `-1130` is an ordinary `SourceError`; a success records `record_success(generation)`.

- [ ] **Step 1: Write failing tests** (adapt `test_binance_usdm_open_interest.py`; `FakeExchange` from `test_binance_usdm.py`, `ManualTime`):
  - `test_request_carries_the_market_id_bounds_and_limit_1000_and_returns_the_raw_list` (params exactly `{symbol, startTime, endTime, limit: 1000}`, the returned object is the response object).
  - `test_start_time_is_always_sent_and_limit_is_always_1000` (several valid calls; every recorded call has both keys).
  - `test_refusals_use_nothing` for `start > end`, a non-`int` or `bool` bound, a bound outside the timestamp range, → `FundingRateRequestRefused` or `TypeError` as stated; the fake records no call, `limiter.starts_in_window(...) == 0`, `pause.check` never reached (a spy), no in-flight slot taken.
  - `test_an_unknown_symbol_and_a_non_perpetual_market_are_never_requested` (parametrized changes of `swap`, `linear`, `settle`, `quote`, `expiry`, a missing id; a symbol that is not in `markets`): no request, no limiter start (P-15 Request row, the unknown-symbol `200 []` is never reached).
  - `test_server_time_comes_from_the_funding_client_and_counts_in_r` (the limiter shows one R start); `test_history_class_counts_in_its_group` (R, C, H each increment the right group).
  - `test_at_most_two_requests_are_in_flight_time_included`; `test_a_cancelled_call_releases_its_in_flight_slot`; `test_the_in_flight_slot_is_taken_before_the_limiter`.
  - `test_429_and_the_other_rate_signals_pause_all_funding_requests` (parametrized `DDoSProtection` 429 text, `RateLimitExceeded`; both endpoints); `test_consecutive_429s_double_the_pause_and_a_success_resets_it`; `test_retry_after_is_read_case_insensitively_and_strictly`; `test_retry_after_is_only_read_for_an_http_429`; `test_418_stops_funding_for_the_run_on_both_endpoints`; `test_a_call_that_waited_in_the_limiter_sends_nothing_after_another_call_got_a_429`; `test_a_call_that_waited_for_a_slot_sends_nothing_after_a_429_or_418`; `test_after_the_pause_earlier_starts_still_count_in_the_limiter`; `test_an_exhausted_budget_makes_the_request_wait_and_it_sends_after_the_window`.
  - `test_a_response_that_is_not_a_list_is_a_source_error` (the HTTP-200 error object included); `test_other_failures_are_source_errors_and_do_not_pause`; `test_1130_is_an_ordinary_source_error`.
  - `test_ccxt_does_not_retry_a_failed_raw_call` (fake transport at CCXT's `fetch` level raising `ccxt.RequestTimeout`: exactly one transport call) and `test_a_fresh_client_has_no_max_retries_on_failure_option`.
  - `test_open_creates_its_own_client_each_time_and_never_the_candle_sources`; `test_close_closes_the_client`.
  - `test_candle_requests_continue_while_funding_is_paused_and_banned` (a candle `BinanceUsdmSource` on its own fake client is unaffected; the candle `RateLimiter` is never acquired by a funding call).
  - `test_the_module_imports_neither_the_candle_limiter_nor_shared_server_time_nor_open_interest` (AST scan, as the OI test).
- [ ] **Step 2: Run.** Expect FAIL. **Step 3: Implement.** **Step 4: Run** the tests and `make check`. Expect PASS.
- [ ] **Step 5: Commit** `feat(collector): funding rate history source on its own CCXT client (ADR-0004 P-1, P-5, P-9)`.

### Task 4: `fetch_funding_rate_once`

**Files:**
- Create: `src/quasar_market_data/collector/fetch_funding_rate_once.py`, `tests/collector/test_fetch_funding_rate_once.py`

**Interfaces:**
- `MAX_HOURS: Final = 500`, `DEFAULT_HOURS: Final = 24`.
- `class FundingRateOnceSource(Protocol)`: `market_id`, `fetch_server_time(request_class=…)`, `fetch_funding_rate_history(…, request_class)`, `close`.
- `class FundingRateSink(Protocol)`: `async write(rows, *, ingested_at_ms, merge=None) -> FundingRateWriteReport`.
- `@dataclass(frozen=True) FetchFundingRateOnceConfig(symbol: str, hours: int = DEFAULT_HOURS, publication_delay_ms: int = DEFAULT_FUNDING_RATE_PUBLICATION_DELAY_MS, time_trust: TimeTrustConfig = DEFAULT_TIME_TRUST)` with validation as `FetchOpenInterestOnceConfig`.
- `@dataclass(frozen=True) FetchFundingRateOnceReport(fetched: int, selection: FundingRateSelection, write: FundingRateWriteReport | None, start_ms: int | None, end_ms: int | None, off_hour_rows: int, full_page: bool)`.
- `async fetch_funding_rate_once(source, writer, config, clock) -> FetchFundingRateOnceReport`.

Order of work as in Decisions. A `SourceError` on the time request is "no observation" except `ExtensionPaused` / `ExtensionStopped`, which are re-raised. The writer is called without a `merge`.

- [ ] **Step 1: Write failing tests** (fakes as `test_fetch_open_interest_once.py`, `FakeClock`): the range and `forward_end_bound_ms` for several lower bounds (including a bound that makes the end land exactly on a settlement); the request class is R; untrusted time (no observation, rejected observation, a time request that raises `SourceError`) makes no history request and writes nothing and the report names the reason; `ExtensionPaused` / `ExtensionStopped` on the time request propagate; `200 []` stores nothing and is not an error; rows stored once and a repeated run is all duplicates; a Regular and a Special row of one hour (either order) are stored; a negative rate and a `""` mark price are stored as `-0.0001` and NULL; a differing re-delivery is a conflict in the report and the stored row is unchanged; a response with a row outside the range, a repeated key, a decreasing time or an error object raises `FundingRateParseError` and nothing is written; an off-hour row (30 min past) is stored and counted in `off_hour_rows`; `full_page` true at exactly 1 000 rows; a writer error propagates unchanged; no `merge` is passed and no row of the selection pending is written (a fake source that returns a row after the bound makes the parser or the selection keep it out: the stored rows never include `T + delay > lower_bound`); `hours` 0, 501, `True` refused.
- [ ] **Step 2: Run.** Expect FAIL. **Step 3: Implement.** **Step 4: Run** the tests and `make check`. Expect PASS.
- [ ] **Step 5: Commit** `feat(collector): fetch one page of funding rate history, eligible rows only (ADR-0004 P-4, P-5)`.

### Task 5: `fetch-funding-once` command

**Files:**
- Modify: `src/quasar_market_data/collector/cli.py`
- Create: `tests/collector/test_cli_fetch_funding_once.py`

**Interfaces:** `main(..., open_funding_source: FundingRateOpenSource = BinanceUsdmFundingRateSource.open)` (a new keyword with a default, so every existing caller is unchanged); parser subcommand `fetch-funding-once --db PATH --symbol SYMBOL [--hours 1..500] [--publication-delay-ms N]`; `_print_funding_rate_report`, `_run_funding_rate`, `_fetch_funding_rate_once`, `_funding_rate_page_failure` (exit codes as `fetch-oi-once`: a page error with at least one committed transaction or an unknown outcome → `4`, a certain failure → `1`, conflicts or a defensive pending row → `3`, untrusted time → `1`, `130` on interrupt). The module docstring of `cli.py` gets one paragraph. `_run_funding_rate` builds `FundingRateLimiter(DEFAULT_BUDGET_PER_5MIN, clock, sleep)`, `ExtensionPause(clock)`, opens the source and `AsyncFundingRateWriter.open(db, activity=CandleWriteActivity())`, migrates first (`_migrate`), closes both with `_close_quietly`.

- [ ] **Step 1: Write failing tests** (adapt `test_cli_fetch_oi_once.py`): output lines and exit `0` for a normal run; untrusted time → exit `1`, no request, nothing written; conflicts → `3`; a parse error → `1` with the error line and nothing written; a partial page failure → `4` with the incomplete-write message; usage errors (`--hours 0`, `--hours 501`, missing `--symbol`) → `2`; a missing database directory → `1`; the database is migrated to `SCHEMA_VERSION` and the migration line appears only in `run` (unchanged); `fetch-once` and `fetch-oi-once` output unchanged (their existing tests pass unedited); the command passes class R and no `merge` to the fakes; the exit code for `KeyboardInterrupt` is `130`.
- [ ] **Step 2: Run.** Expect FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest tests/collector/test_cli_fetch_funding_once.py tests/collector/test_cli_fetch_oi_once.py tests/collector/test_cli_fetch_once.py tests/collector/test_cli_run.py -q` and `make check`. Expect PASS.
- [ ] **Step 5: Commit** `feat(collector): fetch-funding-once command (ADR-0004 P-16 FR-3)`.

### Task 6: Import contracts, documentation, invariant register

**Files:**
- Modify: `pyproject.toml`, `README.md`, `docs/reference.md`, `docs/invariants.md`, `tests/test_import_contracts_negative.py` (a negative case per new contract, as the existing ones)

- [ ] **Step 1: Contracts.** (a) add `quasar_market_data.collector.binance_usdm_funding_rate -> ccxt` to the allowlist of `ccxt is imported only by the Binance adapter`; (b) new contract `funding rate is independent of candle limiter, time and open interest`: sources `binance_usdm_funding_rate`, `funding_rate_limiter`, `extension_pause`, `fetch_funding_rate_once`; forbidden `rate_limiter`, `shared_server_time`, `binance_usdm_open_interest`, `open_interest_budget`, `open_interest_gate`; (c) new contract `candle collection does not use extension datasets`: sources `collection_loop`, `fetch_once`, `binance_usdm`, `rate_limiter`, `shared_server_time`, `candle_writer`, `async_candle_writer`, `cli`'s candle path cannot be listed (`cli.py` wires everything), so the list names the candle modules; forbidden `extension_pause`, `binance_usdm_funding_rate`, `funding_rate_limiter`. Add one negative case per new contract to `tests/test_import_contracts_negative.py` in the style of the existing cases.
- [ ] **Step 2: README.** A "Funding rate: `fetch-funding-once`" paragraph with the command, its exit codes and the statement that it stores rows and claims no coverage; the operational requirement: nothing else on the IP may call `fundingRate` or `fundingInfo` more than `500 − b` times per 5 minutes (default `b = 150`, so 350), both deployments; and that a `429` or `418` on the funding source pauses or stops only funding (ADR-0004 P-9; until A-3 is applied open interest keeps its own gate).
- [ ] **Step 3: `docs/reference.md`:** one paragraph on `extension_pause.py`, `funding_rate_limiter.py`, `binance_usdm_funding_rate.py`, `fetch_funding_rate_once.py`.
- [ ] **Step 4: Register rows** from the next free id after INV-111, ENFORCED unless stated, evidence as `tests/...::test_name[case]`: funding request starts stay within the local budget in every 305 s window with the class caps (limiter tests); an unknown or non-perpetual symbol is never requested, and no request is made without `startTime`, with `limit` ≠ 1000 or with `start > end` (adapter refusal tests); a funding source sends nothing while `ExtensionPause` is set or stopped, a waiting request sends nothing after a `429`/`418` (adapter and pause tests); repeated `429` doubles the pause up to 1 h and `418` stops the run (pause tests); the funding source never uses the candle limiter or time and candle modules never use `ExtensionPause` (import contracts, with their negative cases); CCXT does not retry a failed funding call (adapter test; scope: CCXT 4.5.89 default options); `fetch-funding-once` makes no request on untrusted time and stores only eligible rows, claiming no coverage (INV-110 stays PLANNED for the FR-4 task, this row is scoped to the command).
- [ ] **Step 5: Run** `uv run pytest tests/test_invariant_register.py tests/test_import_contracts_negative.py -q`, `make imports` and `make check`. **Step 6: Commit** `docs: funding rate adapter contracts, README and invariants (ADR-0004 FR-3)`.

### Task 7: Implementation review and pull request

- [ ] **Step 1:** `make check` on the branch head; record the output.
- [ ] **Step 2:** Independent **code review** of `git diff main...HEAD` (fresh Opus agent, read-only) against ADR-0004 P-5 and P-9, this plan and issue #18; apply every finding with its own commit and a test that failed under the matching mutation; re-run `make check`.
- [ ] **Step 3:** Push and open the PR `feat: FR-3 funding rate source, limiter, ExtensionPause and fetch-funding-once`: summary, decisions, verification, both reviews, what is not covered, `Closes #18`.

## Review history

- (plan review and code review: to be filled)
