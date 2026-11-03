# Quasar Market Data API v1 — contract

- **Status:** APPROVED by the user on 2026-10-23, after an independent review (decisions Q1–Q8 and R1–R7 as recommended, section 9; the fixed `limit` maximum of 1000, R6, included). `GET /v1/candles` (section 5) is implemented by S10. `GET /v1/coverage` (section 6.1) was APPROVED by the user on 2026-10-25 (decisions S1–S5 as proposed, including the bounds 300 000 ms and 900 000 ms) and is implemented by S12. Sections for the other reserved endpoints (section 6) still need their own approval before their slices. `GET /v1/indicators` (section 6.3) was APPROVED by the user on 2026-10-28 (decisions I1–I6 as proposed) and is implemented by S18. `GET /v1/open-interest` and the `open_interest` object of `GET /health/ingest` (section 6.5) were APPROVED by the user on 2026-11-03 for OI-5 (ADR-0003 P-11), decisions OA1–OA11 as proposed (section 9), and are implemented by OI-5.
- **Scope of this draft:** the conventions shared by every `/v1` endpoint (sections 1–4, 7) and the full specification of `GET /v1/candles` (section 5), which is what S10 implements. The other endpoints are only reserved (section 6) and get their own sections, approved before their slices.
- **Inputs:** `docs/architecture.md` §4.2, §8; spec `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (§6.3, §11.3–11.5, D5); ADR-0001 (P-3, P-6, P-8); ADR-0002 (C-3, C-5, P-2, P-5, P-6, P-7, P-8); `docs/invariants.md` (INV-007, INV-009, INV-013, INV-014, INV-028).

## 0. What a consumer can rely on

1. Every candle returned is a **confirmed closed** candle stored by the Collector. The still-forming candle is never returned. This holds as long as every ingestion path routes through the closed-candle selection (INV-007: `quasar-collector fetch-once`, `run` and its backfill do).
2. Prices and volumes are **exact decimal text** exactly as stored; the API never converts them to floating point.
3. The API **never contacts an exchange and never writes**. Read-only access is enforced (INV-014 in `docs/invariants.md`). Exchange isolation is enforced only in a narrowed form (INV-044: no exchange or network-client import in the `api` package, no outbound connection in the API tests); the full guarantee is INV-013, which is planned (S21).
4. **A response never implies completeness it cannot show.** Every candle response carries a `coverage` block and the series' `oldest`/`newest` stored open times. The absence of a candle is never evidence that nothing traded.
5. A candle in a *conflicted* slot (section 4) is still returned, **with the value first stored**, flagged `conflicted: true`. The rejected value is not exposed in v1 (ADR-0002 C-3, P-7). **Consumers should treat a `conflicted: true` candle as untrusted**: the exchange is known to revise a candle for a few seconds after it closes (ADR-0001 P-6), so the first stored value may be the wrong one.
6. When the API cannot read the database it understands, it answers **503**, never an empty result.

What the API does **not** promise: that a closed candle can never be revised by the exchange (a revision shows up as a conflict), freshness (a series that stopped growing three days ago still answers `gap_free: true` for a window that ends at its newest candle; the age is visible in `series.newest_open_time_ms`, and staleness is evaluated by `/v1/coverage`, section 6.1), or that the stored history reaches back to any date.

## 1. Conventions

| Topic | Rule |
|---|---|
| Base path | `/v1`. The version is in the path. A trailing slash is a different path (`404 not_found`); the API never redirects. |
| Methods | `GET` only. `HEAD`, `OPTIONS` and everything else is `405 method_not_allowed` with an `Allow: GET` header and the standard error body. |
| Request target | Origin form only (`/v1/candles?...`). The absolute form (`GET http://host/v1/candles?...`) is not supported and answers `404 not_found`; clients and proxies are expected to send the origin form. |
| Encoding | `application/json; charset=utf-8`, UTF-8. Symbols may contain non-ASCII characters (ADR-0001 P-8). In a query string `/` and `:` are legal unencoded; `%`, `+`, `&`, `#`, spaces and non-ASCII characters must be percent-encoded. |
| Time | Integer epoch **milliseconds**, UTC (`open_time_ms`, `time_ms`, `start`, `end`, `next`, `series.*`, `span.*`, `as_of_ms`, `rule.*_ms`, `runs[*].start`, `runs[*].end`). No ISO strings, no time zones. A candle covers `[open_time_ms, open_time_ms + timeframe)`; an open interest row with `time_ms = T` covers `[T - period, T)` (section 6.5.1). Valid times are `946684800000 <= t <= 4102444800000` (the range the schema accepts); anything else is `400`. The one exception is the exclusive `end`, which may be up to one timeframe past that maximum (`4102445100000` for `5m`), so the slot at the maximum can be requested and an echoed `end` is always valid as the next `start` or `end`. |
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
| 404 | `unknown_series` | No **candle** of this `(venue, symbol, timeframe)` has ever been stored. A typo and a not-yet-ingested symbol look the same, and a newly configured instrument answers 404 until its first candle is stored. An *existing* series with nothing in the window is a `200` with an empty list. For `GET /v1/open-interest` (section 6.5) the series is `(venue, symbol, period)` and the test is "no open interest row has ever been stored" and the message says so (decision OA11) | `venue`, `symbol`, `timeframe` (`period` instead of `timeframe` for `/v1/open-interest`) |
| 404 | `not_found` | Unknown path | none |
| 404 | `open_interest_disabled` | `GET /v1/open-interest` while the configuration file has no `[open_interest]` table or it has `enabled = false` (section 6.5.5). The deployment does not collect open interest, so the endpoint cannot judge its slots | none |
| 405 | `method_not_allowed` | Not `GET` | `allow` |
| 500 | `internal_error` | A bug | none (a correlation id is logged, not returned) |
| 503 | `not_ready` | The database file is missing, is empty (the Collector has not initialised it yet), is not this project's, has a schema version other than the one this API was built for, or cannot be read. The API stays up and answers every `/v1` request this way until the condition clears (INV-028) | `reason`: `database_missing`, `schema_mismatch` or `database_unreadable` |

Rules:

