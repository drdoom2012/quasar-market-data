# Shared Server-Time Observation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The collection loop observes the exchange server time once per refresh interval instead of once per symbol and attempt, so a round of N symbols needs about N adapter calls instead of 2N.

**Architecture:** A new collector component, `SharedServerTime`, hands every caller the newest observation that the existing trust gate (`server_lower_bound`) still accepts and refreshes it, single-flight, when it is older than a refresh age or when the gate rejects it. `fetch_once` and `collect_symbol` take it as an optional provider; without one they behave exactly as today. `run_collection_loop` creates one provider per run. Backfill, the domain and the closed-candle selection are untouched.

**Tech Stack:** Python 3.12 `asyncio`, existing `server_lower_bound` and `TimeTrustConfig`; pytest, mypy strict, ruff, import-linter. No new dependency.

**Spec:** `docs/adr/0001-binance-ingestion-fidelity-and-closed-candles.md` (D-D, P-3, P-4, P-6), follow-up F-23 in `docs/follow-ups.md`, `docs/invariants.md` (INV-007, INV-022, INV-024), `src/quasar_market_data/collector/fetch_once.py` (module docstring).

## Facts checked in the repository (2026-11-01)

- `fetch_once(source, writer, config, clock)` calls `source.fetch_server_time()` on every call and passes `None` as the previous accepted server time to `server_lower_bound`; a `SourceError` becomes "no observation".
- `server_lower_bound(observation, now_monotonic_ns, now_wall_ms, previous, config)` returns `TrustedTime` or `UntrustedTime(reason)`. The bound is `S + age * (1 - drift) - margin`. The age of an observation therefore costs only drift (500 ppm: 15 ms at 30 s, 30 ms at 60 s) in how conservative the bound is. It never makes the bound larger than the true server time (INV-024). The age gate `max_age_ms` (60 000) and the clock-divergence gate (100 ms) still reject an observation that is too old or whose wall clock moved against the monotonic one.
- `collect_symbol(source, sink, config, target_open_ms, retry, clock, sleep, stop, limit_for_attempt=None, observe_forming=None)` retries a symbol up to `RetryPolicy.max_attempts` (6) times when time is untrusted; the delays are 1, 2, 4, 8, 8 s.
- `run_collection_loop` owns the `clock` and `config.time_trust`; `Backfiller._walk` calls `fetch_once` on a per-page `DeadlineSource` and is not touched here.
- Only `tests/collector/test_backfill.py:84` asserts a `time_calls` count; the loop tests use `ScriptedSource`, which has no counter yet.
- Not verified: nothing in this plan has been run; the effect on round duration is arithmetic from the code (1000 symbols, `min_request_interval_ms = 100`: about 100 s per round instead of 200 s), not a measurement.

## Global Constraints

- Collector and API stay independent; `domain` stays pure and is not touched; the provider lives in `collector`.
- Every candle reaches the writer only through `fetch_once` (INV-007); the closed-candle selection stays the single gate.
- Fail closed: an observation the trust gate rejects confirms nothing and is never served to a second caller; a failed refresh never turns missing time into a trusted bound.
- No change to the config file, the database schema, any public contract or `docs/adr/`. No new dependency.
- Default behaviour of `fetch_once` and `collect_symbol` without a provider is unchanged.
- Verification: `make check` before **every** commit; during a task `uv run pytest <path>`. No other commands are invented.

## Decisions to approve (CLAUDE.md: accepted decisions are not changed silently)

