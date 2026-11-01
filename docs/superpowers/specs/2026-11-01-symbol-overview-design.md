# Symbol overview page — design

- **Date:** 2026-11-01. Revised the same day after the review of the first draft.
- **Status:** APPROVED by the user on 2026-11-01 (the word "schvaluji", given after the list of open questions in section 9). My reading, which the user can correct: it approves the whole document, confirms B1, and accepts my defaults for Q1 and Q2. Q3 stays a manual measurement before large universes.
- **Size:** a small project: one local page and a small program that serves it. No change to the Collector, the API, the database or any contract.
- **How to read this document:** section 4 holds **verified facts** about the API, each with its source. Section 5 holds **design decisions** (mine unless marked as the user's). Section 9 lists the open questions and blockers. A statement is never in both.

Abbreviations: **C§x** = `docs/contracts/api-v1.md` section x. Code references are to `src/quasar_market_data/`.

## 1. Intent

The user wants to see, at a glance, which symbols the Collector downloads, where each symbol's history starts, how complete it is, and **whether a symbol is fine or something is wrong with it**. Today this means calling several endpoints by hand and reading JSON. The page answers one question first: "is anything wrong?". Everything else is detail for the symbols that are not fine.

## 2. Scope and what the user decided

Approved scope: a separate, read-only, local tool. Python standard library plus plain HTML and JavaScript. No change to the Collector, the API, the database or any contract. No new dependency, framework or endpoint.

The user decided:
- Two parts. **Top:** a summary (counts of symbols by state) and a list of **only the symbols that have a problem**, each with the reasons in words. **Below:** a heatmap, one row per symbol, one strip per row for the last 30 days, with the start of the symbol's history.
- Strip colors: candle present, hole, history not there yet.
- Healthy symbols are collapsed in the problem list (my reading: one expandable group "N symbolů v pořádku").
- Read-only, no controls. The existing API is the only data source.
- The page text is Czech, as in the mockups.

## 3. Goals, non-goals, anti-goals

**Goals**
- A problem is visible without reading numbers.
- Works against a local API and against the VPS API through the SSH tunnel (`localhost:8000`, `compose.yaml` header).
- Refreshes by itself and with a button, without loading the API more than needed.

**Non-goals**
- Prices, candle charts, indicators; authentication, history of past states, alerts.
- Timeframes other than `5m`, venues other than `binance-usdm` (the limits of API v1, C§5.1).
- Changing the API or adding endpoints or response fields.
- Judging whether a history is *long enough* (see L2 and Q2).

**Anti-goals (a failure even if it technically works)**
- **Showing "OK" when the truth is unknown.** An unreachable API, an error, a timeout, an invalid or self-contradicting answer all end in `unknown` for the symbol, or in a page-level error. Never an empty list, never green.
- Hiding a problem by how a state is computed: counting the part before the start of history as holes (false alarm) or as fine (hidden problem), inventing time thresholds, or using a clock other than the API's.
- Anything that talks to the exchange, or lets the browser call anything except the three read-only endpoints below.

## 4. Verified facts

Everything here was read in the contract or the code; nothing is inferred from how the API "probably" works.

### 4.1 Slot states and history start

| # | Fact | Source |
|---|---|---|
| V1 | Each slot gets exactly one state, the first that matches: `conflicted` (conflict record, or candle plus unavailable record), `present` (candle), `unavailable`, `missing` (no record and slot ≤ newest stored candle), `beyond_stored` (slot > newest). | C§4; `domain/span_coverage.py::span_slot_states` |
| V2 | Slots before the oldest stored candle are `missing`. The oldest slot itself has a candle, so it is `present` or `conflicted`, **never** `missing`. A slot before the oldest candle is `conflicted` or `unavailable` only if a conflict or unavailable record exists for it. | C§4 row 4; `api/coverage_reader.py` (`oldest` = `MIN(open_time_ms)`) |
| V3 | `runs` lists maximal runs of consecutive slots in **one** non-present state, ascending, `end` exclusive, `slots` = length; `present` is never listed; up to 10 000 runs. Counts and runs come from the same slot sequence. Two adjacent runs of different states never merge. | C§6.1.3; `domain/timed_coverage.py::classify_span_at` |
| V4 | Consequence of V2 and V3: a `missing` run that starts at or before the oldest candle ends **at or before** the oldest candle's open time, because that slot is not `missing`. | derived from V2, V3 |
| V5 | A series that exists has at least one candle; candles are never deleted. So the newest open time of a series never moves earlier. (That backfill can add candles older than the oldest one is my reading of the backfill feature, `domain/backfill.py`; I read only its header, not the write path, so the design does not rely on it: `O` is read afresh from every answer.) | C§5.2 (series block) |

### 4.2 Time

| # | Fact | Source |
|---|---|---|
| V6 | `as_of_ms` is the server clock, read **once per request**. Every state of that response is computed with it. | C§6.1.3; `api/app.py` coverage handler (`now_ms()` once) |
| V7 | For a slot **after the newest stored candle** with no record: `expected_at = slot open + timeframe + publication_delay`; `not_yet_expected` if `as_of < expected_at`; `due` if `expected_at ≤ as_of ≤ expected_at + lag_threshold`; `stale` if `as_of > expected_at + lag_threshold`. The thresholds (`rule`) are server configuration, bounded to 300 000 ms and 900 000 ms. `missing`, `present`, `conflicted`, `unavailable` never depend on the clock. | C§6.1.2, C§6.1.5; `domain/timed_coverage.py::timed_slot_states`; `api/rule.py` |
| V8 | The slot right after the newest candle has the earliest `expected_at`, so its state is the worst of all later slots. Coverage's `stale_slots > 0` / `due_slots > 0` therefore agrees with `/health/ingest`'s `stale` / `due` for the same series whenever that slot is in the window. | `domain/freshness.py` docstring; `tests/domain/test_freshness.py::test_agrees_with_classify_span_at_over_all_slots_after_newest` (named in INV-057) |
| V9 | `stored_ahead_of_as_of` is true when `newest + timeframe + publication_delay > as_of`. It changes no count; when true, `due == stale == 0`. The contract names a server clock behind the Collector's, or a larger configured delay, as possible causes. | C§6.1.3; `domain/timed_coverage.py` (`TimedCoverage` docstring) |
| V10 | Valid timestamps are `946684800000 ≤ t ≤ 4102444800000`; times are integer epoch ms, UTC; a slot covers `[open, open + 300 000)`. | C§1 |

### 4.3 Which endpoints say what

| # | Fact | Source |
|---|---|---|
| V11 | `/v1/coverage` takes `venue, symbol, timeframe, start, end`, all required, no other parameter; aligned; `end > start`; at most **10 000** slots. 30 days of `5m` are 8 640 slots. | C§6.1.1; `api/params.py` |
| V12 | A series with no stored candle answers `404 unknown_series`: "A typo and a not-yet-ingested symbol look the same, and a newly configured instrument answers 404 until its first candle is stored." | C§2 |
| V13 | `/v1/instruments` returns every configured instrument (ordered by triple, unpaginated) with `newest_open_time_ms`, or `null` when nothing is stored. `null` "says nothing about why". | C§6.4.2; `api/instrument_reader.py` |
| V14 | `/health/ingest` has `as_of_ms`, `rule`, complete counts, but its `behind` list and `universe.missing` list are **capped at 50 entries** (`truncated` / `missing_truncated`). `universe.missing` "does not say why ... and does not judge time". | C§6.2.3; `HEALTH_BEHIND_MAX`, `HEALTH_MISSING_MAX` in `api/app.py` |
| V15 | **The API cannot tell a symbol whose first candle is not yet expected from a symbol that should have been downloaded and was not**: V12, V13 and V14 all say the same thing for both. | V12–V14 |
| V16 | The API does not know how deep the history *should* reach: "that the stored history reaches back to any date" is explicitly not promised. The intended depth (`collector.backfill.days = 30`) is only in the configuration file, which the tool does not read. | C§0 last paragraph; `config/quasar.toml` |

### 4.4 Errors, load and deployment

| # | Fact | Source |
|---|---|---|
| V17 | Every error the application produces has the body `{"error":{"code","message","details"}}`. `503 not_ready` carries `reason` and, for `database_missing` / `database_unreadable`, `Retry-After: 5`. Errors the HTTP server produces before the application (malformed request line, oversized URI) are plain responses outside the contract. A `503` means "no data", never "empty data". | C§2 |
| V18 | The SQLite busy timeout is 5 000 ms; `/health/ready` and `/health/ingest` can wait that long before `503`. | `storage/connection.py::DEFAULT_BUSY_TIMEOUT_MS`; C§6.2.3 |
| V19 | The cost of one `/v1/coverage` request is bounded: counts over at most 10 000 slots in primary-key ranges of three tables. **No benchmark of many coverage requests exists** (the Makefile has `bench`, `bench-health`, `bench-indicators` only). `/health/ingest` is measured at 600 series × 100 000 rows. | C§5.1; `Makefile`; INV-057 |
| V20 | The API is one `uvicorn.run` process with no concurrency limit; SQLite calls run in Starlette's thread pool; the compose memory limit is 576 MiB. The pool size is the library default and was not checked. | `api/cli.py`; `api/app.py` (`run_in_threadpool`); `compose.yaml` |
| V21 | No CORS and no authentication in v1; on the VPS the API has no published port and is reached through an SSH tunnel. | C§7, C§1 Exposure; `compose.yaml` header |
| V22 | The configured universe is 10 symbols today; the contract's target is 300–600. | `config/quasar.toml`; C§6.4.2 |

### 4.5 The status of `/v1/instruments`

| # | Fact | Source |
|---|---|---|
| V23 | Contract section 6.4 says "**PROPOSED for S20, needs the user's approval** (decisions U1 to U7)"; section 9 repeats "PROPOSED, awaiting the user's approval". The README says the same. | C§6.4 first bullet; C§9 S20 table heading; `README.md` (Instruments paragraph) |
| V24 | The endpoint **is implemented and enforced by tests**: `@app.get("/v1/instruments")` in `api/app.py`; commit `3a27ced` (2026-10-29, "feat: S20 ..."); INV-066 and INV-067 are ENFORCED; follow-up F-18 is "closed by S20". | `api/app.py`; `git log`; `docs/invariants.md`; `docs/follow-ups.md` |
| V25 | I found **no record of the user's approval** of section 6.4 or of U1–U7. For section 6.3 such a record exists (commit `084c8b5`, "docs: record the approval of contract section 6.3"); the contract's status line lists approvals for S10, S12, S18 and S19 but not S20. | `git log -- docs/contracts/api-v1.md`; C line 3 |

## 5. Design decisions

### 5.1 Parts

1. **A small local web program** (Python standard library, `ThreadingHTTPServer` or equivalent, binds `127.0.0.1`). It serves the page and one endpoint, `GET /overview.json`. It accepts only `GET`, answers anything else with `405`, and any other path with `404`. It calls only `GET /health/ingest`, `GET /v1/instruments` and `GET /v1/coverage` on the API. Its settings are the API base URL (default `http://localhost:8000`) and its own listening port. This is how the page works despite V21 (no CORS): the browser talks only to the tool.
2. **A page** (one HTML file, plain JavaScript) that fetches `/overview.json` and draws it. It contains no rule about states.
3. The aggregation is a **pure function** (inputs: the three API answers and the window; output: the overview object), kept apart from the I/O, so it is tested with pytest (CLAUDE.md: separate pure logic from I/O). Where the files live follows the project's existing pattern; the tool is not part of the `quasar_market_data` package and does not import it (so it can run on a machine with only the tunnel); `make check` must still pass.

### 5.2 One refresh

1. `GET /health/ingest`. Used **only** for `as_of_ms` (call it `T0`) and `rule`. Its lists are not used because of V14.
2. `GET /v1/instruments`. The complete list of symbols and the only source of `no_data` (V13).
3. For every instrument with `newest_open_time_ms != null`, `GET /v1/coverage` with the window `[S, E)` of 5.3. Instruments with `null` get no coverage call (it would be `404`, V12).
4. The pure function validates each answer (5.6), judges each symbol (5.4), builds the strip (5.7), and returns the overview (5.8).

Steps 1 and 2 run first and must both succeed (5.9); step 3 runs under the concurrency, timeout and deadline of 5.10.

### 5.3 The window (one deterministic rule)

With `tf = 300 000` ms and `T0` from `/health/ingest` (must be an `int` in the V10 range):

- `E = (T0 // tf + 1) × tf` — the end of the slot that contains `T0`.
- `S = E − 8640 × tf` — exactly 30 days, 8 640 slots (≤ 10 000, V11), aligned.

Why `E` is enough: every slot whose `expected_at ≤ T0` opens at or before `T0 − delay − tf`, which is before `E`; the slot containing `T0` is included and is `not_yet_expected` (V7). The slot after the newest stored candle is therefore in the window whenever `newest + tf < E`, which gives V8's guarantee. A series whose newest candle is **before** `S` has every window slot `beyond_stored`, so it reads `stale` (or `due`) and never looks fine.

The tool uses **no clock of its own** to judge anything. States come only from the counts and runs of the coverage answer, which were computed with that answer's own `as_of_ms` and `rule` (V6, V7). The tool compares **no millisecond value against a threshold** and defines no time threshold.

### 5.4 Judging one symbol

Input: a *validated* coverage answer `c` (5.6), the oldest and newest open times `O` and `N` from `c.series`, the window `[S, E)`.

**Step A, history start.** `O` is shown as "Historie od". Slots of `[S, O)` are **pre-history**, not holes.
- For each run `r` with state `missing`: if `r.end ≤ O`, the run is entirely pre-history and is excluded from the problems; if `r.start < O < r.end`, the answer contradicts V4 and the symbol is `unknown` (`inconsistent_runs`); otherwise the run is a real hole.
- `real_missing_slots` = `c.missing_slots` − (slots of pre-history `missing` runs); `hole_runs` = number of real `missing` runs.
- Pre-history slots in a state other than `missing` (a conflict or unavailable record before the oldest candle, V2) are **not** excluded: they are real findings.

This uses only integer arithmetic on aligned values (checked in 5.6), so the boundary cases are exact: `O == S` → no pre-history; `O == S + tf` → exactly one pre-history slot; `O ≥ E` → the whole window is pre-history and nothing is a hole; this needs a stored candle at or after the end of the current slot, which `stored_ahead_of_as_of` (V9) normally flags and 5.4 turns into `unknown`.

**Step B, reasons.** The reasons that apply, from the counts of `c` (after Step A):

| Code | State | When | Text (Czech) |
|---|---|---|---|
| `no_data` | `no_data` | `/v1/instruments` says `newest_open_time_ms` is null | "nic nestaženo (API nerozliší, zda se mělo)" |
| `invalid:<reason>`, `transport:<reason>` | `unknown` | validation (5.6) or the call (5.10) failed | the reason |
| `stored_ahead` | `unknown` | `c.stored_ahead_of_as_of` is true | "hodiny serveru jsou před uloženými daty, čerstvost nelze posoudit" |
| `stale` | `stale` | `c.stale_slots > 0` | "poslední candle před … , N slotů po termínu" |
| `conflict` | `conflict` | `c.conflicted_slots > 0` | "N slotů v konfliktu" |
| `missing` | `holes` | `real_missing_slots > 0` | "N chybějících slotů ve K úsecích" |
| `unavailable` | `holes` | `c.unavailable_slots > 0` | "N slotů potvrzeno burzou jako bez dat" |
| `due` | `behind` | `c.due_slots > 0` | "zaostává o N slotů" |

- The **headline state** is the first that applies in the order `no_data, unknown, stale, conflict, holes, behind`; `ok` if none applies. **All** reasons are kept, so a stale symbol that also has holes shows both. The summary counts each symbol once, by its headline state.
- `not_yet_expected` is never a reason (V7). `ok` is a positive statement: the answer validated, the clock states are clean and no real finding exists. It does **not** say the history is long enough (L2).
- `unavailable` is in `holes`, not `ok`: the Collector does not write such records in v1 (C§4, INV-054), so if one appears it is a data-quality finding that must not be hidden; no new state is introduced.
- `stored_ahead` is `unknown` because V9 says that then `due` and `stale` are zero by construction, so a stopped Collector could look fine; with host and Collector on one machine it should not happen in normal operation. (Decision; see Q1.)

### 5.5 What the tool does not claim

L1. For `no_data` the cause is unknown (V15). A just-added symbol and a broken one look the same. They are shown as their own state and tile, not as an error, and the text says so.
L2. The length of a history is shown (start date, grey part of the strip) but not judged (V16). A symbol with only two days of history and no holes is `ok`; the user sees the grey strip.
L3. `ok` is true at the API's `as_of_ms` of that symbol's answer, not a statement about the future (C§6.1.4).

### 5.6 Validation of the answers (minimal, in the pure function)

A failure of any check gives `unknown` for the symbol (steps 1 and 2: a page-level error, 5.9), with a reason code. Checks are cheap and only about internal consistency:

**Every response:** HTTP status 200; `Content-Type` is JSON; the body parses as UTF-8 JSON, is an object, and is at most 2 MB (a full 10 000-run body is about 0.7 MB, C§6.1.3). A non-200 whose body matches the V17 shape is reported with its `error.code`; any other non-200 is `bad_status`.

**`/health/ingest`:** `as_of_ms` is an `int` in the V10 range; `rule.publication_delay_ms` and `rule.lag_threshold_ms` are non-negative `int`.

**`/v1/instruments`:** `instruments` is a list; each item has string `venue`, `symbol`, `timeframe` and `newest_open_time_ms` that is `null` or an `int` in the V10 range aligned to 300 000; no duplicate `(venue, symbol, timeframe)`; `venue == "binance-usdm"` and `timeframe == "5m"` (anything else is outside API v1, C§5.1, and the symbol becomes `unknown`, not silently skipped). **An empty list is a page-level error** ("žádné nakonfigurované symboly"), never an all-green page.

**`/v1/coverage` for one symbol:**
- *Echo:* `venue`, `symbol`, `timeframe`, `start`, `end` equal the request.
- *Types and ranges:* the nine `coverage` fields have the right types (eight non-negative `int`, `fully_present` bool); `series.oldest_open_time_ms ≤ series.newest_open_time_ms`, both aligned `int` in the V10 range; `as_of_ms` an `int` in the V10 range.
- *Counts:* `slots == (end − start) / tf == 8640`; the seven `*_slots` sum to `slots` (C§6.1.2); `fully_present == (present_slots == slots)`.
- *Runs:* each run has a state in `{conflicted, unavailable, missing, not_yet_expected, due, stale}` (never `present`); `start` and `end` aligned, `start ≥ S`, `end ≤ E`, `start < end`, `slots == (end − start)/tf`; runs ascending and non-overlapping (`next.start ≥ prev.end`); **for each state, the sum of `slots` over its runs equals that state's `*_slots`** (V3); therefore the slots not in any run equal `present_slots`.
- *Cross-checks with the series block:* `not_yet_expected`, `due` and `stale` runs start **after** `N` (V7); `missing` runs end at or before `N + tf` (V1); no slot before `O` is `present` (V2: the runs cover `[S, min(O, E))` completely).
- *Consistency with the other answers:* `c.rule` equals the `/health/ingest` rule; `T0 ≤ c.as_of_ms ≤ T0 + refresh deadline` (a clock that went backwards, or a different server, gives `clock_inconsistent`); `c.series.newest_open_time_ms ≥` the `/v1/instruments` value for that symbol (newest only moves later, V5), otherwise `regressed`.
- A `404 unknown_series` for a symbol that `/v1/instruments` showed as stored is `inconsistent_with_instruments` (V5, V12), not `no_data`.

### 5.7 The strip

The overview does not forward raw runs (up to 10 000 per symbol would be too much at 600 symbols). Per symbol it sends a string of **720 characters**: bucket `k` covers slots `[S + 12k·tf, S + 12(k+1)·tf)` (one hour, 8 640 / 12 = 720). Each character is the **worst** state among the bucket's non-pre-history slots, in the order `stale > conflicted > missing > unavailable > due > not_yet_expected > present`; a bucket that contains only pre-history slots is "history not there yet". The string is built from the runs (cost proportional to the number of runs, not slots). The worst state wins so a one-slot hole stays visible in its bucket. Tooltip: the bucket's UTC time range and its state; the problem list shows exact counts. Payload for 600 symbols is about 0.5 MB.

Colors must not rely on hue alone: the tooltip and the problem list carry the state in words.

### 5.8 The overview object

```
{
  "generated_at_ms": T0,                    # the API's clock, from /health/ingest
  "rule": {"publication_delay_ms": ..., "lag_threshold_ms": ...},
  "window": {"start": S, "end": E, "bucket_slots": 12},
  "refresh": {"duration_ms": ..., "deadline_hit": false},
  "summary": {"total": n, "ok": n, "behind": n, "holes": n, "conflict": n, "stale": n, "unknown": n, "no_data": n},
  "symbols": [ {"venue","symbol","timeframe","state","reasons":[{"code","slots","runs"}],
                "oldest_open_time_ms","newest_open_time_ms","as_of_ms","strip":"…720 chars…"} ]
}
```
or, at page level, `{"api_error": {"code": "...", "message": "..."}}` with a non-200 status (builder picks 502/503). The page shows `generated_at_ms` and, from the **browser** clock, how old the snapshot is; that age is display only and never decides a state.

### 5.9 Failure handling

| Failure | Result |
|---|---|
| `/health/ingest` or `/v1/instruments` fails in any way (connection, timeout, `503`, `500`, bad status, invalid body, empty universe) | One page-level message ("API není připravené: <reason>" / the error); **no** symbol states |
| `503 not_ready` | Same; the reason (`database_missing`, `schema_mismatch`, `database_unreadable`) is shown; `Retry-After` is not honored (the next refresh is the retry) |
| One symbol's coverage fails in any way, or fails validation | That symbol is `unknown`, with the reason; other symbols are unaffected |
| Deadline reached (5.10) | Symbols without an answer are `unknown` (`deadline`) |

`unknown` counts as a problem in the summary and is listed first among problem states after `no_data`; it never counts as `ok`.

### 5.10 Refresh, concurrency and load

Without a new endpoint and without loading the API when nobody looks:

- **Lazy.** The tool refreshes only when a client asks. No background polling.
- **Single flight.** At most one refresh runs at a time. A request that arrives during a refresh waits for that refresh and gets its result; it never starts a second one.
- **Snapshot cache.** A finished snapshot is served for at least **15 s** (constant); the button within that time returns the same snapshot (the page shows its age). Many open tabs therefore cost one refresh per 15 s at most.
- **No overlap in the browser.** The page schedules its next poll (60 s) only after the previous request finished or failed.
- **Bounded concurrency.** At most **4** API calls in flight (a thread pool). The number is my choice and **not measured** against `/v1/coverage` (V19); it is a constant that tests can override.
- **Timeouts.** Each API call has a total timeout of **10 s**, longer than SQLite's 5 s busy timeout (V18), so a locked database shows as `503 database_unreadable` and not as a client timeout. No retry inside a refresh (a retry doubles the load; the next refresh is the retry).
- **Refresh deadline: 60 s** in total. When it is reached no new call starts and unanswered symbols become `unknown` (`deadline`); in-flight calls end by their own timeout.
- **At 300–600 symbols** a refresh is 2 + N calls. With 4 in flight and unknown latency, whether it finishes inside the deadline is not known (V19, V22); the deadline makes the failure visible, not silent. Before the tool is used at that size, the timing of `/v1/coverage` under 4 concurrent calls on a realistic database must be measured by hand (Q3). With the 10 symbols of today: 12 calls per refresh.
- The tool's own queueing is bounded by the number of symbols; it holds no unbounded state, and `/overview.json` rejects any query parameter with `400`.

### 5.11 Heatmap and list, as the user described

Top: summary tiles (`ok`, `behind`, `holes`, `conflict`, `stale`, `unknown`, `no_data`), then the problem list (every symbol whose headline state is not `ok`, ordered by severity then symbol, with all reasons in words), then the collapsed group of `ok` symbols. Below: the heatmap with all symbols, "Historie od" (`O`), and the strip. Layout, colors and refresh controls are left to the builder.

## 6. Tests

All with pytest, a fake API (a local test server returning canned answers), never production data, never the live exchange (CLAUDE.md).

**Pure function (states and window)**
- Window arithmetic: `T0` exactly on a slot boundary, one ms before, one ms after; `E − S == 8640 × tf`; alignment.
- Partial history: `O == S` (no pre-history); `O == S + tf` (one slot); `O` in the middle; `O ≥ E`; `O < S` (history longer than the window); a real hole directly after `O`; a real hole directly before `N`; the leading `missing` run is never counted as a hole.
- Contradiction: a `missing` run that crosses `O` → `unknown`.
- Pre-history slots that are `conflicted` or `unavailable` are still reported.
- Zero data: instrument with `newest_open_time_ms: null` → `no_data`, no coverage call made; **all** instruments `no_data`; empty instrument list → page-level error.
- Several reasons at once (stale + conflict + holes; due + unavailable): headline order, all reasons kept, each symbol counted once in the summary.
- `ok` only when every listed condition holds; `unavailable_slots > 0` alone is `holes`; `not_yet_expected` alone is `ok`; `due` alone is `behind`; series newest before `S` is `stale`, not `ok`.
- `stored_ahead_of_as_of: true` → `unknown`.
- The tool compares no ms against a threshold: with a fake answer whose counts say `due` but whose `as_of_ms` and `rule` would suggest otherwise, the counts win.

**Validation (each gives `unknown` with a reason, never `ok`)**
- Counts that do not sum; `slots != 8640`; `fully_present` contradicting `present_slots`; a run of state `present`; unaligned, overlapping, out-of-window or unordered runs; per-state run sums differing from the counts; `not_yet_expected`/`due`/`stale` run before `N`; `present` slot before `O`; wrong echo of `symbol`/`start`/`end`; `rule` different from the health rule; `as_of_ms` before `T0` or after `T0 + deadline`; `newest` older than `/v1/instruments`; `404` for a stored symbol; missing or mistyped fields; body that is not JSON, not an object, not UTF-8, or over 2 MB; a wrong `Content-Type`.

**Failures**
- `/health/ingest` or `/v1/instruments`: `503` with each `reason`, `500`, timeout, connection refused, truncated body → page-level error and no symbol states.
- One coverage call: `503`, `500`, `404`, timeout, connection reset, invalid body → only that symbol `unknown`.

**Concurrency and refresh**
- Never more than 4 calls in flight (a fake API that counts concurrent requests).
- Two or more simultaneous `/overview.json` requests produce **one** refresh (a fake API that counts refreshes); both get the same snapshot.
- A request within 15 s of a snapshot makes no API call.
- A fake API slower than the deadline (many symbols): the deadline is honored, the remaining symbols are `unknown` (`deadline`), no new call starts after it.
- A request that arrives while a refresh is running does not start a second one even if the first is about to hit its deadline.
- Payload size for 600 fake symbols with 10 000 runs each stays below a stated bound (about 1 MB).

**Strip**
- Length is always 720; a one-slot hole shows in its bucket; the worst-state order; a bucket entirely before `O` is "history not there yet"; a mixed bucket ignores its pre-history slots; the string is built without per-slot loops over 8 640 slots × 600 symbols being required.

**Tool boundary**
- `POST`, `PUT`, `HEAD`, other paths and unknown query parameters are refused; the tool calls only the three allowed API paths with exactly the documented parameters; it binds `127.0.0.1`.

**Manual check**
- Run the tool against a real API on a test database (not production data), including one symbol with a partial history, one with a hole and one never stored. The builder documents the command in the README (CLAUDE.md: no invented commands).
- `make check` must pass.

## 7. Risks and accepted limits

- A wrong rule in 5.4 is a wrong "OK". The review of this document should check 5.4 and 5.6 first.
- `/health/ingest` marks the last symbols of a Collector round `stale` when the round is longer than `rule.lag_threshold_ms` (C§6.2.3 last bullets). Coverage's `stale` has the same cause (V8), so a short flicker of `behind` or `stale` is expected until the threshold fits the universe. The page shows what the API says; it is not a bug of the page.
- Coverage load at 300–600 symbols is unmeasured (V19). The cost of one request is bounded by the contract; the aggregate is not.
- `/v1/instruments` is implemented but its contract section is not recorded as approved (V23–V25): B1.
- A 30-day window is the Collector's backfill depth; older history, if it exists, is not shown.

## 8. Left to the builder

File locations and names, the start command and its README entry, colors and layout, the HTTP status of the page-level error, test organization under `tests/`.

## 9. Open questions and blockers

**Resolution (2026-11-01):** B1 is treated as confirmed, Q1 stays as designed (`unknown`), Q2 stays as designed (no `short_history`), Q3 remains open as a manual measurement. Recording the approval of contract section 6.4 in `docs/contracts/api-v1.md` is a contract edit, is **not** part of this project, and has not been done; the user decides whether and when.

**B1 — (was a blocker) the approval of contract section 6.4 (`/v1/instruments`).** The contract and the README call it PROPOSED, awaiting approval; the endpoint is nevertheless implemented, tested and used by invariants (V23–V25). I found no record of the approval. CLAUDE.md forbids relying on or changing a public contract without explicit approval, and this tool needs the endpoint (there is no other complete, uncapped list of symbols: V14). Needed from the user: confirm that section 6.4 and decisions U1–U7 are approved (then someone records it, as was done for 6.3 in `084c8b5`), or say it is not. This document does not change the contract. The tool depends only on the four fields `venue`, `symbol`, `timeframe`, `newest_open_time_ms`.

**Q1 — decision (user): `stored_ahead_of_as_of` is `unknown` (5.4).** The alternative is to show it as a warning that does not change the state. My reason for `unknown`: with it, `stale` and `due` cannot occur, so a dead Collector would look fine.

**Q2 — decision (user): flag a short history?** Today a symbol with only a few days of history and no holes is `ok` and the grey part of the strip is the only hint (L2, V16). The expected depth lives in `collector.backfill.days` in the configuration file, which the tool does not read. Options: leave as is (default); or pass the expected depth to the tool as a setting and add a reason `short_history`. The second adds a setting that can disagree with the real configuration.

**Q3 — measurement before large universes (not a blocker today):** the timing of `/v1/coverage` under 4 concurrent calls on a database of realistic size. Needed before using the tool with hundreds of symbols; the constants in 5.10 (4 in flight, 10 s, 60 s, 15 s) are placeholders until then.

**Known limits, not open:** `no_data` cannot be explained (L1, V15); `ok` does not judge history length (L2).