- **Order of checks:** parameter validation (`400`) comes first because it needs no database; then database readiness (`503`); then the series lookup (`404`). A request that is both malformed and hits a broken database answers `400`. `GET /v1/open-interest` adds `404 open_interest_disabled` between the parameters and the database (section 6.5.5).
- **Readiness is checked on every request** (the file exists, the project's `application_id`, the schema version), not only at startup, so a deleted or replaced file is noticed.
- `database_unreadable` also covers a file that exists but holds nothing yet (no application id, no tables), which is a normal moment while the Collector starts, corruption, I/O errors, a busy timeout and a WAL database that cannot be opened read-only. `500` is reserved for bugs.
- `503` carries `Retry-After: 5` for `database_missing` and `database_unreadable`, and **no** `Retry-After` for `schema_mismatch` (it needs a deployment, not a retry). Clients must treat `503` as "no data", never as "empty data".

## 3. Pagination

Pagination is by **time**, not by offset:

- A request selects stored candles with `start <= open_time_ms < end`, in **ascending** `open_time_ms`, at most `limit` of them.
- `next` is **always present**. It is the open time of the first stored candle **not** returned, or `null` when no stored candle exists in `[last returned open time + timeframe, end)` (also when nothing was returned). Whenever it is not `null` it is strictly greater than the last returned `open_time_ms` and less than `end`, so a client that passes it as `start` always makes progress and always terminates. The implementation must therefore read `limit + 1` candles: a final page that is exactly full has `next: null`.
- To continue, pass `next` unchanged as `start` **and pass the echoed `end` as `end`**. A client that omits `end` lets it move between pages on a live series.
- Gaps between the last returned candle and `next` are attributed to the page that returned `next` (its `coverage.span` ends at `next`).
- **Pages are not a snapshot.** Each request is one read transaction (section 5.6). Between two requests the Collector may add candles, including backfilled ones before the cursor, and may record conflicts. A consumer that needs one consistent picture reads the pages and then checks `/v1/coverage` for the whole window.

## 4. Data semantics

A **slot** is one timeframe interval of one series, aligned to the epoch (`open_time_ms % timeframe == 0`). For every slot in a span the following facts are looked up in stored rows only; sections 4 and 5 use **no clock** (section 6.1 adds the clock-dependent split of the last state):

- `C`: a stored candle exists;
- `U`: the Collector recorded that the exchange confirmed it has no data for the slot (`unavailable_candles`, ADR-0002 P-6; the Collector does not write these in v1, so `unavailable_slots` is 0 (INV-054));
- `X`: at least one conflict record exists (`candle_conflicts`, ADR-0002 P-5).

Each slot of a span gets **exactly one** state, the first that matches (ADR-0002 P-7):

| Order | State | Condition | Meaning |
|---|---|---|---|
| 1 | `conflicted` | `X`, or both `C` and `U` | The exchange later sent a different value, or the two records disagree. A candle, if stored, is returned with its first stored value and `conflicted: true`. No resolution path in v1 (ADR-0002 Q3). |
| 2 | `present` | `C` | Exactly one stored candle, no recorded disagreement. |
| 3 | `unavailable` | `U` | The exchange confirmed it has no data. The candle does not exist and will not be fabricated. |
| 4 | `missing` | none of the above, and the slot is before the end of the series' **newest** stored candle | Unknown reason: not backfilled yet, or the history does not reach back that far (slots before the oldest stored candle are `missing`). |
| 5 | `beyond_stored` | none of the above, and the slot is at or after the end of the newest stored candle | Not evaluated: it may be not yet published, or stale. Needs the clock and the publication delay; `/v1/coverage` (section 6.1) splits it into `not_yet_expected`, `due` and `stale`. |

Hence, for every span: `slots = present + conflicted + unavailable + missing + beyond_stored`.

## 5. `GET /v1/candles`

### 5.1 Request

| Parameter | Required | Meaning |
|---|---|---|
| `venue` | yes | `binance-usdm` in v1. |
| `symbol` | yes | CCXT unified symbol, exactly as stored, e.g. `BTC/USDT:USDT`. |
| `timeframe` | yes | `5m` in v1. |
| `start` | yes | Inclusive window start, epoch ms, aligned to the timeframe. |
| `end` | no | **Exclusive** window end, epoch ms, aligned to the timeframe, at most one timeframe past the maximum valid time (section 1, Time). If given it must be `> start`. Default: see below. |
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
- `gap_free: false` is never an error; it is the normal answer for a series that is being backfilled or that has a gap. The response lists no individual gaps; `/v1/coverage` (section 6.1) does.
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
| `GET /v1/coverage` | S12 | Specified in section 6.1 below. |
| `GET /v1/instruments` | S20 | The configured universe and, per instrument, the newest stored candle. Specified in section 6.4 below. |
| `GET /v1/indicators` | S18 | Specified in section 6.3 below (indicators computed on demand from stored closed candles; no value across a gap, a conflicted or unavailable slot, or before warm-up; says why). |
| `GET /v1/open-interest` | OI-5 | Specified in section 6.5 below (open interest history at 5m from stored rows, with the slot states of ADR-0003 P-7; no value is inferred for an absent row). |
| Health | S17 | Specified in section 6.2 below (`/health`, `/health/ready`, `/health/ingest`). |

## 6.1 `GET /v1/coverage`

- **Status:** APPROVED by the user on 2026-10-25, decisions S1–S5 as proposed. Implemented by S12. Decisions are in section 9.
- **What it answers:** for one series and a window of at most 10 000 slots, how many slots are in each state at the server's current time, and which runs of consecutive slots are not `present`. It is where individual gaps are listed. It reads stored rows and the server clock only.

### 6.1.1 Request

| Parameter | Required | Meaning |
|---|---|---|
| `venue` | yes | `binance-usdm` in v1. |
| `symbol` | yes | As in section 5.1. |
| `timeframe` | yes | `5m` in v1. |
| `start` | yes | Inclusive window start, epoch ms, aligned. |
| `end` | yes | **Exclusive** window end, epoch ms, aligned, `> start`, at most 10 000 slots after `start` (`400` with `details.maximum` otherwise), within the valid time range, where `end` may be one timeframe past the maximum (as in section 5.1), so the slot at the maximum can be requested. |

There is no other parameter: `as_of`, `limit` and anything else are `400 invalid_parameter` (section 1, Parameters). The clock is the server's (decision S1).

### 6.1.2 States

Each slot of the window gets exactly one state, from the section 4 table, except that the fifth row `beyond_stored` is split by the clock. With `expected_at = slot end + publication_delay_ms`:

| State | Condition |
|---|---|
| `present`, `conflicted`, `unavailable`, `missing` | As in section 4. They never depend on the clock. A stored candle is `present` even when `as_of_ms` is earlier than its own eligibility time. |
| `not_yet_expected` | `beyond_stored` and `as_of_ms < expected_at`. |
| `due` | `beyond_stored` and `expected_at <= as_of_ms <= expected_at + lag_threshold_ms`: expected, tolerated as ingestion lag, not a fault yet. |
| `stale` | `beyond_stored` and `as_of_ms > expected_at + lag_threshold_ms`. |

Hence `slots = present + conflicted + unavailable + missing + not_yet_expected + due + stale`.

### 6.1.3 Response `200`

```json
{
  "venue": "binance-usdm",
  "symbol": "BTC/USDT:USDT",
  "timeframe": "5m",
  "start": 1792672500000,
  "end": 1792674300000,
  "as_of_ms": 1792674030000,
  "rule": { "publication_delay_ms": 15000, "lag_threshold_ms": 60000 },
  "series": {
    "oldest_open_time_ms": 1792643400000,
    "newest_open_time_ms": 1792673100000
  },
  "stored_ahead_of_as_of": false,
  "coverage": {
    "slots": 6,
    "present_slots": 3,
    "conflicted_slots": 0,
    "unavailable_slots": 0,
    "missing_slots": 0,
    "not_yet_expected_slots": 1,
    "due_slots": 1,
    "stale_slots": 1,
    "fully_present": false
  },
  "runs": [
    { "state": "stale", "start": 1792673400000, "end": 1792673700000, "slots": 1 },
    { "state": "due", "start": 1792673700000, "end": 1792674000000, "slots": 1 },
    { "state": "not_yet_expected", "start": 1792674000000, "end": 1792674300000, "slots": 1 }
  ]
}
```

- The example is **illustrative and computed by hand**: the database of section 5.2 (100 consecutive candles up to `1792673100000`), a window of six slots of which the last three are after the newest candle, a delay of 15 000 ms and a lag threshold of 60 000 ms. `expected_at` of the three slots is `1792673715000`, `1792674015000` and `1792674315000`; `as_of_ms` `1792674030000` is past the first one's `expected_at + 60000`, inside the second's tolerance and before the third's `expected_at`.
- `start` and `end` echo the request. `as_of_ms` is the server clock read once for this request. `rule` is the configuration the answer was computed with. `series` is as in section 5.2.
- `coverage` has nine fields: eight integers (`slots` and seven `*_slots`) and the boolean `fully_present`. `slots` equals the sum of the seven `*_slots`. `fully_present` is true only when every slot is `present`, so a window containing a `due` slot is not fully present; unlike `gap_free` it takes the clock into account.
- `runs` is ascending and lists the **maximal runs of consecutive slots in one non-present state** (`end` exclusive, `slots` = length). `present` slots are never listed. A run changes at every change of state, so a window can have up to 10 000 runs (about 0.7 MB); runs are not paginated (decision S5).
- `stored_ahead_of_as_of` is true exactly when `series.newest_open_time_ms + timeframe + publication_delay_ms > as_of_ms`: the newest stored candle would not yet be eligible at this `as_of_ms` and this delay. It changes no count and diagnoses nothing; the cause may be a server clock behind the Collector's, or a publication delay configured larger than the one the Collector used. When it is true, every slot after the newest candle is `not_yet_expected`.

### 6.1.4 What a consumer can rely on, and what it cannot

- `due` and `stale` are judgments against the stated `rule` and the server's clock, not facts about the market. A consumer must read `as_of_ms` and `rule` with the counts.
- The API does not know the publication delay the Collector used. The delay is server configuration; a mismatch shows only as `stored_ahead_of_as_of` in one direction.
- `fully_present: true` means every slot has exactly one stored candle and no recorded disagreement, at that `as_of_ms`. It is not a statement about the future.
- Like every `/v1` response, the answer reflects one read transaction (section 5.6), and `Cache-Control: no-store` applies.

### 6.1.5 Errors and configuration

`400 invalid_parameter`, `404 unknown_series`, `503 not_ready`, `500 internal_error`, in the order of section 2. A server clock outside the valid time range (section 1) is `500`, never a count; the clock is checked after parameter validation and before the database is opened, so a bad clock together with an unreadable database answers `500`, not `503`.

The rule comes from the `[rule]` table of the configuration file (`config/quasar.toml`, section 6.4), which both the API and the Collector read: `rule.publication_delay_ms` (ADR-0001 P-3; `0` to `300 000`) and `rule.lag_threshold_ms` (`0` to `900 000`). The bounds are the same as before S20. A value outside the bounds, or not a non-negative integer, stops the process at start-up with exit status 2 and the process does not bind. (Before S20 the rule was set by the switches `--publication-delay-ms` and `--lag-threshold-ms`; they are removed.) The bounds exist because an unbounded threshold would keep every slot `due` and never `stale`.

## 6.2 Health: `GET /health`, `GET /health/ready`, `GET /health/ingest`

- **Status:** decisions H1–H5 (section 9) were approved by the user on 2026-10-28 together with the S17 plan; implemented by S17. These are unversioned operational endpoints outside `/v1` (a probe must not break on `/v2`). Section 1 applies to them as it does to `/v1` (`GET` only, `Cache-Control: no-store`, UTF-8 JSON, a trailing slash is a different path and `404 not_found`, no redirects), and so does the error body of section 2. Compatibility: new response fields may be added; clients must ignore unknown fields; there is no versioning promise beyond that.
- **Order of checks:** a query parameter (`400 invalid_parameter`; empty pieces such as `?` or `?&` carry no parameter and are ignored) → the host clock (`/health/ingest` only; outside the valid range is `500 internal_error`) → the database (`503 not_ready`).

### 6.2.1 `GET /health` (liveness)

`200 {"status":"alive"}`. It reads no database and no clock. It says only that the process answers; it stays `200` whatever the database state is.

### 6.2.2 `GET /health/ready` (readiness)

`200 {"status":"ready","schema_version":<schema version of this build>}` (2 since OI-2, ADR-0003 P-6) when the database opens read-only and its schema is the one this API was built for, checked inside one read transaction on every call. Otherwise `503 not_ready` with the reason and headers of section 2 (`database_missing`, `schema_mismatch`, `database_unreadable`). Readiness says nothing about freshness: an idle Collector does not make the API unready.

### 6.2.3 `GET /health/ingest` (ingestion freshness)

Information, never a gate: `200` whenever the database is readable, `503 not_ready` when it is not. A lagging Collector never changes an HTTP status.

```json
{
  "as_of_ms": 1792677000000,
  "rule": { "publication_delay_ms": 15000, "lag_threshold_ms": 60000 },
  "scope": "stored_series",
  "status": "current",
  "series_count": 10, "current": 10, "due": 0, "stale": 0,
  "behind": [
    { "venue": "binance-usdm", "symbol": "BTC/USDT:USDT", "timeframe": "5m",
      "newest_open_time_ms": 1792676400000, "lag_slots": 1, "state": "due", "configured": true }
  ],
  "truncated": false,
  "universe": {
    "configured_count": 10, "configured_stored": 9, "unconfigured_stored": 1,
    "missing_count": 1,
    "missing": [ { "venue": "binance-usdm", "symbol": "LTC/USDT:USDT", "timeframe": "5m" } ],
    "missing_truncated": false
  }
}
```

- **`scope` is always `"stored_series"`.** Every count and status describes the series that exist in the database (at least one row in `candles`), never the instruments that should exist. An instrument whose ingestion never started, or whose first write never succeeded, is invisible in these fields. A consumer that needs "every expected symbol is present" compares its own list with the listed counts, or asks `/v1/coverage` per symbol (`404 unknown_series` for a never-stored one). Since S20 the separate `universe` block lists configured instruments that have no stored series; `scope` and the counts are unchanged.
- **`status`** (closed list): `no_series` only when the read found zero stored series; `behind` when `stale > 0`; `catching_up` when `due > 0` and `stale == 0`; `current` otherwise. **`no_series` does not mean the Collector is healthy, idle, broken, not started or not initialised**: a freshly migrated database, a Collector that crashed before its first write and a database with only other tables give the same body. A consumer that needs "data exists" tests `series_count > 0`.
- **Per series** (the rule of S11, decision 1, unchanged; `x` is the instant the slot after the newest stored one is expected, `L` the lag threshold): `current` when the newest stored slot is the newest expected one or newer (a host clock behind the stored data is `current` with `lag_slots` 0); `due` when `x <= as_of <= x + L`; `stale` when `as_of > x + L`. `lag_slots` is the number of expected slots after the newest stored one: `1 + (as_of - x) // timeframe`. A `due` series has `1 <= lag_slots <= 1 + L // timeframe`, which is exactly `1` only when `L` is below one timeframe (the default `60 000` against `300 000`); a `stale` series has `lag_slots >= 1`. Read `state` and `lag_slots` together.
- **`behind`** lists the non-current series: `stale` before `due`, then `lag_slots` descending, then `(venue, symbol, timeframe)`; at most 50 entries, `truncated` is `true` when more exist (the counts are always complete). Since S20 each entry also carries `configured`, a boolean: whether that stored series is in the configuration file (section 6.4). A consumer can ignore a retired series (`configured: false`) without the endpoint hiding it; the series keeps its `state`, and keeps `status` at `behind` while it is stale.
- **`universe`** (added by S20; the information about the configured instruments, apart from everything above; an additive amendment, section 6.2 compatibility). The fields above, `scope` included, are computed exactly as before over **all stored series**, configured or not, with the same rules, order and limits; a request before and a request after S20 on the same database give the same values for them.
  - `configured_count`: the configured `(venue, symbol, timeframe)` triples in the file.
  - `configured_stored`: configured triples with at least one row in `candles` in the read transaction.
  - `unconfigured_stored`: stored series that are not in the file (retired or foreign).
  - Always `series_count == configured_stored + unconfigured_stored` and `configured_count == configured_stored + missing_count`. `series_count` counts every stored series; `configured_stored` only the configured ones; do not mix them up.
  - `missing`: configured triples with no row in `candles`, ordered by `(venue, symbol, timeframe)`, at most 50 entries (a constant of its own, equal to the limit of `behind` today); `missing_count` is complete; `missing_truncated` is `true` when more exist.
  - **What `missing` means and does not mean.** Zero rows for a configured series at the snapshot. It does not say why (never started, first write failed, a symbol the exchange does not serve, a Collector running on another file or database) and it does not judge time: a configured series with any row is never missing, however stale. It never changes `status`, any HTTP status or the order of checks of section 6.2. All configured instruments missing gives `status: no_series`, `series_count: 0` and `missing_count == configured_count`.
  - One read transaction serves both the fields above and `universe`. The API holds the universe in memory from the file it read at start; a change of the file takes effect after a restart.
- **What it cannot tell.** It reads only the newest stored candle per series. A series with holes behind its newest candle is `current` (`/v1/coverage` reports holes). A stopped Collector, an exchange outage, a halted or delisted symbol and a wrong publication delay all show as `stale`; the endpoint does not know the cause or whether the Collector runs. A retired symbol stays a stored series and keeps `status` at `behind`. A mismatch between the API's rule and the Collector's delay shifts `current`/`due` by the difference.
- **`open_interest`** (added by OI-5, ADR-0003 P-11; an additive amendment, section 6.2 compatibility): an object about the stored open interest series, or `null` when open interest is not enabled in the configuration. Specified in section 6.5.8. It is the **last key** of the body, after `universe`. Every field above keeps its meaning and its value; a request before and a request after OI-5 on the same database give the same values for them.
- The answer reflects one read transaction, like every other endpoint.
- A series that cannot be judged (a timeframe label this software does not know, or an open time not aligned to its timeframe; the Collector cannot write either, a hand-edited row can) makes the whole answer `500 internal_error`, never a made-up status.
- **A round longer than the lag threshold shows up as `stale`.** The Collector serves its symbols one after another through one rate limiter, so with a large universe the last symbols of a round can be written more than `lag_threshold_ms` after their slot was expected; those series are `stale`, and `status` is `behind`, for part of every cycle. This is not a fault of the Collector. Set `rule.lag_threshold_ms` in `config/quasar.toml` above the Collector's round duration for the universe, and alert on `behind` that persists for more than one cycle, not on a single answer. (The size of the effect depends on the Collector's request spacing and was not measured.)
- **`behind` is ordered by state, then by `lag_slots`**, which counts slots of each series' own timeframe: across timeframes it is not an ordering by elapsed time (v1 serves `5m` only).
- **Latency under a lock.** `/health/ready` and `/health/ingest` wait for a database lock up to SQLite's busy timeout (5 s) before answering `503 database_unreadable`; a probe whose own timeout is shorter sees a timeout instead of the `503`. `/health` does not touch the database and answers at once.

