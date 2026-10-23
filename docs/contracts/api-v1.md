# Quasar Market Data API v1 — contract

- **Status:** APPROVED by the user on 2026-10-23, after an independent review (decisions Q1–Q8 and R1–R7 as recommended, section 9; the fixed `limit` maximum of 1000, R6, included). Nothing in this document is implemented yet. Sections for the reserved endpoints (section 6) still need their own approval before their slices.
- **Scope of this draft:** the conventions shared by every `/v1` endpoint (sections 1–4, 7) and the full specification of `GET /v1/candles` (section 5), which is what S10 implements. The other endpoints are only reserved (section 6) and get their own sections, approved before their slices.
- **Inputs:** `docs/architecture.md` §4.2, §8; spec `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (§6.3, §11.3–11.5, D5); ADR-0001 (P-3, P-6, P-8); ADR-0002 (C-3, C-5, P-2, P-5, P-6, P-7, P-8); `docs/invariants.md` (INV-007, INV-009, INV-013, INV-014, INV-028).

## 0. What a consumer can rely on

1. Every candle returned is a **confirmed closed** candle stored by the Collector. The still-forming candle is never returned. This holds as long as every ingestion path routes through the closed-candle selection (INV-007: today only `quasar-collector fetch-once` does; the collection loop and backfill, S14 and S16, must).
2. Prices and volumes are **exact decimal text** exactly as stored; the API never converts them to floating point.
3. The API **never contacts an exchange and never writes**. This is guaranteed by construction once S10 exists (INV-013 and INV-014 are PLANNED for S10; they are not enforced yet).
4. **A response never implies completeness it cannot show.** Every candle response carries a `coverage` block and the series' `oldest`/`newest` stored open times. The absence of a candle is never evidence that nothing traded.
5. A candle in a *conflicted* slot (section 4) is still returned, **with the value first stored**, flagged `conflicted: true`. The rejected value is not exposed in v1 (ADR-0002 C-3, P-7). **Consumers should treat a `conflicted: true` candle as untrusted**: the exchange is known to revise a candle for a few seconds after it closes (ADR-0001 P-6), so the first stored value may be the wrong one.
6. When the API cannot read the database it understands, it answers **503**, never an empty result.

What the API does **not** promise: that a closed candle can never be revised by the exchange (a revision shows up as a conflict), freshness (a series that stopped growing three days ago still answers `gap_free: true` for a window that ends at its newest candle; the age is visible in `series.newest_open_time_ms`, and staleness is evaluated by `/v1/coverage`, section 6), or that the stored history reaches back to any date.

## 1. Conventions

| Topic | Rule |
|---|---|
| Base path | `/v1`. The version is in the path. A trailing slash is a different path (`404 not_found`); the API never redirects. |
| Methods | `GET` only. `HEAD`, `OPTIONS` and everything else is `405 method_not_allowed` with an `Allow: GET` header and the standard error body. |
| Encoding | `application/json; charset=utf-8`, UTF-8. Symbols may contain non-ASCII characters (ADR-0001 P-8). In a query string `/` and `:` are legal unencoded; `%`, `+`, `&`, `#`, spaces and non-ASCII characters must be percent-encoded. |
| Time | Integer epoch **milliseconds**, UTC (`open_time_ms`, `start`, `end`, `next`, `series.*`, `span.*`). No ISO strings, no time zones. A candle covers `[open_time_ms, open_time_ms + timeframe)`. Valid times are `946684800000 <= t <= 4102444800000` (the range the schema accepts); anything else is `400`. |
| Decimals | JSON **strings** in the canonical stored form: digits and at most one `.`, no sign, no exponent, no leading zero (except `0.x`), no trailing fractional zero (`"82709"`, not `"82709.0"`). Consumers must parse them as decimals, never as floats. `turnover` is `null` when the venue did not provide it; it is never computed. |
| Integers | Counts and times are JSON integers. Integer query parameters must match `^[0-9]{1,16}$` (ASCII digits only, no sign, no `_`, no spaces, no decimal point); anything else is `400`. |
| Strings | `venue` and `timeframe` are matched exactly. `symbol` is 1–64 characters, no whitespace or control characters, valid UTF-8 after percent-decoding, matched case-sensitively and without Unicode normalization (ADR-0001 P-8). A syntactically invalid symbol is `400`; a valid but unknown one is `404`. |
| Parameters | Unknown query parameters, repeated parameters and empty values are rejected (`400`), so a typo such as `limt=10` fails instead of being ignored. Names and values are case-sensitive. |
| Caching | `Cache-Control: no-store`. A window that is gap-free now can change when the Collector backfills or records a conflict. |
| Compatibility | Within `/v1`, new response fields (including new fields in `coverage` and `series`) and new optional query parameters may be added. Clients must ignore unknown response fields and must send only documented parameters (an older server rejects an unknown one). Removing or renaming a field, changing a meaning, or tightening a limit needs `/v2`. |
| Exposure | No authentication in v1. The API binds to localhost or a private network by default (architecture §9). |

