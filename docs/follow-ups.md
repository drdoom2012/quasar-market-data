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

### F-10. The series-wide hole aggregate is a start-up cost that has not been measured

- **Where:** `src/quasar_market_data/collector/reconcile_reader.py`, `tests/collector/bench_reconcile.py`.
- **What:** the aggregate scans every series' primary key at every start, and again for the post-check. Each read is one
  index scan per symbol, awaited inline, not interruptible by a stop signal, so it delays shutdown and the start of
  round two by its duration. `make bench PROFILE=medium|target` measures it and has **not been run**. The acceptance
  criteria are C1 to C3 of D13 and the decision is the user's (HARD STOP 2): `quasar-collector run` must not be used
  against a real database with long history until the user accepts the numbers. S23 repeats the measurement on a soak
  database.

### F-11. `fetch-once` accepts a delay that no limit can serve

- **What:** `--publication-delay-ms` is accepted by `fetch-once` with no check against `--limit`; `run` refuses such a
  pair before opening anything.

### F-12. A1 is diagnosed in round one only

- **What:** the server-clock lead is observed in the first round. Continuous drift monitoring belongs to S17.

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