## 6.3 `GET /v1/indicators`

- **Status:** APPROVED by the user on 2026-10-28, decisions I1–I6 as proposed (section 9); implemented by S18. Design: `docs/superpowers/specs/2026-10-28-s18-indicators-design.md` (the section numbers in parentheses below point into it). **S19 adds `ema`, `macd`, `atr`, `adx` and `bbands`** (APPROVED by the user on 2026-10-28, decisions J1–J7 as proposed, section 9); design: `docs/superpowers/specs/2026-10-28-s19-indicators-design.md`. The request and response shape, the segment and warm-up rules and the reasons are the same for every indicator.
- **What it answers:** for one series and a window of at most 2016 slots, the value of each requested indicator at every slot, or the reason there is none, and for each indicator its `latest` point. It reads stored rows and the server clock only; it never computes across a slot that is not `present`.

### 6.3.1 Request

| Parameter | Required | Meaning |
|---|---|---|
| `venue` | yes | `binance-usdm` in v1. |
| `symbol` | yes | As in section 5.1. |
| `timeframe` | yes | `5m` in v1. |
| `start` | yes | Inclusive window start, epoch ms, aligned. |
| `end` | no | **Exclusive** window end, epoch ms, aligned, `> start`, within the valid range (as in section 5.1). Default: the end of the newest **expected** slot at the server clock (section 6.3.5). |
| `indicators` | yes | One to ten indicators, separated by `;`, each `name:key=value,key=value`: for example `rsi:period=14;rsi:period=7`. |

