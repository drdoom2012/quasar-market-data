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
  0.69 s with the guard and 0.70 s without. **The numbers are not yet accepted by the user** (HARD STOP 2 of S15):
  accepting them is the user's decision.
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

### F-17. The `/health/ingest` read cost is not accepted

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
  The criteria H1 to H3 (S17 plan, Decision 11) were proposals and are **met by these numbers, but not yet accepted by
  the user** (HARD STOP 2): do not use `/health/ingest` against a real database with long history until the user
  accepts them. Not measured: a cold cache, a real network client, many concurrent requests, more than 600 series. S23
  repeats it on a soak database. The read is linear in the number of series and has no deadline beyond SQLite's busy
  timeout (5 s).

### F-18. Stored series versus the configured universe

- **What:** `/health/ingest` judges only series that exist in the database, so an instrument whose ingestion never
  started is invisible. Comparing the configured universe with the stored series needs a universe on the API side
  (S20, `/v1/instruments`); S17 deliberately adds no Collector-to-API channel.

### F-19. Load-sensitive S16 backfill tests

- **Where:** `tests/collector/test_backfill_run.py` (`test_a_slow_backfill_does_not_skip_a_cycle`,
  `test_unknown_write_outcome_during_backfill_marks_the_symbol_uncertain`).
- **What:** each failed once in about eight full runs during S17 with `nothing is waiting: the simulation is stuck`,
  and passed on every rerun, alone and in the full suite, with and without the S17 changes. They take about 7 s of
  real time each. Not investigated; the pre-commit hook runs the full suite, so a rerun is the workaround.