1. **ADR-0001 text is not changed.** P-4 defines the bound for "an observation" and does not require one observation per request. Assumption A1 (`S` is the server clock at some instant between send and receive) holds for every observation individually, so a reused observation gives a bound that is still at most the true server time. Alternative: amend P-4 to say so explicitly. If you prefer the amendment, it is a one-paragraph change in Task 4.
2. **Refresh age = `time_trust.max_age_ms // 2`** (30 s by default), not configurable. An observation handed out is at most 30 s old plus the wait of one call, so it is far from the 60 s age gate; every 300 s round refreshes at least once. Alternative: a config key (changes the config file contract, not proposed).
3. **A failed refresh falls back to the previous observation**; the age gate then decides (it rejects it after 60 s). With no previous observation the result is `None` ("no observation"), as today. Alternative: return `None` at once, which makes every symbol of a round untrusted on one failed time request even though a 20 s old observation is valid.
4. **Scope: the collection loop only.** Backfill pages keep one observation per page: they run in idle time, on a `DeadlineSource` per page, and sharing there would let a waiter outlive its own deadline.
5. **Refreshes are serialised** by one `asyncio.Lock` (single-flight). A persistently rejected observation therefore costs one request per caller, one after another, like today but without parallelism.

**Amended after the final review (2026-11-01).** Decision 2 changed: the refresh age is `min(5 s, max_age_ms // 2)`, not 30 s. Sharing saves almost nothing more at 30 s than at 5 s, while the age of an observation multiplies the clock-rate error that the drift reserve of ADR-0001 P-4 (A3) must cover (an NTP-slewed monotonic clock can run fast against true time). Decision 3 gained two rules: while one caller refreshes, the others are served a cached observation only if the gate still accepts it, and a failed refresh is not retried for 1 s (a hanging time endpoint must not serialise the round). ADR-0001 text is still unchanged; recording the new weight of A3 in P-4 remains an open choice for the user.

## Review Focus

- A rejected observation must not be served twice: attempt 1 gets an observation with `rtt` above `max_rtt_ms`, attempt 2 one second later must get a fresh one (otherwise every retry of the round fails with `RTT_TOO_LARGE` until the refresh age). Pinned in Task 1 and Task 3.
- A wall-clock step or a system suspend between the observation and a later request is rejected by the divergence gate and triggers a refresh (Task 1).
- The time endpoint fails while a valid observation is cached: collection continues; failing for longer than `max_age_ms` stores nothing and reports the reason (Task 1, Task 3).
- The task that is refreshing is cancelled at shutdown grace: the lock is released and the next caller refreshes (Task 1).
- A symbol served late in a long round is not made untrusted by the age of the shared observation (Task 3).

---

### Task 1: `SharedServerTime`

**Files:**
- Create: `src/quasar_market_data/collector/shared_server_time.py`
- Test: `tests/collector/test_shared_server_time.py`

**Interfaces:**
- Consumes: `KlineSource.fetch_server_time() -> ServerTimeObservation`, `Clock`, `TimeTrustConfig`, `server_lower_bound`, `TrustedTime`, `SourceError`.
- Produces:
  - `class ServerTimeProvider(Protocol)` with `async def observation(self, source: KlineSource) -> ServerTimeObservation | None`.
  - `class SharedServerTime` with `__init__(self, clock: Clock, trust: TimeTrustConfig, refresh_after_ms: int)` and the same `observation` method. It never raises `SourceError`.

- [ ] **Step 1: Write the failing tests** in `tests/collector/test_shared_server_time.py` using `FakeClock` and `FakeSource` from `tests/collector/fetch_fakes.py` (`rtt_ms`, `time_error`, `time_calls`). Names and assertions:
  - `test_the_first_call_fetches_and_a_second_call_within_the_refresh_age_reuses_it`: two calls, `source.time_calls == 1`, both return the same object.
  - `test_an_observation_older_than_the_refresh_age_is_refreshed`: advance the clock by `refresh_after_ms`, `time_calls == 2`.
  - `test_an_observation_the_trust_gate_rejects_is_returned_but_never_served_twice`: `rtt_ms=2500` (above `max_rtt_ms` 2000); two calls give `time_calls == 2`.
  - `test_a_wall_clock_step_makes_the_cached_observation_unusable`: after the first call `clock.advance_wall(5_000)` (new `FakeClock` method in `tests/collector/fetch_fakes.py` that moves only the wall clock), second call refreshes.
  - `test_concurrent_callers_share_one_request`: `asyncio.gather` of 5 calls, `time_calls == 1`.
  - `test_a_failed_refresh_falls_back_to_the_previous_observation_and_never_raises`: first call ok, advance past the refresh age, set `source.time_error = SourceError("down")`, the call returns the first observation.
  - `test_a_failed_first_request_returns_none`.
  - `test_a_cancelled_refresh_releases_the_lock`: a source whose `fetch_server_time` blocks on an `asyncio.Event`; cancel the first caller; a second call with a working source returns an observation.
  - `test_refresh_age_must_be_an_int_below_max_age`: `refresh_after_ms` of `True`, `0`, `max_age_ms` raise `TypeError` / `ValueError`.