- **Window.** `end - start` is at most **2016 slots** (7 days of `5m`) for an explicit and for a defaulted `end` alike; longer is `400 invalid_parameter` with `details.maximum` `2016`. A defaulted `end` is never capped (section 6.3.5).
- **`indicators`.** It is one parameter, so section 1's rule against repeated parameters holds without an exception. Names and keys are lower case and case-sensitive. **Every parameter of an indicator is required**: there are no defaults. Integer values follow section 1's integer rule. An unknown name, an unknown, missing or repeated key, a value outside its range, a constraint between parameters of one indicator that is not met (`macd`: `fast < slow`), an empty item (a leading, trailing or doubled `;`), more than ten items, or the same normalized id twice is `400` with `details.parameter` `indicators`. The characters `:`, `=`, `,` and `;` are legal unencoded in a query string; their percent-encoded forms are accepted and mean the same.
- **Normalized id.** `name:key=value,…`, keys in the order the indicator defines, integers without leading zeros (`rsi:period=014` is `rsi:period=14`). The response identifies each indicator by its id and lists them in request order.
- **`rsi`.** Parameter `period`, an integer from `2` to `100`. Output `rsi`. `warmup_slots = 10 × period`.
- **`ema`.** Parameter `period`, `2` to `200`. Output `ema`. `warmup_slots = 6 × period`. TA-Lib `EMA` (SMA seed).
- **`macd`.** Parameters `fast` (`2` to `100`), `slow` (`3` to `200`), `signal` (`2` to `50`), with **`fast < slow`** (otherwise `400`; TA-Lib would silently swap them, the API refuses so one MACD has one id). Outputs, in this order: `macd`, `signal`, `histogram`. `warmup_slots = 6 × (slow + signal)`. TA-Lib `MACD` on EMAs (not `MACDEXT`); `histogram = macd − signal`. The values can be negative.
- **`atr`.** Parameter `period`, `2` to `100`. Output `atr`. `warmup_slots = 10 × period`. Uses high, low and close.
- **`adx`.** Parameter `period`, `2` to `100`. Outputs, in this order: `adx`, `plus_di`, `minus_di`. `warmup_slots = 13 × period`. Uses high, low and close.
- **`bbands`.** Parameters `period` (`2` to `200`) and `stddev` (`1` to `5`, an integer). Outputs, in this order: `upper`, `middle`, `lower`. `warmup_slots = period − 1`. The middle band is the simple moving average of `period` closes; `upper` and `lower` are the middle ± `stddev` × the **population** standard deviation of the same closes (one `stddev` for both bands). `lower` can be negative near a zero price.

### 6.3.2 Reasons

Every slot of `[start, end)` is a point. A point has either `values` (an object with one entry per output of the indicator) and `reason: null`, or `values: null` and a `reason`. The first matching row decides:

| Order | `reason` | When |
|---|---|---|
| 1 | `conflicted`, `unavailable`, `missing` | The slot has this state (section 4). |
| 2 | `not_yet_expected`, `due`, `stale` | The slot is after the newest stored candle, split by the server clock exactly as in section 6.1.2 with the same `rule`. Stored records win over the clock: a conflict or unavailable record after the newest candle is `conflicted` or `unavailable`. |
| 3 | `warmup` | The slot is `present`, but fewer than `warmup_slots` present slots precede it in its segment (section 6.3.3). |
| 4 | none | The point has a value. |

Rows 1, 3 and 4 depend on stored rows only; only row 2 depends on the clock.

### 6.3.3 Segments, warm-up and read range

- A **segment** is a maximal run of consecutive `present` slots. A slot that is `conflicted`, `unavailable`, `missing`, or after the newest stored candle ends a segment; so does the start of the stored history (slots before the oldest candle are `missing`). **No value is computed from a slot outside its segment.** After a gap the next present slot starts a new segment, and values return only after `warmup_slots` present slots of that segment.
- A point has a value only if at least `warmup_slots` present slots precede it **in its segment**.
- The **read range** of an indicator is `[max(start − warmup_slots × timeframe, 946684800000), end)`. Rows before it never influence the indicator; a computation starts at the first slot of its segment inside the read range (the response states the start as `anchor_open_time_ms`). The read cost is bounded: at most `2016 + 1500` slots of one series on the primary key, plus the conflict and unavailable records of the same range.
- **Each indicator is evaluated on its own read range.** Requesting another indicator in the same request never changes a value.

### 6.3.4 Response `200`

```json
{
  "venue": "binance-usdm",
  "symbol": "BTC/USDT:USDT",
  "timeframe": "5m",
  "start": 1792643400000,
  "end": 1792673400000,
  "as_of_ms": 1792673430000,
  "rule": { "publication_delay_ms": 15000, "lag_threshold_ms": 60000 },
  "series": { "oldest_open_time_ms": 1792500000000, "newest_open_time_ms": 1792673100000 },
  "indicators": [
    {
      "id": "rsi:period=14",
      "name": "rsi",
      "params": { "period": 14 },
      "outputs": ["rsi"],
      "warmup_slots": 140,
      "anchor_open_time_ms": 1792601400000,
      "latest": { "open_time_ms": 1792673100000, "values": { "rsi": "54.21378915036672" }, "reason": null },
      "points": [
        { "open_time_ms": 1792643400000, "values": { "rsi": "48.9930125837313" }, "reason": null },
        { "open_time_ms": 1792643700000, "values": null, "reason": "missing" }
      ]
    }
  ]
}
```

- **The example is an abbreviated illustration and computed by nobody**: its window has 100 slots but only two `points` are shown, and the values are not computed from real data. A real response lists **every slot** of `[start, end)`, in ascending order, with the same open times for every indicator.
- `start`, `end` echo the effective window. `as_of_ms` is the server clock read once for this request; `rule` and `series` are as in sections 6.1.3 and 5.2.
- `anchor_open_time_ms` is the first slot of the computation that produced the first value in the window, or `null` when no point of the window has a value.
- `params` are the normalized integers in the indicator's key order; `outputs` are the keys of `values`.

### 6.3.5 `latest`

`latest` repeats the **last point of the requested window** (`end` minus one timeframe). **It is not the current market value.** It is the newest expected slot only when the window ends there, which the default `end` arranges:

- **Default `end`:** the end of the newest slot with `expected_at <= as_of_ms` (section 6.1.2). If that is not after `start`, the window is the one slot at `start`. If the window from `start` to that end exceeds 2016 slots, the answer is `400` (`details.parameter` `start`, `details.maximum` `2016`), never a shortened window: a shortened default would silently turn `latest` into an old point.
- **Explicit `end`:** `latest` is whatever slot precedes `end`. In the past it is an ordinary historical point: a present, warmed-up slot has its value, a gap is `missing`; `stale` applies only to slots after the newest stored candle. In the future it is `not_yet_expected`.

A series that stopped therefore answers `latest.reason` `due` or `stale`, never the last value it has. A consumer that wants the current value omits `end` and compares `latest.open_time_ms` with `as_of_ms`.

### 6.3.6 Numbers and reproducibility