## 2. Errors

Every error **the application produces**, from every endpoint, has the same body and no other shape (framework-generated errors are converted, including unhandled exceptions):

```json
{
  "error": {
    "code": "invalid_parameter",
    "message": "limit must be between 1 and 1000",
    "details": { "parameter": "limit", "received": "5000" }
  }
}
```

`code` is one of a closed list (a new code is an additive change); `message` is for humans and may change; `details` is an object whose keys are documented per code and may be empty. `received` is the raw input cut to 64 characters. The body never contains stack traces, file paths, SQL, or other internals.

Out of this contract: errors the HTTP server itself produces before the application sees the request (malformed request line, oversized URI or headers, unsupported HTTP version). They are plain server responses, not this body.

| HTTP | `code` | When | `details` |
|---|---|---|---|
| 400 | `invalid_parameter` | A parameter is missing, malformed, out of range, repeated, empty, or unknown; `start`/`end` is not aligned to the timeframe or outside the valid time range; an explicit `end <= start`; the window is longer than 10 000 slots; the timeframe or venue is not served; the symbol is syntactically invalid | `parameter`, `received` (omitted when missing), `maximum` (for limits and window length) |
| 404 | `unknown_series` | No **candle** of this `(venue, symbol, timeframe)` has ever been stored. A typo and a not-yet-ingested symbol look the same, and a newly configured instrument answers 404 until its first candle is stored. An *existing* series with nothing in the window is a `200` with an empty list | `venue`, `symbol`, `timeframe` |
| 404 | `not_found` | Unknown path | none |
| 405 | `method_not_allowed` | Not `GET` | `allow` |
| 500 | `internal_error` | A bug | none (a correlation id is logged, not returned) |
| 503 | `not_ready` | The database file is missing, is not this project's, has a schema version other than the one this API was built for, or cannot be read. The API stays up and answers every `/v1` request this way until the condition clears (INV-028) | `reason`: `database_missing`, `schema_mismatch` or `database_unreadable` |

Rules:

- **Order of checks:** parameter validation (`400`) comes first because it needs no database; then database readiness (`503`); then the series lookup (`404`). A request that is both malformed and hits a broken database answers `400`.
- **Readiness is checked on every request** (the file exists, the project's `application_id`, the schema version), not only at startup, so a deleted or replaced file is noticed.
- `database_unreadable` also covers corruption, I/O errors, a busy timeout and a WAL database that cannot be opened read-only. `500` is reserved for bugs.
- `503` carries `Retry-After: 5` for `database_missing` and `database_unreadable`, and **no** `Retry-After` for `schema_mismatch` (it needs a deployment, not a retry). Clients must treat `503` as "no data", never as "empty data".

## 3. Pagination

Pagination is by **time**, not by offset:

- A request selects stored candles with `start <= open_time_ms < end`, in **ascending** `open_time_ms`, at most `limit` of them.
- `next` is **always present**. It is the open time of the first stored candle **not** returned, or `null` when no stored candle exists in `[last returned open time + timeframe, end)` (also when nothing was returned). Whenever it is not `null` it is strictly greater than the last returned `open_time_ms` and less than `end`, so a client that passes it as `start` always makes progress and always terminates. The implementation must therefore read `limit + 1` candles: a final page that is exactly full has `next: null`.
- To continue, pass `next` unchanged as `start` **and pass the echoed `end` as `end`**. A client that omits `end` lets it move between pages on a live series.
- Gaps between the last returned candle and `next` are attributed to the page that returned `next` (its `coverage.span` ends at `next`).
- **Pages are not a snapshot.** Each request is one read transaction (section 5.6). Between two requests the Collector may add candles, including backfilled ones before the cursor, and may record conflicts. A consumer that needs one consistent picture reads the pages and then checks `/v1/coverage` for the whole window.

## 4. Data semantics

A **slot** is one timeframe interval of one series, aligned to the epoch (`open_time_ms % timeframe == 0`). For every slot in a span the following facts are looked up in stored rows only; the contract uses **no clock**:

- `C`: a stored candle exists;
- `U`: the Collector recorded that the exchange confirmed it has no data for the slot (`unavailable_candles`, ADR-0002 P-6; the Collector does not write these before S16, so `unavailable_slots` is 0 until then);
- `X`: at least one conflict record exists (`candle_conflicts`, ADR-0002 P-5).

Each slot of a span gets **exactly one** state, the first that matches (ADR-0002 P-7):

| Order | State | Condition | Meaning |
|---|---|---|---|
| 1 | `conflicted` | `X`, or both `C` and `U` | The exchange later sent a different value, or the two records disagree. A candle, if stored, is returned with its first stored value and `conflicted: true`. No resolution path in v1 (ADR-0002 Q3). |
| 2 | `present` | `C` | Exactly one stored candle, no recorded disagreement. |
| 3 | `unavailable` | `U` | The exchange confirmed it has no data. The candle does not exist and will not be fabricated. |
| 4 | `missing` | none of the above, and the slot is before the end of the series' **newest** stored candle | Unknown reason: not backfilled yet, or the history does not reach back that far (slots before the oldest stored candle are `missing`). |
| 5 | `beyond_stored` | none of the above, and the slot is at or after the end of the newest stored candle | Not evaluated: it may be not yet published, or stale. Needs the clock and the publication delay (`/v1/coverage`). |

Hence, for every span: `slots = present + conflicted + unavailable + missing + beyond_stored`.

## 5. `GET /v1/candles`

### 5.1 Request

| Parameter | Required | Meaning |
|---|---|---|
| `venue` | yes | `binance-usdm` in v1. |
| `symbol` | yes | CCXT unified symbol, exactly as stored, e.g. `BTC/USDT:USDT`. |
| `timeframe` | yes | `5m` in v1. |
| `start` | yes | Inclusive window start, epoch ms, aligned to the timeframe. |
| `end` | no | **Exclusive** window end, epoch ms, aligned to the timeframe. If given it must be `> start`. Default: see below. |
| `limit` | no | Maximum candles in this response, `1`–`1000`, default `1000`. |

**Effective window.** The window never exceeds **10 000 slots** (`end - start <= 10000 × timeframe`; for `5m` about 34.7 days, which covers the planned 30-day backfill depth, spec D5). An explicit `end` beyond that is `400` with `details.maximum`. When `end` is omitted:

- if `start` is before the end of the series' newest candle, `end` is the end of that candle (`newest + timeframe`), capped at `start + 10 000 × timeframe`;
- otherwise (the caller asks for data after everything stored) `end = start + timeframe`: a one-slot window that is entirely `beyond_stored`, so the answer is `200` with `candles: []`. This is the normal "anything new since T?" poll.

The response always echoes the effective `start` and `end`. To read past a capped window, use the echoed `end` as the next `start`.

The cost of a request is bounded: at most `limit` candle rows, plus counts over at most 10 000 slots in the primary-key range of `candles`, `unavailable_candles` and the `candle_conflicts_slot` index.

`limit` is a **contract maximum of 1000 and a fixed default of 1000** in v1; it is not configurable, because lowering it later would be a breaking change (section 1, Compatibility). Spec D5 listed the API limit among the starting values that are configuration; this contract fixes it instead (decision R6).

There is no way to ask for "the latest N" in v1 (decision Q3); `series.newest_open_time_ms` tells a consumer where the series ends, so `start = newest - (N-1) × timeframe` reads the last N slots.

Example:

```text
GET /v1/candles?venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start=1792643400000&limit=3
```

### 5.2 Response `200`

```json
{
  "venue": "binance-usdm",
  "symbol": "BTC/USDT:USDT",
  "timeframe": "5m",
  "start": 1792643400000,
  "end": 1792673400000,
  "series": {
    "oldest_open_time_ms": 1792643400000,
    "newest_open_time_ms": 1792673100000
  },
  "candles": [
    {
      "open_time_ms": 1792643400000,
      "open": "82855.4",
      "high": "82866.1",
      "low": "82768.1",
      "close": "82768.2",
      "volume": "369.221",
      "turnover": "30587379.8759",
      "conflicted": false
    },
    {
      "open_time_ms": 1792643700000,
      "open": "82768.2",
      "high": "82768.2",
      "low": "82664.7",
      "close": "82709",
      "volume": "580.95",
      "turnover": "48054366.2802",
      "conflicted": false
    },
    {
      "open_time_ms": 1792644000000,
      "open": "82709.1",
      "high": "82765.3",
      "low": "82686.2",
      "close": "82744.5",
      "volume": "380.864",
      "turnover": "31506256.9708",
      "conflicted": false
    }
  ],
  "next": 1792644300000,
  "coverage": {
    "span": { "start": 1792643400000, "end": 1792644300000 },
    "slots": 3,
    "present_slots": 3,
    "conflicted_slots": 0,
    "unavailable_slots": 0,
    "missing_slots": 0,
    "beyond_stored_slots": 0,
    "gap_free": true
  }
}
```

- The example is **illustrative**: it shows a database loaded from the committed fixture `tests/fixtures/binance/btcusdt_5m_closed_raw.json` (100 consecutive 5m candles, values in canonical form), which is why the series ends at `1792673100000`.
- `start` and `end` echo the effective window. `series` is present on every `200`: the open times of the oldest and newest stored candle of the series (a series that exists has at least one candle, and candles are never deleted).
- `candles` are ascending, unique by `open_time_ms`, each with exactly the fields shown. `conflicted` is always present. `turnover` may be `null`.

### 5.3 The `coverage` block

`coverage` speaks for **this page only**: the span `[span.start, span.end)` where `span.start = start` and `span.end` is `next` when `next` is not `null`, else `end`. It is computed from stored rows by the states of section 4:

| Field | Meaning |
|---|---|
| `slots` | Number of slots in the span. |
| `present_slots`, `conflicted_slots`, `unavailable_slots`, `missing_slots`, `beyond_stored_slots` | Number of slots in each state. They always sum to `slots`. |
| `gap_free` | `true` only when `conflicted_slots`, `unavailable_slots`, `missing_slots` and `beyond_stored_slots` are all `0`, i.e. every slot of the span has exactly one stored candle and no recorded disagreement. |

- `gap_free` is about **the span only**. It says nothing about the age of the data: with a defaulted `end` the span stops at the newest stored candle, so a series that died long ago still reports `gap_free: true` there. Use `series.newest_open_time_ms` for age and `/v1/coverage` for staleness.
- Returned candles with `conflicted: true` are exactly those conflicted slots that have a stored candle. A conflicted slot without a stored candle is counted but has nothing to flag.
- `gap_free: false` is never an error; it is the normal answer for a series that is being backfilled or that has a gap. The response lists no individual gaps; `/v1/coverage` will.
- An empty `candles` list with `gap_free: false` is an honest answer ("nothing stored in this span"); it is never evidence that the market was closed.

### 5.4 Errors

`400 invalid_parameter`, `404 unknown_series`, `503 not_ready`, `500 internal_error`, in the order of section 2.

### 5.5 Behavior that must be tested (informative, for the S10 plan)

Values are returned byte-for-byte as stored; `limit` and the 10 000-slot window are enforced; `limit + 1` is read, so an exactly full last page has `next: null`; `next` is strictly increasing and a paging loop terminates; a window crossing a gap reports `missing_slots`, and slots before the oldest candle are `missing`; a conflicted slot is flagged, returns the first stored value and is counted once; a slot with both a candle and an unavailable record is `conflicted`; an unavailable-only slot is counted as `unavailable`, never fabricated; the five counts always sum to `slots`; a `start` after the newest candle with no `end` answers `200` with an empty list and `beyond_stored_slots: 1`; an explicit `end <= start` is `400` but a defaulted one never is; unknown series is `404` while an existing series with an empty window is `200`; integer parameters like `1_000`, ` 5`, `1.0` and non-ASCII digits are `400`; every application-produced error uses the section 2 body, including `405` on `HEAD` and `404` on a trailing slash; a database with a different schema version, a missing file and an unreadable file answer `503` with the right `reason` and `Retry-After`, checked on every request including after the file is replaced; the handler cannot write (read-only connection) and does not import an exchange library.

### 5.6 Implementation requirements (normative for S10)

- All queries of one request — the readiness check, the series lookup, the candle read, the counts and `series` — run inside **one explicit read transaction** (the reader connection is in autocommit mode, so each statement would otherwise see its own snapshot). `conflicted_slots` must agree with the returned flags.
- SQLite calls run off the event loop (a worker thread or pool), so a busy timeout cannot stall unrelated requests.
- Accepted risks, not in scope for v1: no rate limiting, so many concurrent 1000-row requests are possible; no authentication.

## 6. Reserved endpoints

Each is specified in its own section of this document and approved before its slice. They share sections 1–4.

| Endpoint | Slice | Intent |
|---|---|---|
| `GET /v1/coverage` | S12 | Per-slot states for a window (section 4 plus the clock-dependent states: stale and not-yet-expected, spec §11.4), with the `as_of` time and ingestion-lag threshold stated in the response. This is where individual gaps are listed. |
| `GET /v1/instruments` | S20 | The configured universe and, per instrument, the newest stored candle. |
| `GET /v1/indicators` | S18 | Indicators computed on demand from stored closed candles; refuses to compute across a gap, a conflicted or unavailable slot, or with insufficient warm-up, and says why (architecture §8). |
| Health | S17 | Liveness separate from readiness (architecture §8). Path and body are decided in S17; the `503 not_ready` reasons of section 2 are the readiness vocabulary. |

## 7. Out of scope for v1

Authentication, rate limiting, CORS, write endpoints, streaming, candle revision history, exposing rejected conflict values, a configurable page limit, timeframes other than `5m`, venues other than `binance-usdm`, and any trading functionality.

## 8. Traceability

| Contract statement | Source |
|---|---|
| Only closed candles are returned | INV-007, ADR-0001 P-3 |
| Decimal strings, `turnover` nullable | ADR-0002 P-2, spec D4, INV-006 |
| Conflicted slot returns the stored value, flagged; candle plus unavailable record is conflicted | ADR-0002 C-3, P-5, P-7, Q3 |
| Unavailable slots are counted, never fabricated | ADR-0002 P-6, spec §4 |
| 1000 per request, paginated | spec D5 (fixed by this contract, R6) |
| `start`/`end` bounds and `next` cursor = next open time | spec §11.5 |
| Time range of valid timestamps | `candles.open_time_ms` CHECK in migration 001 |
| Symbol rule | ADR-0001 P-8, ADR-0002 C-5 |
| Read-only, no exchange access | architecture §4.2, INV-013, INV-014 |
| `503` on schema mismatch, checked per request | ADR-0002 P-8, C-6, INV-028 |
| Coverage derived on read, not stored | ADR-0002 C-4, spec §11.4 |

## 9. Decisions (answered by the user on 2026-10-23: all as recommended)

Original decisions:

| # | Question | Decision | Alternative that was not chosen, and its cost |
|---|---|---|---|
| Q1 | How much does the candle response say about completeness? | A clock-free `coverage` block (counts and a flag; individual gaps via `/v1/coverage`), plus `series`. It keeps S10 independent of S11/S12. | List gap runs in the candle response: more useful, but unbounded size and it pulls part of S11 into S10. Or no block until S12: contradicts the anti-goal "a response that implies completeness when coverage is partial". |
| Q2 | Is `venue` required? | Required: explicit now, so a second venue (S22) changes nothing. | Default `binance-usdm`: shorter URLs, but a silent default that must be removed or kept forever. |
| Q3 | "Latest N candles" (descending order or a `before` cursor)? | Not in v1 candles: ascending only; `series.newest_open_time_ms` lets a consumer compute the start; indicators (S18) do their own look-back. It can be added later without breaking anything. | Add `order=desc` now: doubles the pagination cases (mirrored cursor) and the tests. |
| Q4 | Time as epoch ms only, or also ISO strings? | Epoch ms only: one unambiguous representation, equals the stored form. | Add `open_time` ISO: friendlier to humans, a second representation that can disagree in tooling. |
| Q5 | Expose `ingested_at_ms` per candle? | No: it is diagnostic and excluded from "identical" comparison (ADR-0002 P-2). Freshness belongs to `/v1/coverage`. | Expose it: lets a consumer reason about settling, but ties them to a field the schema calls diagnostic. |
| Q6 | HTTP status for a bad parameter: `400` or FastAPI's `422`? | `400` for all parameter errors, with our body (section 2); the framework's own validation body is replaced. | `422`: framework default, but the body differs from every other error unless converted anyway. |
| Q7 | Schema mismatch or missing database: serve or refuse? | `503 not_ready` for every `/v1` request while the API stays running, so liveness and readiness stay separable (S17). | Refuse to start: simpler, but a Collector restart order would then decide whether the API is up. |
| Q8 | Where is health (architecture §8 `GET /health`)? | Decide in S17; this draft only reserves the readiness vocabulary. | Fix it now: no information yet to justify it. |

Decisions from the independent review (the user followed all recommendations):

| # | Question | Decision |
|---|---|---|
| R1 | Default `end` with `start` at or after the newest candle: `400` or `200`? | `200`, empty list, one-slot window, `beyond_stored_slots: 1` (section 5.1). `end <= start` is a `400` only for an explicit `end`. |
| R2 | Say where the series ends? | Every `200` carries `series.oldest_open_time_ms` and `series.newest_open_time_ms`; `complete` is renamed `gap_free` and documented as span-scoped and age-blind. |
| R3 | The stored-and-unavailable slot (ADR-0002 P-7) | Defined as `conflicted`; the five states are disjoint and sum to `slots` (section 4). |
| R4 | Bound the window? | Yes: at most 10 000 slots (about 34.7 days of `5m`), which bounds the `unavailable_candles` and `candle_conflicts` scans; a longer explicit window is `400`. A defaulted window is capped and the cap is visible through the echoed `end`. |
| R5 | What is `database_unreadable`; when is the database checked? | Corruption, I/O errors, busy timeout and an unopenable WAL are all `503 database_unreadable`; readiness is checked on every request; `400` precedes `503`, which precedes `404`. |
| R6 | Is `limit` 1000 a contract maximum or configuration? | A fixed contract maximum and default of 1000 in v1, not configurable. This deviates from the wording of spec D5 ("configuration") because lowering it later would be breaking. |
| R7 | Return conflicted candles by default? | Yes, always, flagged `conflicted: true`, with the consumer rule "treat as untrusted" (section 0, point 5). No opt-in parameter. |
