# OI-3 Open Interest Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An adapter that fetches raw Binance open interest history on its **own** CCXT client under the P-5 request rules and the P-10 budget (sliding window, 429/418), plus the `fetch-oi-once` CLI command (issue #3).

**Architecture:** Pure request rules in `domain` (no I/O). In `collector`: a sliding-window `OpenInterestBudget` (request starts over 300 s), an `OpenInterestGate` (429 pause with doubling, 418 stop), and `BinanceUsdmOpenInterestSource`, the second module that imports `ccxt`. It owns a `binanceusdm` client created by itself, takes the budget and the gate at construction, allows at most 2 requests in flight, and counts its own `fapiPublicGetTime` calls in the same budget. `fetch-oi-once` mirrors `fetch-once`: observe time, judge eligibility, fetch one range, parse (OI-1), select eligible rows (OI-1), write (OI-2). Nothing runs it continuously: the task, rounds and config are OI-4.

**Tech Stack:** Python 3.12, CCXT async 4.5.89, asyncio, pytest, mypy strict, ruff, import-linter.

**Spec:** `docs/adr/0003-open-interest.md` (P-1, P-5, P-9 rows "CCXT client", "Request limiter", "Server time", "Concurrency", P-10 "Budget" and "Other consumers", P-15 rows marked OI-3, P-16, amendment A-1). Patterns: `collector/binance_usdm.py`, `collector/rate_limiter.py`, `collector/fetch_once.py`, `collector/cli.py` (`_fetch_once`, `_run`), `collector/binance_open_interest.py` (OI-1 parser), `collector/async_open_interest_writer.py` (OI-2), `tests/collector/manual_time.py`, `tests/collector/test_binance_usdm.py`, `tests/collector/test_cli_fetch_once.py`.

## Global Constraints

- Branch `oi-3-open-interest-adapter` from `main`; one commit per task, message given in each task, the attribution trailer the executing session is given. The PR is squash-merged: its title is a conventional commit subject and its body reads as the squash commit message.
- **Candle behaviour does not change.** `RateLimiter`, `SharedServerTime`, the kline path of `BinanceUsdmSource`, `collection_loop.py`, the candle writer, the stdout and exit codes of `fetch-once` and `run` stay as they are. The only edit in candle code is renaming `_market_id` to `usdm_market_id` in `binance_usdm.py` so the new module can reuse the USDT-M perpetual check (identical body, its tests keep passing).
- The open interest source never calls the candle `RateLimiter`, `SharedServerTime` or the candle client (P-9). It does not touch the database (the CLI does, through the OI-2 writer).
- No config key, no API change, no schema change, no change of `docs/contracts/` (OI-4, OI-5). `budget_per_5min` is a constructor argument here; its validation (`100 ≤ b ≤ 400`) is the **config** error of OI-4. The budget class accepts any `b ≥ 1` so the property test can use small values, and the CLI uses the ADR default 400 (a constant `DEFAULT_BUDGET_PER_5MIN` in the new source module, replaced by the config in OI-4).
- Fail closed: a refused or failed request is a `SourceError` (or subclass); nothing is fabricated; a response is parsed all-or-nothing by the OI-1 parser.
- TDD: failing test first; record RED and GREEN output. A PreToolUse hook runs `make check` on `git commit`; run it before each commit and report the real output. Tests use `tmp_path` only and mocked exchange responses; no network in `make check`.

## Decisions this plan makes (not fixed by the ADR)

