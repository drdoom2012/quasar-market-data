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