- [ ] **Step 2: Run** `uv run pytest tests/collector/test_shared_server_time.py -v`; Expected: FAIL (module missing).

- [ ] **Step 3: Implement** `SharedServerTime` in `shared_server_time.py`. A cached observation is usable iff `server_lower_bound(cached, clock.monotonic_ns(), clock.wall_ms(), None, trust)` is a `TrustedTime` and `monotonic_ns - cached.receive_monotonic_ns <= refresh_after_ms` (in ns). `observation` checks this, takes the lock, checks again, then fetches. A fetched observation is stored and returned whether trusted or not (the caller's gate reports the reason); the next caller finds it unusable and refreshes. On `SourceError` return the previous observation (or `None`). Validate `refresh_after_ms` with the exact-type pattern of the module (`type(x) is not int` → `TypeError`; `not 1 <= x < trust.max_age_ms` → `ValueError`). Module docstring states the safety argument in the Facts section and Decision 3.

- [ ] **Step 4: Run** the same command; Expected: PASS. Then `make check`; Expected: all green.

- [ ] **Step 5: Commit** `git add src/quasar_market_data/collector/shared_server_time.py tests/collector/test_shared_server_time.py tests/collector/fetch_fakes.py && git commit -m "feat: shared server-time observation (not wired in yet)"`.

### Task 2: `fetch_once` and `collect_symbol` accept a provider

**Files:**
- Modify: `src/quasar_market_data/collector/fetch_once.py` (the `fetch_once` signature and the observation lines; module docstring "Order of work"), `src/quasar_market_data/collector/collection_loop.py` (`collect_symbol`)
- Test: `tests/collector/test_fetch_once.py`, `tests/collector/test_collect_symbol.py`

**Interfaces:**
- Consumes: `ServerTimeProvider` (Task 1).
- Produces: `fetch_once(source, writer, config, clock, *, server_time: ServerTimeProvider | None = None)` and `collect_symbol(..., observe_forming=None, *, server_time: ServerTimeProvider | None = None)`; the latter forwards it to `fetch_once` on every attempt.

- [ ] **Step 1: Write the failing tests.**
  - `test_fetch_once_uses_the_provider_instead_of_asking_the_source` (`test_fetch_once.py`): a provider returning a fixed `ServerTimeObservation`; `source.time_calls == 0` and the closed candle is eligible.
  - `test_a_provider_returning_none_is_no_observation` (`test_fetch_once.py`): `selection.untrusted_reason is TimeRejection.NO_OBSERVATION`, nothing written.
  - `test_without_a_provider_every_call_asks_the_source` (`test_fetch_once.py`): two calls, `time_calls == 2`.
  - `test_collect_symbol_forwards_the_provider_to_every_attempt` (`test_collect_symbol.py`): a counting provider, two attempts (first untrusted), provider called twice.

- [ ] **Step 2: Run** `uv run pytest tests/collector/test_fetch_once.py tests/collector/test_collect_symbol.py -v`; Expected: FAIL (`unexpected keyword argument 'server_time'`).

- [ ] **Step 3: Implement** the two keyword parameters. In `fetch_once`: `observation = await source.fetch_server_time()` (with the existing `SourceError` → `None`) is replaced by `observation = await server_time.observation(source)` when a provider is given and kept otherwise. No other line changes.

- [ ] **Step 4: Run** the same command, then `make check`; Expected: PASS and green.

- [ ] **Step 5: Commit** `git commit -am "feat: fetch_once and collect_symbol accept a server-time provider"` (after `git add` of the new tests).

### Task 3: One provider per collection run

**Files:**
- Modify: `src/quasar_market_data/collector/collection_loop.py` (`run_collection_loop`), `tests/collector/loop_fakes.py` (`ScriptedSource.time_calls`)
- Test: `tests/collector/test_collection_loop.py`

**Interfaces:**
- Consumes: `SharedServerTime(clock, config.time_trust, config.time_trust.max_age_ms // 2)`, `collect_symbol(..., server_time=...)`.
- Produces: one provider per `run_collection_loop` call, shared by all symbols and rounds.

- [ ] **Step 1: Write the failing tests** (add `self.time_calls = 0` to `ScriptedSource`, incremented in `fetch_server_time`):
  - `test_a_round_of_many_symbols_observes_server_time_once`: 20 symbols, `source.time_calls == 1` after round one.
  - `test_a_later_round_observes_again`: two rounds 300 s apart, `time_calls == 2`.
  - `test_a_rejected_observation_is_not_reused_by_the_retry`: first time request answered with an RTT above `max_rtt_ms` (delay in `ScriptedSource` through the manual clock), the symbol's second attempt succeeds and stores the target candle (`SymbolOutcome.COLLECTED`).
  - `test_a_failing_time_endpoint_is_survived_while_the_observation_is_valid_and_fails_closed_after`: round one fine; time endpoint raises `SourceError` afterwards; a retry inside 60 s still collects, a round after more than `max_age_ms` stores nothing and reports `time untrusted`.

- [ ] **Step 2: Run** `uv run pytest tests/collector/test_collection_loop.py -v`; Expected: the four new tests FAIL (`time_calls` is the symbol count, not 1).

- [ ] **Step 3: Implement** the provider creation in `run_collection_loop` and pass `server_time=` in `run_symbol`'s `collect_symbol` call. Then run the whole suite. Tests that count adapter calls change on purpose; update only their expected counts, never a validation: `test_600_symbol_first_round_keeps_the_spacing_and_the_concurrency_bound` (counts klines and time starts) and any test that fails for the same reason. A failure with another cause is analysed, not adjusted.

- [ ] **Step 4: Run** `uv run pytest -q`, then `make check`; Expected: all green.

- [ ] **Step 5: Commit** `git commit -am "feat: the collection loop shares one server-time observation across symbols"`.

### Task 4: Documentation

**Files:**
- Modify: `docs/invariants.md`, `README.md`, `docs/follow-ups.md`, `src/quasar_market_data/collector/fetch_once.py` (docstring), optionally `docs/adr/0001-binance-ingestion-fidelity-and-closed-candles.md` (only if Decision 1 is answered "amend")

- [ ] **Step 1:** Add `INV-070` (next free number after INV-069; check the file first): "A shared server-time observation is served only while the trust gate accepts it and it is younger than half of `max_age_ms`; a rejected observation is never served twice; a failed refresh never creates trust". Status `ENFORCED`, evidence the Task 1 and Task 3 test names.
- [ ] **Step 2:** README, next to the `min_request_interval_ms` paragraph: one sentence that a round observes server time about once per 30 s, not per symbol.
- [ ] **Step 3:** `docs/follow-ups.md` F-23: mark the per-symbol cost as fixed for the collection loop, leave backfill pages and the unmeasured round duration as open.
- [ ] **Step 4:** Run `make check`; Expected: green. **Commit** `git commit -am "docs: INV-070, README and F-23 for the shared server-time observation"`.

## Out of scope (recorded, not fixed here)

- Backfill pages still observe server time once per page (Decision 4).
- W-6 (server time never regresses between observations) is still not checked across observations; with a shared observation the evidence for it would exist, so it is a cheap follow-up (ADR-0001 P-4).
- The real round duration at 100 to 1000 symbols is not measured; run a live round with the target universe before relying on the arithmetic.