- **A range over 500 period ends is refused, not split** (ADR P-15 allows either; the issue leaves it open). Splitting needs a page plan, which is OI-4's catch-up; the adapter stays a one-request primitive. The refusal is a `SourceError` raised **before** any request or budget use.
- **`as_of_ms` is an argument of `fetch_open_interest_hist`** (the caller's server lower bound, P-5 horizon rule). The adapter does not read a clock to decide it. `start_time_ms < as_of_ms − H` is refused. `H = 29 days` is a domain constant. `end_time_ms > as_of_ms` is **not** refused: eligibility is judged by the caller after the response (OI-1), as for klines.
- **Pure rules live in `domain/open_interest_request.py`** (`validate_open_interest_request`), so OI-4 plans pages with the same function. It checks: both bounds are `int` timestamps aligned to the period, `start ≤ end`, `period_end_count ≤ MAX_PERIOD_ENDS (500)`, `start ≥ as_of − H`. The adapter sends `limit = period_end_count` (never above 500), so the response can hold exactly the range and truncation cannot happen even if Binance changed its rule.
- **Refuse versus pause versus ban are three `SourceError` subclasses** in the new source module: `OpenInterestRequestRefused` (rules, a caller bug or a too-old range), `OpenInterestPaused` (`until_monotonic_ns`, `remaining_ms`), `OpenInterestBanned`. A generic `except SourceError` still fails closed; OI-4 can special-case the last two (a pause is not a retry-reserve error).
- **A pause raises, it does not make the caller wait.** While paused, `fetch_*` raises `OpenInterestPaused` immediately without using budget. Waiting up to an hour inside a call would hold the in-flight slot and the round's deadline would cancel it anyway; the caller decides (skip the symbol, wait, stop).
- **429/418 are classified from CCXT's message, not from its class** (review C1, verified in CCXT 4.5.89): `binance.handle_errors` raises `ccxt.DDoSProtection("binanceusdm 429 <reason> <body>")` for **both** HTTP 429 and 418, and `RateLimitExceeded` does not inherit from `DDoSProtection`. The adapter matches the strict prefix `f"{exchange.id} 418 "` / `f"{exchange.id} 429 "` on the message: `418` is a ban; `429` is a pause. Any other `DDoSProtection` and any `ccxt.RateLimitExceeded` (body code `-1003`) is treated as a **pause** (fail closed: stop asking), with no `Retry-After`. Fakes in the tests raise exactly what CCXT raises (`DDoSProtection("binanceusdm 429 Too Many Requests {...}")`), plus the other shapes for the fallback rule.
- **429 pause length:** with `n` consecutive pauses (a success resets `n`), `pause = max(retry_after_ms, min(300 s × 2^(n−1), 3600 s))`. The doubling is capped at 1 h; a larger `Retry-After` is honoured fully (Binance's answer is the authority, P-10 point 2).
- **`Retry-After`** can only come from `exchange.last_response_headers` (CCXT puts nothing in the exception text); with 2 requests in flight it may belong to the other response, and the pause never goes below 300 s, so this is safe. The lookup is case-insensitive and strict: a base-10 integer of seconds `>= 0`, anything else counts as `0`. Fakes carry a `last_response_headers` attribute.
- **418:** `OpenInterestGate.record_banned()` is permanent for the process run; every later `fetch_*` raises `OpenInterestBanned` with no budget use; one error log line (stdlib `logging`) on the transition.
- **A 429/418 on `/fapi/v1/time` counts too** (the ADR names only the history endpoint; the time request is part of the OI budget and of the same IP limit, P-9/P-10, so it is a decision recorded here). `BinanceUsdmSource.fetch_server_time` wraps every `ccxt.BaseError` in a `SourceError` (`raise ... from error`), so the shared error helper inspects `error.__cause__` as well as the error itself.
- **The gate is checked again after the grant** (review H1): order is `gate.check()` → semaphore → `budget.acquire()` → `gate.check()` (synchronous, no `await` before the CCXT call). A call that waited for the semaphore or the budget while another got a 429/418 raises `OpenInterestPaused`/`OpenInterestBanned` and sends nothing (its start stays counted: conservative).
- **The `fapiData` base URL is validated at `open`** (review M2): `urls['api']['fapiData']` must be exactly `https://fapi.binance.com/futures/data` (same strictness as `fapiPublic`), else `SourceError` and the client is closed. `BinanceUsdmSource.open` validates only `fapiPublic`, which stays.
- **CCXT's own throttle** (`enableRateLimit=True`, `rateLimit=50`) can delay the network send after the budget grant; the budget counts adapter call starts, like `RateLimiter` (documented in the module docstring). The delay only spaces requests further, never closer.
- **In-flight cap:** an `asyncio.Semaphore(2)` is acquired **before** the budget, and the budget grant is followed at once by the CCXT call (no `await` between), as `RateLimitedSource` does. A start is counted when the call starts, not while it waits for the semaphore.
- **Own server time** (`fetch_server_time`) acquires the same semaphore and budget, then delegates to a `BinanceUsdmSource` built on the **same own client** (`BinanceUsdmSource.open(client, clock)` validates the base URL and loads the markets; no duplicated time code). The candle source is never involved: this is another instance, from another client.
- **`fetch-oi-once` shape:** `fetch-oi-once --db PATH --symbol S [--periods N] [--publication-delay-ms MS]`. It requests the `N` newest eligible period ends: `B = floor(lower_bound / 5 min) × 5 min`, range `[B − (N−1)·5 min, B]`, `N` in `1..500`, default 3. Rows not yet eligible (end + delay > bound) are pending and not stored. Untrusted time stores nothing and exits `1`, exactly like `fetch-once` (`cli.py` `_print_report`). Exit codes mirror `fetch-once`: `0` stored, nothing conflicting; `3` stored but conflicts found; `1` nothing written with certainty; `4` the outcome of some write is unknown **or a page failed after at least one transaction committed** (review H3: `AsyncOpenInterestWriter.write` raises `OpenInterestPageError` with `.cause`, `.committed_transactions`, `.partial`; the CLI unwraps it: `committed_transactions > 0` or `cause` an `OpenInterestWriteOutcomeUnknown` → `4` with a message naming the committed count, otherwise `1`); `130` interrupted; `2` usage. A 429/418 on the time request (the first request) or on the history request is reported as such, not as untrusted time, and exits `1` (implementation review M1).
- **CLI wiring** (review, low): `fetch-oi-once` creates a `CandleWriteActivity` (never entered, nothing else writes) for `AsyncOpenInterestWriter.open`. `main` gets a new keyword `open_oi_source: Callable[[Clock, Sleep], Awaitable[OpenInterestUseCaseSource]]`; the default builds `OpenInterestBudget(DEFAULT_BUDGET_PER_5MIN, clock, sleep)`, `OpenInterestGate(clock)` and calls `BinanceUsdmOpenInterestSource.open`. Tests inject a fake factory with `FakeClock`; `fetch-once` and `run` keep their `OpenSource`.
- **No new dependency, no new abstraction** beyond the three small classes above.

## Review Focus

1. A range of 501 period ends, an unaligned bound, a `start` older than `as_of − H` and `start > end` never reach the exchange fake (call count 0) and use no budget (Tasks 2, 5).
2. A fake that returns the **newest** `limit` rows of a longer range: since the request never exceeds `limit`, the adapter never sees truncation; a hypothetical longer request is shown to lose rows (control test) so the guard test can fail (Task 5).
3. The sliding window: in no 300 s interval more than `b` starts, with time, hist, retries and many concurrent tasks all competing; exact boundary behaviour (start at `t`, next allowed at `t + 300 s`) (Task 3).
4. 429 / 418: pause length, doubling, reset after success, 418 permanence, no budget use while paused or banned, and a candle source on another client continues (Tasks 4, 5).
5. Own client: the OI source's `ccxt` instance is not the candle source's; both modules are the only importers of `ccxt` (import-linter updated) (Tasks 5, 6).

---

### Task 1: Facts about CCXT 4.5.89 (spike, no production code)

**Files:** none committed except notes in the Task 5 module docstring.

- [ ] **Step 1: Read the installed CCXT** (`uv run python`, `inspect`) and answer, writing the answers into the docstring of Task 5:
  1. Which exception does CCXT raise for HTTP 429 and 418 on `fapiDataGetOpenInterestHist` (`ccxt.RateLimitExceeded`, `ccxt.DDoSProtection`) and how is the HTTP status available (exception attribute, text)?
  2. Is `Retry-After` available: `exchange.last_response_headers` after a failed request, or in the exception?
  3. Which exception for code `-1130` (expected `ccxt.BadRequest`)?
  4. The implicit method name `fapiDataGetOpenInterestHist` exists on the client and takes `{"symbol", "period", "startTime", "endTime", "limit"}` (print `exchange.fapiDataGetOpenInterestHist` and the `api['fapiData']['get']` entry).
  5. `enableRateLimit` default and `rateLimit` of the client (its own throttle queue is kept as is, P-9).
- [ ] **Step 2:** the plan already rests on these facts, verified in the plan review: 429 and 418 both arrive as `ccxt.DDoSProtection("binanceusdm <code> <reason> <body>")` (not `RateLimitExceeded`); `-1130` is `BadRequest`; `urls['api']['fapiData']` is `https://fapi.binance.com/futures/data`; `enableLastResponseHeaders` is true, `rateLimit` 50. If any re-check differs, **stop and report before Task 4**.

No commit.

### Task 2: Pure request rules

**Files:**
- Create: `src/quasar_market_data/domain/open_interest_request.py`, `tests/domain/test_open_interest_request.py`

**Interfaces:**
- `MAX_PERIOD_ENDS: Final = 500`, `RETRIEVAL_HORIZON_MS: Final = 29 * 86_400_000`.
- `class OpenInterestRequestError(ValueError)`.
- `validate_open_interest_request(start_ms, end_ms, period, as_of_ms) -> int` returns the number of period ends (= the `limit` to send); raises `OpenInterestRequestError` naming the violated rule; wrong types raise `TypeError`.

- [ ] **Step 1: Failing tests** (parametrised, one rule each):
  - 500 period ends accepted (returns 500); 501 raises `OpenInterestRequestError` (boundary on both sides).
  - `start`/`end` not aligned to 5m raises (each separately); `start > end` raises; `start == end` returns 1.
  - `start == as_of − H` accepted; `start == as_of − H − 5 min` raises (rule is `start ≥ as_of − H`); an `as_of` that makes `as_of − H` negative still compares correctly.
  - `bool`/`float`/`str` bounds raise `TypeError` (no silent conversion).
  - the domain stays pure (existing `tests/test_domain_purity.py` passes).
- [ ] **Step 2:** run, expect RED (module missing). **Step 3:** implement with `period_end_count` and `ensure_timestamp_ms`. **Step 4:** GREEN.
- [ ] **Commit:** `feat(domain): open interest request rules, at most 500 period ends and the 29-day horizon (ADR-0003 P-5)`

### Task 3: Sliding-window budget

**Files:**
- Create: `src/quasar_market_data/collector/open_interest_budget.py`, `tests/collector/test_open_interest_budget.py`

**Interfaces:**
- `WINDOW_MS: Final = 300_000`.
- `class OpenInterestBudget(budget_per_5min: int, clock: Clock, sleep: Sleep)`; `async acquire() -> None`. `budget_per_5min` is an `int ≥ 1` (`bool`, `float` rejected). One `asyncio.Lock` (FIFO) like `RateLimiter`. Keeps a `deque` of start instants (monotonic ns). A grant: drop instants `≤ now − 300 s`... **exactly**: an instant `s` still counts while `now < s + 300 s`; if fewer than `b` count, append `now` and return; else sleep (ms, rounded up) until `oldest + 300 s`, then re-read the clock and re-check (an early wake-up cannot over-grant). A cancelled waiter consumes nothing.

- [ ] **Step 1: Failing tests** with `ManualTime`:
  - `b` grants at one instant succeed; the `b+1`-th waits; it is granted exactly when the first start is 300 s old (not 1 ms earlier).
  - a start at `t` frees its slot at `t + 300 s` exactly (boundary).
  - **property test** (seeded `random`, 200 runs): random arrival times, random `b` in 1..20, 1..8 concurrent tasks acquiring in random patterns, a run of simulated ~1 hour; after the run, for **every** grant instant `g`, the number of grants in `[g − 300 s + 1 ns, g]`... i.e. in any half-open 300 s window, is `≤ b`.
  - **b = 100 all consumers competing:** four task kinds (round, retry, time, backfill) acquire in a tight loop for 20 simulated minutes; max starts in any 300 s window `== 100` and never above.
  - a cancelled waiter (queued and sleeping) leaves the count unchanged; the next waiter is granted on schedule.
  - an early wake-up (sleep returns before the deadline) does not grant (fake sleep that returns immediately once).
  - invalid `b` (`0`, `-1`, `True`, `1.0`, `"5"`) raises.
- [ ] **Step 2:** RED. **Step 3:** implement. **Step 4:** GREEN; mutation check by hand: change `<` to `<=` in the window test and see the boundary test and the property test fail (report it).
- [ ] **Commit:** `feat(collector): sliding-window request budget for open interest (ADR-0003 P-10)`

### Task 4: 429 / 418 gate

**Files:**
- Create: `src/quasar_market_data/collector/open_interest_gate.py`, `tests/collector/test_open_interest_gate.py`

**Interfaces:**
- `class OpenInterestPaused(SourceError)` (`remaining_ms: int`), `class OpenInterestBanned(SourceError)` (both in `open_interest_gate.py`, re-exported by the source module).
- `class OpenInterestGate(clock: Clock, logger: logging.Logger | None = None)`:
  `check() -> None` (raises `OpenInterestBanned` if banned, else `OpenInterestPaused` while `now < pause_until`);
  `record_rate_limited(retry_after_ms: int) -> int` returns the pause applied in ms (formula in Decisions);
  `record_banned() -> None`; `record_success() -> None` (resets the consecutive count; a success **during** a pause cannot happen because `check` raises, but one in flight when the 429 arrived may land after it: it must **not** clear an active pause, only the counter once the pause has ended).

- [ ] **Step 1: Failing tests:**
  - first 429 with `retry_after_ms = 0` → 300 000 ms; with 10 000 → 300 000; with 900 000 → 900 000.
  - consecutive 429s (each after the previous pause ended) → 300, 600, 1 200, 2 400, 3 600, 3 600 s (cap); `Retry-After` 7 200 s → 7 200 s (honoured, not capped); after a success the next 429 is 300 s again.
  - a second 429 recorded **during** an active pause (two requests in flight both got 429) extends nothing twice: pause end is `max(existing, new)` and `n` increments once per distinct pause, not once per exception (test with two `record_rate_limited` calls at the same instant → one doubling step).
  - `check` raises `OpenInterestPaused` with the right `remaining_ms` during the pause and passes at the exact pause end.
  - `record_banned` → every later `check` raises `OpenInterestBanned`, also after 2 h of fake time, and an error is logged exactly once (caplog) even if called twice.
  - a late success during a pause does not shorten it.
- [ ] **Step 2:** RED. **Step 3:** implement (monotonic clock). **Step 4:** GREEN.
- [ ] **Commit:** `feat(collector): open interest 429 pause with doubling and 418 stop (ADR-0003 P-10)`

### Task 5: The open interest source on its own client

**Files:**
- Modify: `src/quasar_market_data/collector/binance_usdm.py` (rename `_market_id` → `usdm_market_id`, nothing else), `tests/collector/test_binance_usdm.py` only if it imports the old name; `pyproject.toml` (the `ignore_imports` entry `quasar_market_data.collector.binance_usdm_open_interest -> ccxt`, moved here from the old Task 6 so this task's `make check` can pass, review C2).
- Create: `src/quasar_market_data/collector/binance_usdm_open_interest.py`, `tests/collector/test_binance_usdm_open_interest.py`

**Interfaces:**
- `class OpenInterestRequestRefused(SourceError)`.
- `class BinanceUsdmOpenInterestSource`:
  - `@classmethod async open(budget, gate, clock=None, exchange=None) -> Self`: `client = ccxt_async.binanceusdm() if exchange is None else exchange` (**always a new instance by default**), check `client.urls['api']['fapiData']` is exactly the USDT-M data URL (else close the client, `SourceError`), `inner = await BinanceUsdmSource.open(client, clock)`; on failure the client is closed (the inner `open` already does).
  - `async fetch_server_time() -> ServerTimeObservation`: `gate.check()`, semaphore, `budget.acquire()`, `gate.check()`, then `inner.fetch_server_time()`; 429/418 handling as below, taken from `error.__cause__` because the inner source wraps every CCXT error in `SourceError`.
  - `async fetch_open_interest_hist(symbol, start_time_ms, end_time_ms, *, as_of_ms) -> list[object]`: order: `usdm_market_id` (unknown symbol → `SourceError`), `validate_open_interest_request` (error → `OpenInterestRequestRefused`, **no budget, no request**), `gate.check()`, semaphore, `budget.acquire()`, `gate.check()`, `fapiDataGetOpenInterestHist({"symbol": market_id, "period": "5m", "startTime", "endTime", "limit": count})`.
  - Error mapping in one private helper used by both calls, per the Decisions (message prefix `<id> 429 ` / `<id> 418 `, other `DDoSProtection` and `RateLimitExceeded` as a pause): a pause → `gate.record_rate_limited(retry_after_ms)` then raise `OpenInterestPaused` with the applied pause; a ban → `gate.record_banned()` then `OpenInterestBanned`; any other `ccxt.BaseError` (including `BadRequest -1130`) → `SourceError(f"... failed: {error}")`; response not a list → `SourceError`; success → `gate.record_success()`.
  - `async close()` closes the inner source (and so the client).
- `MAX_IN_FLIGHT: Final = 2`, `DEFAULT_BUDGET_PER_5MIN: Final = 400`.

- [ ] **Step 1: Failing tests** with a fake `exchange` (async methods, records calls and the `params` dict, scripted responses/exceptions, an `asyncio.Event` hook to hold calls in flight), `ManualTime`, a real `OpenInterestBudget` and `OpenInterestGate`:
  - **Request shape:** params are exactly `symbol` (market id), `period="5m"`, `startTime`, `endTime`, `limit == number of period ends`; the raw response list is returned untouched (strings stay strings; same object contents).
  - **P-15 `> limit` never requested:** 501 period ends → `OpenInterestRequestRefused`, `calls == []`, budget starts == 0. 500 → one call with `limit=500`. Unaligned start, unaligned end, `start > end` → refused, no call.
  - **P-15 newest-limit fake:** the fake honours `limit` the way Binance does (returns the *newest* `limit` rows of the range; it also accepts `limit` 501 and 600, as observed in P-5 point 5, so the refusal is the adapter's, not the fake's). With a range of 500 period ends, all 500 rows come back and `parse_open_interest_hist` accepts them. Control (documents why the guard exists): the same fake given a hypothetical 501-slot request would drop the oldest row — asserted on the **fake** directly, so the guard test would fail if the rule were removed (test calls the adapter with 501 and asserts it raises *instead of* returning 500 rows).
  - **P-15 horizon / `-1130`:** `start = as_of − H − 5 min` → refused, no call; `start = as_of − H` → sent. The fake raises `ccxt.BadRequest('binance {"code":-1130,"msg":"parameter \'startTime\' is invalid"}')` → `SourceError` (not `OpenInterestRequestRefused`, not a crash), gate unchanged, budget start counted (the request was sent).
  - **Real CCXT shapes (review C1):** `DDoSProtection("binanceusdm 429 Too Many Requests {...}")` → pause (300 s, `Retry-After` from the fake's `last_response_headers`, case-insensitive `retry-after`, a junk value `"abc"`/`"-1"`/`"1.5"` → 0); `DDoSProtection("binanceusdm 418 ...")` → ban; `DDoSProtection("binanceusdm 503 ...")` and `RateLimitExceeded` → pause without `Retry-After`; each is also raised from the **time** call, which the inner source wraps in `SourceError` (cause preserved) and still pauses/bans.
  - **Gate re-check (review H1):** with `b = 1`, call A is running, call B waits in the budget; A gets a 429; time advances past the budget wait: B raises `OpenInterestPaused` and the fake saw exactly one call. Same with a 418 and with the semaphore as the waiting point.
  - **`fapiData` URL (review M2):** a client whose `urls['api']['fapiData']` is `http://...`, another host, a path with a query, or missing → `SourceError`, client closed.
  - **Unknown symbol** → `SourceError`; not a USDT-M perpetual market → `SourceError` (same check as candles).
  - **Own server time:** `fetch_server_time` calls `fapiPublicGetTime` of the OI client, returns a `ServerTimeObservation`, and counts one start in the budget; the candle `SharedServerTime` is never imported by the module (`import` check in the test via `sys.modules` of a fresh interpreter is overkill: assert by import-linter in Task 6).
  - **Own client:** `open()` without `exchange` creates a new `ccxt_async.binanceusdm` each time (monkeypatch the constructor to a recording factory; two `open`s → two distinct clients; never the one the candle source holds), validates the base URL, loads markets.
  - **In flight ≤ 2:** hold 5 concurrent `fetch_open_interest_hist` calls on the fake; at most 2 reached the exchange; releasing one lets the next in; peak concurrency recorded by the fake never exceeds 2, including time calls.
  - **Semaphore/budget order (review M3):** `b = 3`, two calls held in flight on the fake for 400 s of fake time while 10 more are queued: the recorded call instants never show more than 3 in any 300 s (fails if the semaphore were taken after the budget).
  - **Budget competition (P-15, b = 100):** 150 concurrent mixed calls (hist and time) with the real budget `b = 100` and `ManualTime`: the fake's recorded call instants never show more than 100 in any 300 s.
  - **Retry after 429 not budget-free:** after a 429 the gate pauses: the next 10 calls raise `OpenInterestPaused`, the fake sees none, budget starts unchanged; after the pause ends (advance time) a call goes through. Doubling over three consecutive 429s observed through `remaining_ms`.
  - **418:** raises `OpenInterestBanned`; every later hist and time call raises it without reaching the fake, also after 2 h of fake time.
  - **Candle requests continue (P-15):** a second source (a plain `BinanceUsdmSource` on its own fake) keeps serving klines and server time while the OI source is paused and banned; the OI gate/budget objects are not reachable from it.
  - **Response not a list** → `SourceError`; cancellation of a pending call propagates `CancelledError` and releases the semaphore (a later call still proceeds).
- [ ] **Step 2:** RED. **Step 3:** implement (module docstring records the Task 1 facts and says it is the second importer of `ccxt`). **Step 4:** GREEN; hand mutations to report: remove the 500 check; remove the horizon check; take the semaphore after the budget; let a pause not block the call. Each must fail at least one test. Also record whether a real-shape 429 reaches `record_rate_limited` (the C1 mutation: match on `RateLimitExceeded` only, which must fail).
- [ ] **Commit:** `feat(collector): open interest source on its own CCXT client with request rules, budget and 429/418 (ADR-0003 P-5, P-9, P-10)`

### Task 6: Import contract for independence from the candle limiter and shared time

**Files:**
- Modify: `pyproject.toml` (new contract), `tests/test_import_contracts_negative.py` (**add the contract name to `CONTRACT_NAMES`**: the test asserts `f"{len(CONTRACT_NAMES)} kept, 0 broken"`; plus a negative case in its pattern).

- [ ] **Step 1: Failing test:** a negative case where `binance_usdm_open_interest.py` (a probe copy) imports `quasar_market_data.collector.shared_server_time` or `quasar_market_data.collector.rate_limiter` must violate a **new** contract `open interest source is independent of the candle limiter and shared server time` (type `forbidden`, source `binance_usdm_open_interest`, `open_interest_budget`, `open_interest_gate`; forbidden `rate_limiter`, `shared_server_time`; ADR-0003 P-9). A second probe: another `collector` module importing `ccxt` still violates `ccxt is imported only by the Binance adapter`.
- [ ] **Step 2:** add the contract; **Step 3:** `make imports` GREEN and the negative tests prove both contracts bite.
- [ ] **Commit:** `build: import contract keeping the open interest source off the candle limiter (ADR-0003 P-9)`

### Task 7: `fetch-oi-once`

**Files:**
- Create: `src/quasar_market_data/collector/fetch_open_interest_once.py`, `tests/collector/test_fetch_open_interest_once.py`, `tests/collector/test_cli_fetch_oi_once.py`
- Modify: `src/quasar_market_data/collector/cli.py` (new subcommand, `_fetch_oi_once`, a second `OpenSource`-like factory parameter `open_oi_source` on `main`, default `BinanceUsdmOpenInterestSource.open` wrapped with a default budget/gate; module docstring gains the `fetch-oi-once` paragraph).

**Interfaces (use case):** `FetchOpenInterestOnceConfig(symbol, periods=3, publication_delay_ms=DEFAULT_OPEN_INTEREST_PUBLICATION_DELAY_MS, time_trust=DEFAULT_TIME_TRUST)`; `async fetch_open_interest_once(source, writer, config, clock) -> FetchOpenInterestOnceReport(fetched, selection, write, start_ms, end_ms)`. `source` is a small `Protocol` (`fetch_server_time`, `fetch_open_interest_hist`, `close`) defined in this module; `writer` a `Protocol` with `write(rows, *, ingested_at_ms)` that `AsyncOpenInterestWriter` satisfies.

Order: observe time (a `SourceError` is "no observation", as `fetch_once`), `server_lower_bound` at the request send time; **untrusted time: no request is made** (unlike klines there is no reason to fetch rows that can only be pending; nothing could be stored), report `untrusted_reason`, exit `1` with a message naming the reason, as `fetch-once` does when time is untrusted. Trusted: `B`, range, `as_of = lower_bound`, fetch, `parse_open_interest_hist`, `select_eligible_open_interest`, write eligible.

- [ ] **Step 1: Failing use-case tests** (fake source with scripted server time/rows, `FakeClock`, recording writer):
  - trusted time: range is `[B − (N−1)·5 min, B]` with `B` aligned at or below the bound; request has `as_of_ms == lower_bound`.
  - a row whose end + 15 s is after the bound is pending and **not** written; eligible ones are written once with `ingested_at_ms = clock.wall_ms()`.
  - untrusted time (stale observation / failed time request): no hist call, nothing written, reason in the report.
  - parse error (row outside range, wrong symbol) propagates, nothing written; `SourceError`, `OpenInterestPaused` propagate unchanged; `OpenInterestPageError` propagates unchanged (the CLI unwraps it).
  - `periods` 0 and 501 → `ValueError` in the config.
- [ ] **Step 2: Failing CLI tests** (mirror `test_cli_fetch_once.py`: injected source factory, `tmp_path` database, `FakeClock`): stores rows and prints a report line (`fetched`, `eligible`, `pending`, `inserted`, `duplicates`, `conflicts`); second identical run inserts 0 and exits 0; a conflicting re-delivery exits 3 and the stored row is unchanged; `--periods 501` and a missing `--db` directory exit per `fetch-once` (usage `2` / failure `1`); outcome unknown exits 4 (also when raised as the `.cause` of an `OpenInterestPageError`); a page that fails after 1 committed transaction (`--periods 150`, fake writer or faulty connection) exits **4** and names the committed count, a page that fails in its first transaction exits 1; `KeyboardInterrupt` exits 130; the source and writer are closed in every path (spy); migration to schema 2 happens before the first request (a v1 database is upgraded, as `fetch-once` does); `fetch-once` and `run` output unchanged (their existing tests stay green).
- [ ] **Step 3:** RED. **Step 4:** implement. **Step 5:** GREEN.
- [ ] **Commit:** `feat(collector): fetch-oi-once stores the newest eligible open interest rows (ADR-0003 P-16 OI-3)`

### Task 8: Live check, README, register

**Files:**
- Modify: `README.md` (command table row `fetch-oi-once`; new section "Open interest request budget" with the operational requirement: nothing else on the IP may call `/futures/data/*` more than 600 times per 5 minutes, no second Collector with open interest enabled; the 429/418 behaviour; that OI-3 alone writes nothing continuously), `docs/invariants.md` (rows below), `docs/follow-ups.md` only if a review finding is deferred, `tests/live/` (a live test, marker `live`, not in `make check`), `tests/test_invariant_register.py` expectations if the register test enumerates ids (read it first).
- Test: `tests/live/test_live_open_interest.py`

- [ ] **Step 1: Live test** (marker `live`): open the OI source on the real endpoint, `fetch_server_time`, `fetch_open_interest_hist("BTC/USDT:USDT", B − 2·5 min, B, as_of=bound)`; parse with the OI-1 parser; assert ≤ 3 rows, all values decimal strings. **Run it** (`make live` subset) and report the real result; if the network is unavailable say so and do not claim it passed.
- [ ] **Step 2: Invariant rows** (follow the INV-07x format; evidence by test names): `INV-080` open interest requests never cover more than 500 period ends nor start older than 29 days before the server lower bound, and a violation is refused before any request (ENFORCED: Task 2 and 5 tests); `INV-081` open interest request starts never exceed `budget_per_5min` in any 300 s, time requests included, with at most 2 in flight (ENFORCED: Task 3, 5); `INV-082` a `429` pauses all open interest requests for at least 300 s and `Retry-After`, a `418` stops them for the process run, and neither touches candle sources (ENFORCED: Task 4, 5). INV-078 ("only eligible open interest rows are persisted") changes from PLANNED to ENFORCED **for `fetch-oi-once`** only if a test shows the gate end to end (pending rows are never passed to the writer); otherwise its evidence column names `fetch-oi-once` and it stays PLANNED for OI-4. INV-079 ("never delays or stops candle collection") stays PLANNED; its evidence column gains the OI-3 pieces (own client, own budget, import contract) and still names OI-4 for task isolation.
- [ ] **Step 3: `make check`** with the real output; `uv run python -m quasar_market_data.collector.cli fetch-oi-once --help` shows the command.
- [ ] **Commit:** `docs: open interest request budget, live check and invariants INV-080 to INV-082`

## Risks and open points (report in the PR)

- `Retry-After` attribution with 2 requests in flight (Decisions); the floor of 300 s keeps the rule safe.
- Whether a 418 caused by open interest also bans klines for the IP is **not known** (ADR P-10 point 3); the budget exists to make it unlikely.
- The budget is per process, not per IP: another consumer of `/futures/data/*` on the same IP is invisible (README requirement).
- `fetch-oi-once` with untrusted time exits 1 and stores nothing, which differs from `fetch-once` (exit 0 with pending candles); chosen because nothing is stored.
- Nothing in OI-3 runs continuously; it cannot affect candle collection until OI-4 wires it in.
