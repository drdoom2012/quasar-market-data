# Follow-ups

Known gaps that were deliberately left open. None of them blocks the slice that found it. Each entry says where it
was found, what is wrong, and a suggested fix. Remove an entry when it is fixed (in the commit that fixes it).

## From the S10 review (`GET /v1/candles`, independent review of the implemented branch)

### F-1. The echoed `end` can exceed the valid time range

- **Where:** `src/quasar_market_data/api/candle_reader.py` (effective window), contract `docs/contracts/api-v1.md` section 5.1.
- **What:** with no `end`, the API echoes `end = start + timeframe` (or `newest + timeframe`). For
  `start=4102444800000` (the maximum valid time) the echoed `end` is `4102445100000`, above the contract maximum. A
  client that follows the contract advice "use the echoed `end` as the next `start`" then gets `400`. The slot at the
  maximum also cannot be requested with an explicit `end`, because an explicit `end` above the maximum is `400`.
- **Fix:** decide in the contract: either allow an `end` up to `MAX + timeframe`, or stop the window at the maximum
  and say so. Add a test for the boundary. Needs a contract change, so it needs approval.

### F-2. A new or empty database answers `503 schema_mismatch` without `Retry-After`

- **Where:** `src/quasar_market_data/api/database.py`, contract section 2, README (`quasar-api serve`).
- **What:** while the Collector creates and migrates the file there is a window in which the file exists but is empty
  or unmigrated. The API answers `schema_mismatch`, which the contract says "needs a deployment, not a retry", so
  clients are told not to retry during a normal start-up.
- **Fix:** either add a contract note that this window exists, or answer `database_unreadable` (with `Retry-After`)
  for a file that is empty or has `application_id` 0 and no objects, and keep `schema_mismatch` for a foreign or
  wrong-version database. The second is a behaviour change and needs approval.

### F-3. `received` of an undecodable query shows the start of the query, not the bad part

- **Where:** `src/quasar_market_data/api/params.py` (`_undecodable`).
- **What:** for a malformed escape, invalid UTF-8 or a raw non-ASCII byte, `received` is the first 64 characters of the
  whole query string. That is usually the valid prefix.
- **Fix:** cut a window around the first offending position instead. Test with a long valid prefix.

### F-4. The `Allow` header is read from a key that never matches

- **Where:** `src/quasar_market_data/api/app.py` (`http_exception` handler): `exc.headers.get("allow", "GET")`.
- **What:** Starlette stores the header as `"Allow"`, so the lookup always falls back to `"GET"`. The answer is right
  only because the route is `GET` only.
- **Fix:** look the header up case-insensitively, or drop the lookup and use the single allowed method on purpose.
  A test with a second route would show the difference.

### F-5. Unhandled errors are logged twice under uvicorn

- **Where:** `src/quasar_market_data/api/app.py` (`unhandled` handler).
- **What:** Starlette re-raises after the handler has sent the 500, so uvicorn logs a second traceback ("Exception in
  ASGI application") without the correlation id. The response body is correct.
- **Fix:** log the correlation id in a middleware that also stops the re-raise, or accept the duplicate and document
  it. Low value; log noise only.

### F-6. Absolute-form request targets answer `404`

- **Where:** routing (Starlette/uvicorn); observed with `GET http://host/v1/candles?...`.
- **What:** RFC 7230 requires servers to accept the absolute form; the API answers `404 not_found`.
- **Fix:** probably out of scope for v1 (clients and proxies use the origin form). Decide and, if kept, state it in
  contract section 1.

### F-7. Smaller notes from the same review

- **Process-test port race** (`tests/test_api_process.py`): if a foreign process binds the chosen port while the
  child is still importing, its answer arrives as a JSON or assertion error instead of a retry. Negligible chance;
  fix only if the test ever flakes (for example by asserting the answer comes from our app before using it).
- **SQLite version re-check per request:** `open_reader` re-checks the SQLite version on every call although the
  plan says "once at startup". The result is mapped to `503` and is harmless; the cost is one comparison.
- **Live Binance demonstration:** run once by hand on 2026-10-25 (collector `fetch-once`, then `quasar-api serve`,
  `curl`); it is not part of `make check`.

## From the S11 review (coverage calculation)

### F-8. S12 must list `due`, bound the lag threshold and state `as_of`

- **Where:** `docs/contracts/api-v1.md` section 6 (`/v1/coverage`, S12); `domain/timed_coverage.py`.
- **What:** contract section 6 names only `stale` and `not_yet_expected`; the domain also has `due` (expected, within
  the lag threshold). A huge `lag_threshold_ms` keeps every slot `due`, so the configured value must be bounded.
  The answer is only meaningful with the `as_of` and the rule it was computed with.
- **Fix:** S12 lists `due` in its contract section, bounds the configured lag threshold, and states `as_of` and the
  rule (publication delay, lag threshold) in the response.
