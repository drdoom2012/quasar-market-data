# Follow-ups

Known gaps that were deliberately left open. None of them blocks the slice that found it. Each entry says where it
was found, what is wrong, and a suggested fix. Remove an entry when it is fixed (in the commit that fixes it).

## From the S10 review (`GET /v1/candles`, independent review of the implemented branch)

### F-5. Unhandled errors are logged twice under uvicorn

- **Where:** `src/quasar_market_data/api/app.py` (`unhandled` handler).
- **What:** Starlette re-raises after the handler has sent the 500, so uvicorn logs a second traceback ("Exception in
  ASGI application") without the correlation id. The response body is correct.
- **Fix:** log the correlation id in a middleware that also stops the re-raise, or accept the duplicate and document
  it. Low value; log noise only.

### F-7. Smaller notes from the same review

- **Process-test port race** (`tests/test_api_process.py`): if a foreign process binds the chosen port while the
  child is still importing, its answer arrives as a JSON or assertion error instead of a retry. Negligible chance;
  fix only if the test ever flakes (for example by asserting the answer comes from our app before using it).
- **SQLite version re-check per request:** `open_reader` re-checks the SQLite version on every call as well as at
  start-up (`quasar-api serve`). Kept on purpose: `open_reader` is shared and the check is one comparison. Not a defect.
- **Live Binance demonstration:** run once by hand on 2026-10-25 (collector `fetch-once`, then `quasar-api serve`,
  `curl`); it is not part of `make check`.

## From the S14 plan (`quasar-collector run`)

### F-8. Limits of the collection loop that were left open on purpose

- **Where:** `src/quasar_market_data/collector/collection_loop.py`, `cli.py`, `rate_limiter.py`.
- **No server-time regression check across calls.** `fetch_once` starts every call without the previous accepted
  observation, so ADR-0001 P-4 (server time never regresses) is not checked across the loop's calls. Needs state shared
  between calls.
- **No long cool-off after Binance 418/429.** Those errors are ordinary retryable `SourceError`s with the short bounded
  backoff.
- **No hard deadline on a round or on `AsyncCandleWriter.close()`.** The bounds are the attempt count, the CCXT request
  timeout (10 s default, not re-enforced here), the limiter queue and SQLite's `busy_timeout` per lock wait.
- **Two requests per symbol and attempt** (server time, klines). A shared observation per round would halve it; measure
  in S23 first.
- **Request spacing is a placeholder** (100 ms) and is spacing of adapter calls, not of network requests; Binance
  weights were not read.
- **The uncertainty flag lives only in the process.** A restart starts clean; clearing it properly needs batch identity
  and read-back, which belongs to S15/S16.

## From the S15 plan (restart reconcile)

### F-9. The window selects exist twice

- **Where:** `src/quasar_market_data/api/coverage_reader.py` and `src/quasar_market_data/collector/reconcile_reader.py`.
- **What:** both run the same window selects. A shared reader in `storage` would remove the copy; deferred because it touches the API read path.
- **S18:** `src/quasar_market_data/api/indicator_reader.py` adds a third copy of the same bounded selects (candles,
  conflict and unavailable records of one window).

### F-10. The series-wide hole aggregate: start-up cost fixed, the post-check cost measured

- **Where:** `src/quasar_market_data/collector/reconcile_reader.py`, `collector/cli.py`, `tests/collector/bench_reconcile.py`.
- **Before** (`make bench PROFILE=target`, 2026-10-28, 600 series x 105 120 rows): 75.6 s per read, run twice and
  inline (pre-start and post-check), not interruptible; `holes` 34.3 s, `largest_runs` 34.5 s, `bounds` 6.3 s
  (`MIN, MAX` in one aggregate walks the series). C1 and C2 failed, C3 passed.
- **Fixed** (plan `docs/superpowers/plans/2026-10-28-reconcile-read-cost.md`, the user's decision of 2026-10-28,
  option (b) of S15 D13): bounds by two primary-key seeks (also in the API readers), one materialized `LAG` pass, a
  backfill hole scan that starts at its horizon, a window-only pre-start read, and the post-check in the background
  from a round-one snapshot, interruptible at shutdown after 2 s.
- **After** (same profile and machine, SQLite 3.53.4, page cache not dropped, so not a cold-disk figure; the full read
  measured with the `ReadGuard` installed, as the CLI runs it):

  | Read | Time |
  |---|---|
  | `pre_start_ms` (what the start waits for) | 0.68 s |
  | post-check, first run / second run | 35.9 s / 34.8 s |
  | of which `holes` (one walk of all 63 072 000 rows) | 34.9 s |
  | of which `bounds` (600 series) | 0.34 s |
  | linear projection of the post-check to 600 x 3 years | 108 s |

  C1 (≤ 60 s) and C2 (≤ 180 s) are met by the post-check; C3 holds (primary key only, no sorter on the candle walk,
  rows visited = rows stored). The start no longer waits for the history at all. The guard stops the read with
  `sqlite3_interrupt`, not a progress handler: a handler made the read wait for the GIL behind a busy event loop (a
  review measured 530 s instead of 2 s with a fully busy main thread); with `sqlite3_interrupt` the same probe gives
  0.69 s with the guard and 0.70 s without. **Accepted by the user on 2026-10-28** (HARD STOP 2 of S15 is closed for
  this profile); the open points below are not covered by that acceptance.
- **Still open:** a cold disk, a soak database (S23), more than 600 series or 3 years. The post-check holds one read
  snapshot for its duration, so the WAL cannot be checkpointed past it meanwhile (about 36 s at this size).

### F-11. `fetch-once` accepts a delay that no limit can serve

- **What:** `--publication-delay-ms` is accepted by `fetch-once` with no check against `--limit`; `run` refuses such a
  pair before opening anything.

### F-13. A backward wall-clock step ends the collector

- **Where:** `attempt_limit` in `src/quasar_market_data/domain/reconcile.py`.
- **What:** in round one only, a wall clock that steps back past the oldest wanted slot (roughly one slot, 5 minutes,
  or more) makes `attempt_limit` raise `ValueError`, which ends `run` with exit `1` (fail closed). Known limitation;
  fix by clamping or reporting per symbol.

### F-8 (addition). Restart shows the stored state but still does not identify the uncertain batch

- A restart reads the stored facts, so the state of the database is visible, but the batch whose write was uncertain is
  still not identified (needs batch identity and read-back, S16).

### F-14. `clock_behind_stored` in `reconcile-after` can be a false positive

- **Where:** `src/quasar_market_data/collector/cli.py` (post-check, `started_wall_ms`).
- **What:** when round one crosses a slot boundary and later symbols store the next slot, the flag reads `true` with a
  correct clock because it is computed against `started_wall_ms`. Diagnostic only; counts and gaps stay correct.
  Reproduced with two symbols, `--max-concurrency 1` and the clock advanced inside the first `fetch_klines`.
- **Fix:** compute the flag from a fresh wall reading.

### F-8 (S16). Backfill does not identify an uncertain batch either

- A backfill page write with an unknown outcome marks the symbol uncertain exactly as in a round (exit 4); the batch is
  not identified, and the mark is process-local.

### F-15. "Tried, nothing returned" and back-off are not persisted

- **Where:** `src/quasar_market_data/collector/backfill.py`.
- **What:** the exhaustion of a target the exchange did not serve (288 cycles), the per-symbol back-off and a
  suspension after repeated idle overruns live in memory, so every restart re-asks: up to `K + 1 = 21` probes per
  symbol per start, at the normal rate-limited backfill rate. The count over many restarts is unbounded; the rate is
  not. Persisting a marker needs a schema decision (ADR-0002 P-9).
- **Related:** strong evidence for writing `unavailable_candles` (plan D3) also needs a persisted first observation.

## From the S17 plan (health and readiness)

### F-16. The Collector has no health signal of its own

- **What:** S17 adds health to the API only (`/health`, `/health/ready`, `/health/ingest`). The Collector has no HTTP
  endpoint, heartbeat or status table; a stopped Collector shows only as `stale` series in `/health/ingest`, which
  cannot name the cause. A heartbeat needs a schema change (the user's decision). Process supervision (restart policy,
  container health) belongs to S21.
- **S21 (2026-10-29):** `compose.yaml` adds `restart: unless-stopped` and a healthcheck that only checks that the
  Collector's main process exists. Neither is a health signal of ingestion: a dead main process already stops the
  container, Docker does not restart an unhealthy container, and a stuck or idle Collector stays `healthy`. Still open.

### F-17. The `/health/ingest` read cost: accepted at 600 series, the rest unmeasured

- **Where:** `src/quasar_market_data/api/health_reader.py`, `tests/api/bench_health.py`.
- **What:** the read does two primary-key seeks per series (the next series and its newest candle), pinned by a test
  that the work does not grow with rows per series. Measured by the builder on 2026-10-28 with `make bench-health`
  (SQLite 3.53.4, Python 3.12.5, page cache not dropped, so not a cold-disk figure):

  | Profile | Rows | Steps per series (tiny db: 75.6) | Snapshot, max of 10 reads | Whole handler, max of 5 |
  |---|---|---|---|---|
  | `medium` (100 series x 100 000) | 10 000 000 | 67.9 | 1.1 ms | not measured (script added later) |
  | `target` (600 series x 100 000) | 60 000 000 | 66.3 | 15.4 ms (typically 3 to 5 ms) | 17.7 ms (typically 5 ms) |

  H3 (a writer commits inside each read; the result equals the pre-read snapshot): 5 commits in 5 reads, 0 differing
  results. The first `target` run used a weaker H3 that could not fail; it was replaced and rerun.
  The criteria H1 to H3 (S17 plan, Decision 11) are met by these numbers, and the user **accepted them on 2026-10-28**
  (HARD STOP 2 of S17 is closed for this profile). Not measured: a cold cache, a real network client, many concurrent requests, more than 600 series. S23
  repeats it on a soak database. The read is linear in the number of series and has no deadline beyond SQLite's busy
  timeout (5 s).

### F-18. Stored series versus the configured universe

- **Status: closed by S20 (2026-10-29).** The API reads the universe from `config/quasar.toml`; `/health/ingest` has a separate `universe` block listing configured instruments with no stored series, and `GET /v1/instruments` lists the configured universe with the newest stored candle. The S17 fields are unchanged. Different files for the two processes are only visible by the hash in the start line. The original finding follows.
- **What:** `/health/ingest` judges only series that exist in the database, so an instrument whose ingestion never
  started is invisible. Comparing the configured universe with the stored series needs a universe on the API side
  (S20, `/v1/instruments`); S17 deliberately adds no Collector-to-API channel.

### F-19. Load-sensitive S16 backfill tests

- **Where:** `tests/collector/test_backfill_run.py` (`test_a_slow_backfill_does_not_skip_a_cycle`,
  `test_unknown_write_outcome_during_backfill_marks_the_symbol_uncertain`).
- **What:** each failed once in about eight full runs during S17 with `nothing is waiting: the simulation is stuck`,
  and passed on every rerun, alone and in the full suite, with and without the S17 changes. They take about 7 s of
  real time each. Not investigated; the pre-commit hook runs the full suite, so a rerun is the workaround.
- **Status (2026-11-01): fixed.** The GitHub workflow (Linux) failed on `test_a_slow_backfill_does_not_skip_a_cycle`,
  `test_a_stalled_source_does_not_delay_a_round`, `test_unknown_write_outcome_during_backfill_marks_the_symbol_uncertain`
  and `test_cli_backfill.py::test_no_backfill_disables_it` (exit `4` instead of `0`); all four failed every time in a
  clean `python:3.12-bookworm` container, before and after the S21 commits. **Cause:** `ManualTime.settle()` granted a
  fixed number of event-loop hops, but the code under test reads and writes SQLite on worker threads
  (`asyncio.to_thread`, `AsyncCandleWriter`). On a slower thread hand-off the simulation saw "nothing is waiting", or
  simulated time ran past the shutdown grace while a write was still in flight (the exit `4`). **Fix:** `settle()`
  also waits, in real time, for every job handed to an executor (`tests/collector/manual_time.py`; a test that blocks a
  thread on purpose passes `wait_for_threads=False`). Pinned by `tests/collector/test_manual_time.py`; the suite
  passes in the Linux container and on macOS, six repeated Linux runs of the affected files were green.

## From S18 (`GET /v1/indicators`)

### F-20. RSI reads "0" where it is undefined (0/0), and the value depends on `start`

- **Where:** `src/quasar_market_data/api/indicator_engine.py`, pinned by `tests/api/test_indicator_engine.py::test_r5_constant_closes` and `tests/api/test_indicators_endpoint.py::test_flat_range_after_movement_reads_zero_only_when_the_whole_range_is_flat`.
- **What (observed on the locked build, ta-lib 0.8.2, C library 0.8.1; the TA-Lib source was not consulted):** TA-Lib returns exactly `0.0` only when the average gain and the average loss are BOTH exactly zero over the whole computation range from the anchor to that point (the 0/0 case, where RSI is undefined). A market that turns flat after moving keeps its last RSI ratio (for example 10.687 stayed flat over 2000 constant slots). So one slot can read `"0"` or about 64 depending on `start`: with RSI(2), closes moving up to the slot before `s` and constant from `s`, slot `s+50` reads `"63.953414949372856"` for `start=s`, `"63.955521365130394"` for `start=s+10`, `"0"` for `start=s+21` and `"0"` for `start=s+40`. A consumer must not read `"0"` as a market statement (oversold or otherwise). For the 0/0 case this contradicts the 'slightly different (observed below 0.01 RSI points)' sentence of contract 6.3.6.
- **Same split in ADX, +DI and -DI (S19, observed on the locked build):** with `adx:period=14` they read exactly `0` when the whole range from the anchor is flat (every high = low = close), and keep a frozen non-zero value when the anchor lies before a run that turned flat after moving (for example `adx` 16.16, `+DI` 16.63, `-DI` 23.04 at slot 299 for a run flat from slot 100, anchored at slot 0). ATR is `0` over a flat range, which is a true statement. Pinned by `tests/api/test_indicator_convergence.py::test_flat_run_adx_and_di_read_zero_only_when_the_whole_range_is_flat` and `::test_flat_run_atr_is_zero_and_bbands_collapse`. The decision below applies to every indicator that shows the split (RSI, ADX, +DI, -DI), and both characterization tests must be updated together with it.
- **Decision for the user (not taken):** (a) emit `null` with a new reason for the 0/0 point (a contract change), or (b) keep the value and correct contract 6.3.6.
- **Test:** the characterization test `test_flat_range_after_movement_reads_zero_only_when_the_whole_range_is_flat` pins the current behaviour and must be updated together with that decision.

### F-21. The cost of indicator requests is measured only on one machine, warm cache, up to 8 concurrent requests

- **Where:** `tests/api/bench_indicators.py` (`make bench-indicators`).
- **Measured (S19, docs/reference.md):** the worst-case request (2016 slots, ten items over six indicators, read range 3516 slots, body about 2.4 MB) in process, and against a separate `quasar-api serve` process at 1, 2, 4 and 8 concurrent requests, with the body build and encoding in the thread pool (J7, implemented in S19): request latency, `GET /health` latency probed every 10 ms (and probes above 100 ms), each server's CPU time and its lifetime peak RSS (one server per level, reaped with `os.wait4`). The S18 on-event-loop figure (27-41 ms of body build) is kept in docs/reference.md only as a reference; the benchmark has no switch to turn J7 off, so it does not measure the gain.
- **Not measured:** memory of one request (the peak covers the whole life of the server, start-up included), a cold disk, other machines or platforms, more than 8 concurrent requests, several series or symbols requested at once, a long-running server (leaks, fragmentation). The `/health` probe shares a Python process with the client that decodes the 2.4 MB bodies, so its latency is an upper estimate of the server's. There is no pass/fail criterion and no rate limit (contract 5.6, accepted risk).

## From S21 (Docker Compose deployment)

### F-22. The memory limits are measured once (and in a run where four symbols never backfilled)

- **Where:** the S21 memory measurement (plan Task 3 step 5, comment at `mem_limit` in `compose.yaml`).
- **What (observed once, 2026-10-29/30, OrbStack engine 29.8.2, linux/amd64 emulated on arm64):** BTC, SOL, DOGE, LTC, XRP and AVAX each backfilled to 30 days (8867 rows) and then logged `status=no_targets`; ETH, BNB, ADA and LINK never logged a `backfill symbol=` line and kept only the rows of the live rounds. `/health/ingest` reported all 10 series `current`, so the live path was not affected. **Cause (found and fixed 2026-10-30):** `Backfiller._run` (`src/quasar_market_data/collector/backfill.py:151-159`) computes `index = (self._cursor + step) % n` but reassigns `self._cursor = index + 1` inside the loop, so the stride grows (0, 2, 5, 9, 4, ...) and with 10 symbols indices 1, 3, 6 and 8 are never visited, in any window; only 1, 2, 4 and 8 symbols avoid it. Reproduced by a simulation of the arithmetic, by a native arm64 run without Docker (same order: BTC SOL DOGE LTC XRP BTC AVAX DOGE XRP) and on the stock-engine VM. The S16 tests use at most two symbols, where it does not show. The live rounds and `/health/ingest` are unaffected, so the failure is silent: the four symbols never got history or interior holes repaired. Fixed: the steps now count from the cursor at the start of the window, pinned by `tests/collector/test_backfill.py::test_one_window_visits_every_symbol_once_in_order` and `::test_the_next_window_resumes_after_the_last_visited_symbol`; a native run after the fix backfilled all ten symbols in config order in the first window (8891 rows each). Still open below: the memory limits.
- **Memory limits:** `mem_limit` 384m (collector) and 576m (api) are about twice the cgroup `memory.peak` of that one emulated run (174 and 260 MiB), which includes page cache and did not cover the backfill of the four symbols above. Re-measure on the VPS after the first deployment.
- **H1 condition 7 on the VPS:** the stock-engine isolation was proven on a local VM (design Appendix B.1). The VPS run (B.2) is part of the release check before the first `docker compose pull` (plan Task 6 step 7).

## From the 2026-11-01 code review

### F-23. Server time was observed once per symbol per round (2 adapter calls per symbol), which matters at 1000 symbols

- **Status (2026-11-01):** fixed for the collection loop by `SharedServerTime` (plan `2026-11-01-shared-server-time.md`, INV-070): one observation per about 5 s is shared by all symbols and retries, so a round needs about N adapter calls plus a few time requests. Still open: backfill pages observe server time once per page, and the real round duration at 100 to 1000 symbols is not measured (run a live round with the target universe). The original finding follows.

- **Where:** `src/quasar_market_data/collector/fetch_once.py` (one `fetch_server_time` and one `fetch_klines` per symbol per attempt), `rate_limiter.py`.
- **What (arithmetic from the code, not measured):** one round needs at least 2 grants per symbol. At 1000 symbols and `min_request_interval_ms = 100` that is about 2000 grants, about 200 s of a 300 s cycle before any retry or backfill, and `/health/ingest` shows the last symbols as `stale` for part of every cycle (contract 6.2.3, "A round longer than the lag threshold"). Sharing one server-time observation across the symbols of a round would halve the grants; it changes the ADR-0001 P-4 freshness argument (`max_age_ms`) and was not done.
- **Fixed in the same review:** the rate-limiter wait no longer counts as round-trip time. `KlineSource.fetch_server_time` returns a `ServerTimeObservation` stamped at the network call, after the limiter's grant, so a long queue (many symbols, high `max_concurrency`) cannot make time untrusted (`tests/collector/test_fetch_once.py::test_time_spent_waiting_for_the_rate_limiter_is_not_round_trip_time`).
- **Review finding, no action:** the review reported `500 internal_error` from `/health/ingest` for a stored timeframe label the domain does not know as a defect. Contract 6.2.3 states it as intended (a series that cannot be judged makes the whole answer `500`, never a made-up status).

## From the overview dashboard redesign (2026-11-06)

### F-24. The symbol detail page does not exist; the overview links to it

- **Where:** `tools/symbol_overview/page.html` (every name in "Needs attention" and "Healthy" links to `#/symbol/<symbol>?venue=...&timeframe=...`).
- **What:** the links lead nowhere (the hash changes, nothing is drawn). The 30-day strips, the strip legend, the per-symbol history start and the per-symbol funding line were removed from the overview by the redesign (they live in git history, in the commit before `feat(overview): redesign the page as a dashboard`); `/overview.json` still carries `strip`, `oldest_*`, `newest_*` and the funding fields. A tool that hangs or a tunnel that stalls now shows the error state after 90 s instead of waiting for ever.
- **Fix:** build the detail view from the fields already in `/overview.json` (one series per link; the hash carries venue and timeframe because a symbol name can occur in more than one series). Do not change the API for it.