- Values are **float64 results** (TA-Lib), not exact decimals: a JSON **string** holding the shortest decimal that parses back to the same float64, in positional notation (no exponent, no leading `+`, no trailing fractional zero, `-0` written `0`, a leading `-` possible for indicators that go negative). Inputs are the stored decimal closes, and for `atr` and `adx` also the highs and lows, **of `present` slots only** (a conflicted slot's stored high or low reaches no computation), converted to float64 for the computation only. The API does not re-validate stored candles. A result that is NaN or infinite at a point that passed warm-up is `500 internal_error`, never a value.
- **Reproducible only for the same build.** For one indicator the `values` and the reasons of rows 1, 3 and 4 are byte-identical when the effective window, the indicator id, the stored rows of its read range and the build (TA-Lib, numpy, the TA-Lib C library, the platform) are the same. The whole body is not: `as_of_ms` and the reasons of row 2 depend on the clock. Another build may differ in the last digits.
- **Warm-up is an approximation, not the whole-history value.** The smoothed indicators (`rsi`, `ema`, `macd`, `atr`, `adx`) keep the influence of their starting state; starting at the anchor instead of at the oldest stored candle is an accepted approximation. `warmup_slots` is chosen so that the remaining weight of the initial averages (the seeds), counting every path through which they reach the output, is at most about `e^-9`. **That is a weight, not an accuracy bound:** no difference in output units is promised. **A value is not promised to equal the value over the whole stored history, and two requests with different `start` may give slightly different values for the same slot.** Observed on generated data (not guaranteed bounds): on ordinary data and after a sudden trend reversal, about `0.001` points for RSI and ADX; after a 1000× volatility collapse, up to `0.5` RSI points, `0.13` ADX points and `3.6 %` of ATR; after a flat run that follows a move, see the next point.
- `bbands` depend only on the last `period` closes and are exact in principle, but TA-Lib keeps running sums, so the last digits of a value can depend on where its segment starts.
- A flat market (constant closes) gives the value TA-Lib defines for it; the contract adds no interpretation. For RSI, ADX and both DI this means: if the whole computation range from the anchor is flat they read `0`, but if the market turns flat after moving they stay at a frozen value, so one slot can read `0` or a frozen value depending on `start` (follow-up F-20; whatever is decided for F-20 covers ADX and DI too).

### 6.3.7 Errors

`400 invalid_parameter`, `404 unknown_series`, `503 not_ready`, `500 internal_error`, in the order of section 2 with these additions: parameters (every `indicators` error included) → the host clock (outside the valid range is `500`, as in section 6.1.5, checked before the database is opened) → the window of a defaulted `end` (`400`) → the database (`503`) → the series (`404`). A data-quality situation (gaps, conflicts, staleness, warm-up) is never an error: it is `200` with reasons. A window entirely before the oldest stored candle is `200` with every point `missing`.

## 6.4 `GET /v1/instruments`

- **Status:** PROPOSED for S20, needs the user's approval (decisions U1 to U7, section 9). Design: `docs/superpowers/specs/2026-10-28-s20-universe-config-design.md` (5.1, 5.4).
- **What it answers:** the configured universe, and for each configured instrument the open time of its newest stored candle. It reads stored rows only; it never contacts an exchange and never writes.
- **The universe** is defined by the configuration file `config/quasar.toml`, read by the Collector and the API (one file, one place). The API reads it once at start and holds it in memory; a change of the file takes effect after a restart of the process.

### 6.4.1 Request

No parameter. Any query parameter, known or not, is `400 invalid_parameter` (section 1, Parameters; as for the health paths, section 6.2). Order of checks: the query (`400`) → the database (`503 not_ready`, section 2). There is no `404` and no series lookup.

### 6.4.2 Response `200`

```json
{
  "instruments": [
    { "venue": "binance-usdm", "symbol": "BTC/USDT:USDT", "timeframe": "5m", "newest_open_time_ms": 1792673100000 },
    { "venue": "binance-usdm", "symbol": "LTC/USDT:USDT", "timeframe": "5m", "newest_open_time_ms": null }
  ]
}
```

- One entry per configured `(venue, symbol, timeframe)`, **ordered by that triple** (so the answer is deterministic whatever the order in the file).
- `newest_open_time_ms` is the open time of the newest stored candle of that series, or `null` when nothing is stored (not yet ingested, or the first write never succeeded). `null` is not an error and says nothing about why.
- **Only configured instruments are listed.** A stored series that is not in the file (retired or foreign) is not listed; its candles stay stored and `/v1/candles`, `/v1/coverage` and `/v1/indicators` keep serving them (INV-067).
- **Unpaginated:** the size is bounded by the file (target 300 to 600 entries), not by data.
- Section 1 conventions apply (`GET` only, `Cache-Control: no-store`, UTF-8 JSON). All reads run in one read transaction (section 5.6) and take the newest candle per configured series by primary key, never a scan. A database that cannot be read is `503 not_ready` with the reason and headers of section 2, never an empty list.

## 6.5 `GET /v1/open-interest` and the `open_interest` object of `GET /health/ingest`

- **Status:** APPROVED by the user on 2026-11-03, decisions OA1 to OA11 as proposed (section 9). Implemented by OI-5 (issue #5, ADR-0003 P-7, P-8, P-10, P-11). Plan: `docs/superpowers/plans/2026-11-03-oi-5-open-interest-api.md`.
- **What it answers:** for one series `(venue, symbol, period)` and a window of at most 10 000 slots, the open interest rows Binance reported and the Collector stored, and for every slot of the page the state of ADR-0003 P-7. It reads stored rows, the server clock and the configuration file only; it never contacts an exchange and never writes.
- **Sections 1 to 4 and 5.6 apply** (conventions, error body, pagination by time, one read transaction, work off the event loop), with the differences stated below.

### 6.5.1 What a row is

- A slot is one `time_ms`, the **end** of a 5m period, aligned to 5 minutes. The row with `time_ms = T` belongs to the period `[T - 5m, T)`, that is to the candle with `open_time_ms = T - 5m`. **Never join a row to a candle by equal timestamps.** This is why the field is called `time_ms` and the response states `"time_semantics": "period_end"`.
- `open_interest` and `open_interest_value` are decimal strings in the canonical form of section 1, exactly as stored (Binance's own figures; `open_interest_value` is never computed here). Binance does not document the units; the project inferred them from one sample (ADR-0003 P-2, E-3): `open_interest` in the base asset (BTC for `BTC/USDT:USDT`), `open_interest_value` in USDT. Whether a value is the open interest at the instant `T` or a statistic over the period is not documented, and this API claims only "the value Binance reports for the period ending at `T`".
- Identity is `(venue, symbol, period, time_ms)`. The response carries no `ingested_at_ms` (diagnostic, as for candles, decision Q5).

### 6.5.2 Request

| Parameter | Required | Meaning |
|---|---|---|
| `venue` | yes | `binance-usdm` in v1. |
| `symbol` | yes | CCXT unified symbol, exactly as stored, e.g. `BTC/USDT:USDT`. |
| `period` | yes | `5m` in v1. |
| `start` | yes | Inclusive window start, epoch ms, aligned to the period. It selects rows by `time_ms` (the period **end**): to read the open interest that belongs to the candle opening at `O`, ask for `start = O + 5m`. |
| `end` | no | **Exclusive** window end, epoch ms, aligned, `> start`, at most one period past the maximum valid time (as in section 5.1). Default: see below. |
| `limit` | no | Maximum rows in this response, `1` to `1000`, default `1000` (fixed, as in section 5.1, decision R6). |

A row is selected when `start <= time_ms < end`. The window never exceeds **10 000 slots**; the effective window, the defaulting of `end` (`newest + period` capped at `start + 10 000 x period`, or one slot when `start` is after the newest stored row), `next` and the paging rules are those of sections 3 and 5.1 with `time_ms` in the place of `open_time_ms` and the newest stored `time_ms` of the series in the place of the newest candle. With a defaulted `end` and `start` at or before the newest stored `time_ms`, the span stops at the newest stored row: no slot after it is in the page, so a series that stopped long ago still answers `gap_free: true` (an absent slot *before* the newest row can still be `not_yet_expected`, rule 3 of section 6.5.3). With `start` after the newest row the single slot `[start, start + period)` is judged like any slot after it (`not_yet_expected`, `due`, `stale` or `no_longer_recoverable`). **A defaulted window therefore does not show the age of a stopped series.** Freshness is judged by `GET /health/ingest` (section 6.5.8); to see the states after the newest row, pass an explicit `end` (decision OA10). Parameter rules of section 1 (unknown, repeated, empty values) apply; `timeframe` is not a parameter of this endpoint.

Example:

```text
GET /v1/open-interest?venue=binance-usdm&symbol=BTC/USDT:USDT&period=5m&start=1792672500000&end=1792674600000
```

### 6.5.3 Slot states

For a slot `T` at `as_of_ms`, the **first** matching state wins (ADR-0003 P-7). `rule` is the one stated in the response (section 6.5.4); `H` is `rule.retrieval_horizon_ms`, 29 days.

| Order | State | Condition |
|---|---|---|
| 1 | `conflicted` | One or more `open_interest_conflicts` records for `T`. A stored row, if any, is returned with its **first stored** values and `conflicted: true`. |
| 2 | `present` | A stored row for `T`. |
| 3 | `not_yet_expected` | `T + rule.expected_after_ms > as_of_ms`: by the Collector's schedule `T` is not yet expected to be published. It does not say Binance has not published it; nobody looked. |
| 4 | `no_longer_recoverable` | `T < as_of_ms - H` and no row: older than the retrieval horizon, so the Collector will never request it. It does not say the data existed, or that Binance would refuse it. |
| 5 | `due` | No row, `T` is after the newest stored `time_ms` of the series, and `as_of_ms <= T + expected_after_ms + lag_threshold_ms`: expected, tolerated as lag. |
| 6 | `stale` | No row, `T` is after the newest stored `time_ms`, and `as_of_ms > T + expected_after_ms + lag_threshold_ms`. |
| 7 | `missing` | None of the above: no row, and `T` is before the newest stored `time_ms` (including slots before the oldest row that are still inside the horizon). The reason is unknown: Binance documents that periods without trades are omitted, and a row can also be late or lost; a response cannot tell these apart. |

Hence `slots = present + conflicted + not_yet_expected + no_longer_recoverable + due + stale + missing`. A slot becomes `no_longer_recoverable` only by the passage of time and is never reported `present` by inference. A stored row is `present` whatever the clock says (stored facts win over the clock).

Two consequences of the order, both different from candle coverage (section 6.1.2), where only slots after the newest stored candle are refined by the clock:

- `due` and `stale` exist only after the **newest stored** `time_ms`: they describe a series that has stopped (or is late as a whole). A single absent row with newer rows stored is never `due` or `stale`: it is `not_yet_expected` while `T + expected_after_ms > as_of_ms`, then `missing` (or `no_longer_recoverable` after `H`).
- A slot before the newest row and within `expected_after_ms` of `as_of_ms` is `not_yet_expected`, not `missing`: with `c > 1` rows reach the database in groups, so an older absent slot may still arrive with the symbol's next request.

### 6.5.4 Response `200`

```json
{
  "venue": "binance-usdm",
  "symbol": "BTC/USDT:USDT",
  "period": "5m",
  "time_semantics": "period_end",
  "start": 1792672500000,
  "end": 1792674600000,
  "as_of_ms": 1792674530000,
  "rule": {
    "cadence": 1,
    "expected_after_ms": 500000,
    "lag_threshold_ms": 60000,
    "retrieval_horizon_ms": 2505600000
  },
  "series": {
    "oldest_time_ms": 1792643700000,
    "newest_time_ms": 1792673400000
  },
  "rows": [
    { "time_ms": 1792672500000, "open_interest": "95677.779", "open_interest_value": "7446123150.675", "conflicted": false },
    { "time_ms": 1792672800000, "open_interest": "95690.12", "open_interest_value": "7447031002.4", "conflicted": true },
    { "time_ms": 1792673400000, "open_interest": "95702.5", "open_interest_value": "7447990111.25", "conflicted": false }
  ],
  "next": null,
  "coverage": {
    "span": { "start": 1792672500000, "end": 1792674600000 },
    "slots": 7,
    "present_slots": 2,
    "conflicted_slots": 1,
    "not_yet_expected_slots": 1,
    "no_longer_recoverable_slots": 0,
    "due_slots": 1,
    "stale_slots": 1,
    "missing_slots": 1,
    "gap_free": false
  }
}
```

- The example is **illustrative and computed by hand**: seven slots `1792672500000` to `1792674300000`; rows stored for the 1st, 2nd (conflicted) and 4th slots; the newest stored `time_ms` is the 4th; `as_of_ms` is the 6th slot plus `expected_after_ms` plus 30 000 ms, so the 5th slot is `stale`, the 6th `due` and the 7th `not_yet_expected`; the 3rd slot has no row and lies before the newest, so it is `missing`. The values are not real.
- `start` and `end` echo the effective window. `as_of_ms` is the server clock read once for this request. `series` is present on every `200`: the `time_ms` of the oldest and newest stored row of the series (a series that exists has at least one row, and rows are never deleted).
- `rule` is what the states were computed with: `cadence` is `c` of ADR-0003 P-10 (each symbol is asked every `c` periods); `expected_after_ms = c x 300 000 + round_offset_ms`; `lag_threshold_ms` is `rule.lag_threshold_ms` of the configuration file (the value `/v1/coverage` uses, decision OA3); `retrieval_horizon_ms` is `H`, fixed at 29 days in v1.
- `rows` are ascending, unique by `time_ms`, each with exactly the fields shown; `conflicted` is always present. `next` is always present and defined as in section 3.

### 6.5.5 Errors and order of checks

`400 invalid_parameter`, `404 open_interest_disabled`, `404 unknown_series`, `503 not_ready`, `500 internal_error`. Order: parameters (`400`) -> the configuration has an enabled `[open_interest]` table (`404 open_interest_disabled`, needs no database) -> the host clock (outside the valid range is `500`, as in section 6.1.5) -> the database (`503`) -> the series (`404 unknown_series` with the message "no open interest row of this series has been stored" and `details` `venue`, `symbol`, `period`, when no open interest row of the series has ever been stored: a typo, a symbol not yet collected, and a database where collection has not started look the same). An existing series with nothing in the window is a `200` with an empty `rows` list. A data-quality situation (gaps, conflicts, staleness) is never an error. The one exception is a stored value this software cannot judge: an unaligned `time_ms` of a stored row or of a conflict record in the page (the Collector cannot write one; a hand-edited database can) makes the answer `500 internal_error`, never a wrong count, as in section 6.2.3. `GET /health/ingest` counts such a series instead of failing (`unjudged_series`, section 6.5.8, decision OA9).

### 6.5.6 The `coverage` block

`coverage` speaks for **this page only**: the span `[span.start, span.end)`, where `span.start = start` and `span.end` is `next` when it is not `null`, else `end` (section 5.3). The counts are those of the states of section 6.5.3 at `as_of_ms`; `slots` and the seven `*_slots` (see the example) always satisfy `slots = present + conflicted + not_yet_expected + no_longer_recoverable + due + stale + missing`. `gap_free` is `true` only when every slot of the span is `present` (all other counts are `0`). It is the same condition as `fully_present` of `/v1/coverage` (section 6.1.3); the name `gap_free` is kept because the issue and ADR-0003 P-11 call it that and because this response is page-scoped like section 5.3. Unlike section 5.3 the other counts include the clock-dependent states, but see section 6.5.2: with a defaulted `end` the span never reaches them, and `gap_free` says nothing about the age of the data. Returned rows with `conflicted: true` are exactly the conflicted slots that have a stored row. The response lists no individual gaps; open interest has no separate coverage endpoint in v1.

### 6.5.7 Where `rule` comes from, and what a consumer can rely on

- The API reads the same configuration file as the Collector (section 6.1.5, S20). `cadence` is `open_interest_cadence(S, budget_per_5min)` with `S` the number of symbols of the configured venue and `budget_per_5min` from `[open_interest]`; `expected_after_ms = cadence x 300 000 + round_offset_ms`. The same pure function and the same file give the Collector and the API the same `c` (ADR-0003 P-10, P-15). If the two processes read different files the states shift; compare the `sha256` printed by both at start. A change of the file takes effect after a restart of each process.
- The Collector judges the retrieval horizon and the expectation against the Binance **server lower bound** (ADR-0001 P-4); the API uses the **host clock**. The edges of `no_longer_recoverable` and `not_yet_expected` can differ from the Collector's by the clock skew between the two.
- `due`, `stale` and `not_yet_expected` are judgments against the stated `rule` and the server clock, not facts about Binance or the market. Read `as_of_ms` and `rule` with the counts.
- A row is never fabricated, interpolated or carried forward. The absence of a row is never evidence that open interest was zero or that nothing traded; Binance omits periods without trades (ADR-0003 E-1).
- With `c > 1` (more symbols than the budget allows asking every period) a row can legitimately reach the database up to `c x 5 min + round_offset_ms` after its period end; this is the reason for `expected_after_ms`, and the latency is not guaranteed (ADR-0003 P-10).
- A `conflicted: true` row is **untrusted**: the first stored value may be the wrong one (section 0, point 5). The rejected value is not exposed.
- `present` means "we stored a value Binance reported for `T`", not "it will never change".
- `Cache-Control: no-store` applies.

### 6.5.8 The `open_interest` object of `GET /health/ingest`

```json
"open_interest": {
  "as_of_ms": 1792677530000,
  "rule": { "cadence": 1, "expected_after_ms": 500000, "lag_threshold_ms": 60000 },
  "scope": "stored_series",
  "status": "degraded",
  "series_count": 10, "current": 9, "due": 1, "stale": 0, "unjudged_series": 0,
  "conflicted_series": 1,
  "behind": [
    { "venue": "binance-usdm", "symbol": "ETH/USDT:USDT", "period": "5m",
      "newest_time_ms": 1792676700000, "lag_slots": 1, "state": "due", "configured": true }
  ],
  "truncated": false
}
```

- The example is **illustrative and computed by hand**: ten series; nine hold the row for the newest expected slot `1792677000000` (`current`); the tenth, `ETH/USDT:USDT`, ends at `1792676700000`, so `x = 1792676700000 + 300000 + 500000 = 1792677500000` is 30 000 ms before `as_of_ms` (`due`, `lag_slots` 1); one series has a conflict in a slot of the last 24 hours, hence `degraded`.
- **`null`** (the key is present) when the configuration has no enabled `[open_interest]` table. Otherwise an object, judged in the **same read transaction** and at the same `as_of_ms` (repeated inside the object) as the candle fields.
- **A series** is a `(venue, symbol, period)` with at least one row in `open_interest`; `scope` is always `"stored_series"`, like the candle part: a configured symbol whose first row never arrived is invisible here, and `no_series` does not mean the open interest task is healthy, idle, broken or not started.
- **A series that cannot be judged** (decision OA9): its `period` is not the configured open interest period, or its newest `time_ms` is not aligned to the period (the Collector cannot write either; a hand-edited row can). It is counted in `series_count` and in `unjudged_series`, in none of `current`, `due`, `stale`, and not listed in `behind`; the answer is never a `500`, so open interest cannot change the candle part of the answer. `series_count = current + due + stale + unjudged_series`.
- **Per series** (the rule of section 6.2.3 with the open interest expectation): `N` is the newest stored `time_ms`; the newest expected slot is `T_e = floor((as_of_ms - expected_after_ms) / period) x period`. `current` when `N >= T_e`; otherwise `x = N + period + expected_after_ms`, `due` when `as_of_ms <= x + lag_threshold_ms`, `stale` after; `lag_slots = (T_e - N) / period` (>= 1 when not current).
- **`status`** (closed list), the first that matches: `no_series` when `series_count` is `0`; `degraded` when `conflicted_series > 0` or `unjudged_series > 0`; `behind` when `stale > 0`; `catching_up` when `due > 0`; `current`. `degraded` takes precedence over `behind` and `catching_up` (ADR-0003 P-8): a consumer that needs freshness reads the counts, which are always complete.
- **`conflicted_series`:** the number of series with at least one `open_interest_conflicts` record whose slot satisfies `as_of_ms - 86 400 000 < time_ms`. A conflict recorded now for an older slot does not count here; it stays visible per slot in `GET /v1/open-interest`. It makes open interest health stricter than candle health on purpose, which does not count conflicts (ADR-0003 O-4).
- **`behind`:** the non-current series, `stale` before `due`, then `lag_slots` descending, then `(venue, symbol, period)`; at most 50 (the same constant as the candle `behind`); `truncated` is `true` when more exist (the counts are complete). `configured` is whether `(venue, symbol)` is in the configuration file with the configured open interest period. A retired series stays stored, keeps going `stale` and keeps `status` at `behind`.
- **What it cannot tell.** Only the newest stored row per series is read for freshness: a series with holes behind its newest row is `current` (`GET /v1/open-interest` shows the states). A stopped open interest task (it stops alone on a fatal error, ADR-0003 P-9), a `418`, a `429` pause and a Collector that is down all show as `due`, then `stale`; the endpoint does not know the cause. Candle health and open interest health are independent: a stale open interest never changes a candle field or any HTTP status.

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
| `/v1/coverage` states `not_yet_expected`, `due` and `stale` from `as_of` and the rule; `due` is a third clock state | S11 plan decisions 1–3 (approved 2026-10-25), INV-045 |
| The response states `as_of_ms` and the rule; the configured lag threshold is bounded | follow-up F-8 |
| Health endpoints: liveness reads nothing; readiness is the schema check; ingestion freshness is information over stored series only, `200` unless unreadable | S17 plan decisions 1 to 6 and 10, INV-056, INV-057 |
| `due` bounds `lag_slots` by `1 + L // timeframe` | S17 plan decision 5, `tests/domain/test_freshness.py` |
| `/v1/indicators`: per-point values or reasons, segments that never cross a non-present slot, `warmup_slots`, `latest` is the window's last point, float64 values reproducible only for the same build | S18 design (`docs/superpowers/specs/2026-10-28-s18-indicators-design.md`), decisions I1–I6 |
| S19 indicators: `ema`, `macd`, `atr`, `adx`, `bbands`, the `macd` parameter constraint, high/low inputs, the `2016 + 1500` read bound | S19 design (`docs/superpowers/specs/2026-10-28-s19-indicators-design.md`), decisions J1–J7 |
| `/v1/instruments`: the configured universe from the versioned file, ordered by triple, `newest_open_time_ms` or `null`, configured instruments only | S20 design (`docs/superpowers/specs/2026-10-28-s20-universe-config-design.md`), decisions U4, U6, INV-066, INV-067 |
| `/v1/open-interest`: `time_ms` is the period end, seven slot states with the first match winning, `not_yet_expected` after `c x 5 min + round_offset`, `no_longer_recoverable` after 29 days, `due`/`stale` at the newest edge, `conflicted` rows returned with their first stored values | ADR-0003 P-3, P-5, P-7, P-8, P-10, P-11; decisions OA1 to OA11 |
| `/health/ingest` `open_interest` object: additive, `null` when not enabled, per-series freshness with the open interest expectation, `conflicted_series` makes the status `degraded` | ADR-0003 P-8, P-11, O-4; decisions OA5, OA6 |
| `/health/ingest` `universe` block and `configured` on `behind` entries; the S17 fields keep their meaning; the two equalities | S20 design, decisions U5, U6, INV-066, INV-067 |

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
| Q8 | Where is health (architecture §8 `GET /health`)? | Decide in S17; this draft only reserves the readiness vocabulary. (Answered in S17: section 6.2 and decisions H1 to H5 below.) | Fix it now: no information yet to justify it. |

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

S12 decisions (answered by the user on 2026-10-25: all as proposed):

| # | Question | Proposal | Alternative that is not proposed, and its cost |
|---|---|---|---|
| S1 | Where does `as_of` come from? | The server clock, stated in the response; no `as_of` parameter. | An optional `as_of` for replaying history: reproducible answers, but a caller can ask about a "now" that is not now. It can be added later without breaking anything. |
| S2 | Are `start` and `end` both required? | Yes; the section 5.1 rules, no defaulting. | A defaulted `end`: copies the two-branch rule of section 5.1 for little gain. |
| S3 | How is the rule configured and bounded? | Server start-up options with the defaults and bounds of section 6.1.5. The bounds (300 000 ms and 900 000 ms) are the builder's numbers. | No bound: a huge lag keeps every slot `due` forever (follow-up F-8). |
| S4 | Is `stored_ahead_of_as_of` exposed? | Yes, unchanged from S11. | Hide it: a clock or delay mismatch becomes invisible. |
| S5 | Are `runs` paginated? | No; at most 10 000 runs, bounded by the window cap. | A second cursor kind for a window with a different fault in every slot. |

S17 decisions (approved by the user on 2026-10-28 with the S17 plan):

| # | Question | Decision | Alternative that was not chosen, and its cost |
|---|---|---|---|
| H1 | Where is health (Q8)? | `GET /health` (liveness, the path of architecture §8), `GET /health/ready`, `GET /health/ingest`, unversioned, outside `/v1`. | `/health/live` plus an architecture edit: `/health` would be a `404` for probes that assume it. |
| H2 | Does freshness gate readiness? | No: `/health/ingest` is information and `200` while the database is readable. | Make a stale Collector `503`: a probe could restart a healthy API because of the Collector. |
| H3 | Does a query parameter on a health path fail? | `400 invalid_parameter`, as everywhere (section 1); an empty query (`?`, `?&`) names no parameter and is accepted. | Ignore it: probe-friendly, but a second rule next to section 1. A `400` from a liveness probe can trigger a restart. |
| H4 | How many non-current series are listed? | 50 (`truncated` says more exist); counts are complete. | Unbounded: the response grows with the universe. |
| H5 | What does `/health/ingest` cover? | Stored series only; `no_series` means zero stored series and nothing else. | Compare with the configured universe: needs a Collector or configuration channel (S20, follow-up F-18). |

S18 decisions (approved by the user on 2026-10-28: all as proposed):

| # | Question | Proposal | Alternative that is not proposed, and its cost |
|---|---|---|---|
| I1 | How are several indicators requested? | One `indicators` parameter, items separated by `;` (`rsi:period=14;rsi:period=7`), so section 1's rule against repeated parameters has no exception. | A repeated `indicator=` parameter: reads better, but needs an exception to the rule that protects against typos and duplicates. |
| I2 | Which bounds? | RSI `period` 2 to 100; at most 10 indicators per request; every parameter required, no defaults (architecture §8, explicit parameters). | Defaults (`period=14`): shorter URLs, but a silent default that has to be kept forever. |
| I3 | How long is the warm-up? | `warmup_slots = 10 × period` for RSI. An approximation of the whole-history value, stated as such (section 6.3.6). | Compute from the start of each segment: exact, but the read grows with the stored history of every request. |
| I4 | How big is the window? | At most 2016 slots (7 days), also for a defaulted `end`, which is never capped. | A capped default: always answers, but `latest` could silently be an old point. |
| I5 | How are values written? | float64 results as the shortest round-trip decimal string, positional; declared not exact; reproducible for the same build only. | JSON numbers: shorter, but parsers differ on precision and exponents. Exact decimals: not what TA-Lib computes. |
| I6 | What is `latest`? | The last point of the requested window, never the current market value; a stopped series answers `due` or `stale`. | The newest stored value: always has a number, but would present stale data as current. |

S19 decisions (approved by the user on 2026-10-28: all as proposed):

| # | Question | Proposal | Alternative that is not proposed, and its cost |
|---|---|---|---|
| J1 | Names and outputs? | `ema`, `macd` (`macd`, `signal`, `histogram`), `atr`, `adx` (`adx`, `plus_di`, `minus_di`), `bbands` (`upper`, `middle`, `lower`). | Long names (`bollinger`): readable, but diverge from TA-Lib names that consumers search for. |
| J2 | Ranges? | Section 6.3.1; `macd` `fast < slow` or `400`. | Accept `slow < fast` and let TA-Lib swap: two ids for one indicator. |
| J3 | Warm-up? | One criterion (total seed weight over all paths `≤ e^−9`, RSI's), per-indicator integer formulas (ADX `13 × period`), verified for every allowed value; read bound `2016 + 1500`. | One factor `10 × longest period` for all: simpler to state, but EMA(200) and MACD would read 2000–2500 extra slots for no gain; or keep the bound at 1000 by shrinking ranges: no EMA(200). |
| J4 | Bollinger multiplier? | Integer `stddev` 1..5, both bands; population deviation, SMA middle. | Decimal parameter (`2.5`): needs a decimal grammar and normalization (`2.50` vs `2.5`) in the contract; can be added later without breaking. |
| J5 | ADX outputs? | `adx` with `plus_di` and `minus_di`. | ADX only: smaller body, but Core cannot tell trend direction without a second indicator name. |
| J6 | Flat markets (ADX/DI 0/0)? | Characterize TA-Lib's value, extend F-20; the F-20 decision applies to all indicators. | Decide F-20 now inside S19: widens S19 into a contract change of S18's reasons. |
| J7 | Event-loop cost of a bigger body? | Build and encode the `/v1/indicators` body in the thread pool. | Leave it (F-21): a 5 MB body built on the loop stalls `/health` for roughly 100 ms (estimate, unmeasured). |

S20 decisions (PROPOSED, awaiting the user's approval at HARD STOP 1; design `docs/superpowers/specs/2026-10-28-s20-universe-config-design.md`, section 6 step 1):

| # | Question | Proposal | Alternative that is not proposed, and its cost |
|---|---|---|---|
| U1 | Where is the universe and every operational value defined? | One TOML file, `config/quasar.toml`, committed and versioned, read by both the Collector and the API; every key required, unknown keys an error. | Switches at start (today): nothing records which universe ran, and the API cannot know which instruments should exist (F-18). A channel from the Collector to the API: breaks independence. |
| U2 | Is the file reloaded while a process runs? | No: a change takes effect after a restart. | Hot reload: more states to test and two processes that can disagree mid-run. |
| U3 | What does a relative path in the file mean? | A relative `database.path` resolves against the directory of the configuration file, never against the working directory; an absolute path is used as it is. | Resolve against the working directory: the same file means a different database depending on where the process was started. |
| U4 | Shape and order of `GET /v1/instruments`? | Section 6.4: no parameter, `{"instruments":[{"venue","symbol","timeframe","newest_open_time_ms"}]}`, one entry per configured triple, ordered by `(venue, symbol, timeframe)`, `null` when nothing is stored, configured instruments only, unpaginated. | Also list stored but unconfigured series: hides which instruments are intended. Paginate: complexity for a list bounded by the file. |
| U5 | How does `/health/ingest` show configured instruments with no stored series? | A separate `universe` block (section 6.2.3) with `configured_count`, `configured_stored`, `unconfigured_stored`, `missing_count`, `missing` (at most 50), `missing_truncated`, and the two equalities; `missing` never changes `status` or any HTTP status. | Fold missing instruments into `series_count`, `status` or `behind`: rewrites the meaning of the S17 fields. |
| U6 | What happens to the S17 fields and to a series removed from the file? | The S17 fields (`scope`, `status`, `series_count`, `current`, `due`, `stale`, `behind`, `truncated`) keep their meaning and are computed over all stored series; a retired series stays in them, appears in `behind` while stale with `configured: false`, and is counted in `unconfigured_stored`. Nothing is excluded or deleted. | Exclude retired series from the statistics: rewrites the S17 fields; it would need its own contract change and approval. |
| U7 | Which values does version 1 accept? | Exactly one venue, `binance-usdm`, and `timeframes == ["5m"]`; anything else is refused at start as not supported yet (S22), so the file shape does not change when a venue is added. | Accept any venue or timeframe now: untested combinations. |

The file format, the audit of the runtime settings (design 5.7: which values are in the file and which stay in code) and the removal of the switches (`--symbol`, `--limit`, `--publication-delay-ms`, `--max-concurrency`, `--min-request-interval-ms`, `--backfill-days`, `--no-backfill`, `--db` for the Collector; `--publication-delay-ms`, `--lag-threshold-ms`, `--host`, `--port`, `--db` for the API; `fetch-once` keeps its switches) are operational, and are part of the same approval so that nothing is decided twice.

OI-5 decisions (APPROVED by the user on 2026-11-03, all as proposed; section 6.5):

| # | Question | Proposal | Alternative that is not proposed, and its cost |
|---|---|---|---|
| OA1 | What does `GET /v1/open-interest` answer when the configuration has no enabled `[open_interest]` table? | `404` with the new closed-list code `open_interest_disabled`, checked after the parameters and before the database. The API cannot judge slots without `cadence` and `round_offset_ms`, so it refuses instead of guessing. | Serve stored rows with a guessed rule: could label slots `stale` that the Collector never intended to collect. The plain `not_found`: indistinguishable from an older API build that lacks the endpoint. **Cost of the proposal:** with `enabled = false` (for example, pausing collection) the already stored history is unreadable through this endpoint and health shows `null`, although the rows are in the database; the candle endpoints are unaffected. |
| OA2 | Which key selects rows, and what do `start`/`end` mean? | The slot is `time_ms`, the period **end**; `start <= time_ms < end`, aligned to 5 minutes, window and paging as for candles (10 000 slots, `limit` 1000, `next`). The candle for open time `O` joins at `time_ms = O + 5m`; the contract says so in three places. | Select by the period start `time_ms - 5m`: matches candle windows, but invents a second timestamp that disagrees with the stored column and with Binance's field (ADR-0003 P-3). |
| OA3 | Which lag threshold splits `due` from `stale` for open interest? | The existing `rule.lag_threshold_ms` of the file (60 000 by default), echoed in `rule`. No new configuration key. With a large universe (`c > 1`) it is the operator's job to set it above the spread of a round, as for candles. | A new key `open_interest.lag_threshold_ms`: more precise, but changes the approved `[open_interest]` table (ADR-0003 P-13, every key required) and needs its own approval. |
| OA4 | What is in `rule`, and is the 29-day horizon exposed? | `cadence`, `expected_after_ms`, `lag_threshold_ms`, `retrieval_horizon_ms`: everything the states were computed with, so a consumer can recompute them. | Only `expected_after_ms`: shorter, but `no_longer_recoverable` would depend on a constant the response does not state. |
| OA5 | `/health/ingest` when open interest is not enabled | `"open_interest": null`, key always present. | Omit the key: the shape depends on configuration. An object with `status: "disabled"` and zero counts: zeros would read as "nothing is behind". |
| OA6 | Status precedence and scope in `/health/ingest` | `no_series`, `degraded` (any `conflicted_series`), `behind`, `catching_up`, `current`, following ADR-0003 P-8 literally (O-4 was approved as proposed); stored series only, retired ones flagged `configured: false`, as for candles. | `behind` before `degraded`: freshness would never be hidden by an old conflict, but it departs from the approved ADR wording. Judging configured series only: hides a retired series, which S20 (U6) deliberately did not do. |
| OA7 | Does the response list individual gaps or expose `ingested_at_ms` or units? | No: counts only (as in the candle response), no `ingested_at_ms` (Q5), units documented as inferred, not a field. A per-run gap listing for open interest is a later, separate section if wanted. | A runs list or an `/v1/open-interest/coverage` endpoint: useful, but the issue scope is one endpoint plus the health object, and it would double the contract surface. |
| OA8 | Is the health `conflicted_series` window about the slot or about when the conflict was recorded? | The slot (`as_of_ms - 24 h < time_ms`), as ADR-0003 P-8 words it. | `first_seen_at_ms`: also catches a late conflict in an old slot, but the ADR says slot and the index serves `time_ms`. |
| OA9 | What does `/health/ingest` do with an open interest series it cannot judge (a `period` other than the configured one, an unaligned newest `time_ms`)? | It is counted in `series_count` and in a new field `unjudged_series`, judged as neither current, due nor stale, and makes the status `degraded`; the answer stays `200`. Open interest must not change the candle part (ADR-0003 P-8). | A `500` for the whole answer, as for an unjudgeable candle series (section 6.2.3): consistent with that rule, but a hand-edited open interest row would then take down the candle health too. |
| OA10 | What is the defaulted `end` of `GET /v1/open-interest`? | As for candles (section 5.1): `newest + period` (or one slot when `start` is after the newest row), so a defaulted window starting at or before the newest row has no slot after it and does not show the age of a stopped series; the contract says so (6.5.2) and sends the consumer to `/health/ingest` or to an explicit `end`. | As `/v1/indicators` (section 6.3.5): the end of the newest *expected* slot, so a stopped series answers `stale` by default; it is the better default for freshness, but it differs from the endpoint this one mirrors and from the issue text ("mirror `GET /v1/candles`"). |
| OA11 | Is the `unknown_series` body of this endpoint different from the candle one? | Yes: message "no open interest row of this series has been stored", `details` `venue`, `symbol`, `period` (no `timeframe`), as section 2 now states. | Reuse the candle message and `timeframe`: wrong text and a field the request never had. |
