# OI-6: open interest on the symbol overview page — design

- **Date:** 2026-11-03. **Revision 3** the same day: revision 2 followed the user's targeted review, revision 3 follows an independent read-only review of revision 2 (section 13 lists both).
- **Status:** APPROVED by the user on 2026-11-03 ("schvaluji, U2 varianta C, zbytek jak navrhuješ"): the whole document, **U2 = option C**, and U1, U3, U5, U6, U7, U8 as proposed. U4 (the issue tracker) was not decided: nothing on GitHub is changed without a separate request. **Not covered by this approval:** the contract text (section 6.6 and the edits of 6.5) and ADR-3 amendment A-2, which are approved separately as text before the endpoint is implemented (6.5, 10); and gate G1, which precedes any OI-6b plan.
- **Size:** a project with a written design, built as **two slices and two pull requests**: **OI-6a** a new API endpoint, its contract text and a benchmark harness; **OI-6b** the overview page. Issue #6 of the ADR-0003 tracking issue (#8) covers both.
- **How to read this document:** section 4 holds **verified facts**, each with its source. Section 5 is the **formal slot model** shared by the API, the validator and the tests. Sections 6 to 8 hold **design decisions**, mine unless marked as the user's. A statement is never in both. Section 12 lists what only the user can decide.

Abbreviations: **C§x** = `docs/contracts/api-v1.md` section x. **S§x** = `docs/superpowers/specs/2026-11-01-symbol-overview-design.md` (the page's existing design, in force except where this document says otherwise). **ADR-3** = `docs/adr/0003-open-interest.md`. Code references are to `src/quasar_market_data/` or `tools/symbol_overview/`. `tf` = 300 000 ms.

## 1. Intent

The symbol overview page answers "is anything wrong with the data the Collector downloads?" for candles. Since OI-4 and OI-5 the Collector also stores open interest and the API serves it (`GET /v1/open-interest`, `open_interest` in `/health/ingest`), but the page shows nothing about it. The user wants to see open interest **the same way as candles**: a summary of states, a list of symbols that have a problem with the reasons in words, and a heatmap with a strip per symbol showing where the holes are.

## 2. What the user decided

1. The page shows open interest "the same as now, for candles". The **strip with the position of holes is required**, so the **API is extended with runs**, and **each symbol gets a second strip**. (2026-11-03)
2. The candle summary stays; open interest gets its own row of counts; the problem list shows reasons from both sources, each marked "candles" or "open interest"; the **headline state of a symbol stays the candle state**. (2026-11-03)
3. From the targeted review of 2026-11-03: **a benchmark at 300 and 600 symbols is mandatory before the implementation of OI-6b is approved**; a batch endpoint is **not** added automatically; the candle API, the candle headline and the candle UI are preserved; OI-6a and OI-6b stay separate; **no new configuration key and no database migration**; a change of product behavior (8.4) needs approval.

Everything else is my proposal and is marked as such.

## 3. Goals, non-goals, anti-goals

**Goals**
- A hole, a conflict or a stopped open interest series is visible without reading numbers, as for candles.
- The two strips of a symbol are comparable: the same hour is in the same horizontal position in both.
- **Candle results never depend on open interest** (8.1, 8.9): not on its answers, not on the time its calls take.
- Whether the per-symbol endpoint scales to the target universe is **measured**, not assumed (section 7).

**Non-goals**
- Open interest values, charts, units, or any statement about what the numbers mean (C§6.5.1).
- A combined state of candles and open interest per symbol.
- Judging whether an open interest history is long enough, or **when collection began** (V9).
- A batch endpoint (considered only after the benchmark, 7.4, with its own approval).
- Changing `GET /v1/open-interest`, `GET /v1/coverage`, the existing fields of `/health/ingest`, the Collector, the database schema or any configuration key.

**Anti-goals (a failure even if it technically works)**
- **Showing open interest as fine when the truth is unknown.** An answer that is missing, invalid, late, from a different series, or self-contradicting is `unknown` for that symbol's open interest, never a green strip. A deadline that stops the refresh is never green either.
- An open interest failure, or the time open interest takes, turning a candle state into `unknown` or hiding a candle problem.
- Inventing a time threshold or a decision clock in the page: states come only from the API answers (S§3).
- Hiding "open interest is not enabled" behind an all-green second strip.
- Replacing the unknown start of collection by an estimate (V9).

## 4. Verified facts

Each was read in the contract or the code on 2026-11-03.

| # | Fact | Source |
|---|---|---|
| V1 | A slot of open interest is one `time_ms`, the **end** of a 5m period; the row at `T` belongs to the period `[T − tf, T)`, i.e. to the candle that opens at `T − tf`. | C§6.5.1 |
| V2 | The state of a slot, first match wins: `conflicted` (a conflict record exists for `T`), `present` (a row), `not_yet_expected` (`T + expected_after_ms > A`), `no_longer_recoverable` (`T < A − H`, no row), `due`/`stale` (no row, `T >` the newest row), `missing` (everything else). `A` is the answer's `as_of_ms`, `H` is `retrieval_horizon_ms` (29 days). There is no `unavailable` state and no `stored_ahead_of_as_of` field. | C§6.5.3, C§6.5.4; `domain/open_interest_coverage.py::open_interest_slot_states` |
| V3 | `GET /v1/open-interest` returns counts and rows (at most 1000 per page); it lists no gaps. **No open interest endpoint returns runs** (decision OA7 left them for "a later, separate section"); C§6.5.6 says "open interest has no separate coverage endpoint in v1". | C§6.5.6, C§9 OA7 |
| V4 | The candle `GET /v1/coverage` returns counts and `runs` for at most 10 000 slots, reads only open times, and builds counts and runs from one slot sequence. `runs` is not paginated. | C§6.1.3; `api/coverage_reader.py`; `domain/timed_coverage.py::classify_span_at` |
| V5 | The pure function `open_interest_slot_states` returns `(time_ms, state)` for every slot of a span; `classify_open_interest_span` counts them. Neither groups runs. | `domain/open_interest_coverage.py` |
| V6 | `series.oldest_time_ms` and `series.newest_time_ms` of an open interest answer are `MIN` and `MAX` of `time_ms` over the **rows of the series in the `open_interest` table**, whole series, not only the window. Conflict records do not enter them. | `api/open_interest_page.py::_read` |
| V7 | `open_interest_conflicts` has **no foreign key** to `open_interest`. The Collector's writer records a conflict only after the `INSERT … ON CONFLICT DO NOTHING` found an existing row whose values differ, so **through the Collector a conflict never exists without a stored row**. The contract and the pure function nevertheless allow it ("a stored row, if any", C§6.5.3 state 1; the function does not require `conflicts ⊆ stored`), so a hand-edited database can contain one, and a consumer must accept it. | `storage/migrations/002_open_interest.sql`; `collector/open_interest_writer.py` (the batch loop); C§6.5.3 |
| V8 | `/health/ingest` carries `open_interest`, always present: `null` when open interest is not enabled; otherwise an object with `as_of_ms` (the same value as the top-level `as_of_ms`, judged in the same read transaction), `rule` (`cadence`, `expected_after_ms`, `lag_threshold_ms`), counts and a `behind` list capped at 50. | C§6.5.8 |
| V9 | **The API does not say when collection began.** `oldest_time_ms` is the oldest *stored* row. A slot before it may lie before collection started, or after it started but was never stored (Binance omits periods without trades, a lost row, a catch-up that did not get that far); the API cannot tell these apart (C§6.5.3 state 7). The catch-up depth is the fixed 29-day horizon (ADR-3 P-12, returned as `rule.retrieval_horizon_ms`), but whether a catch-up reached it depends on the Collector process (budget, an in-memory try book, restarts), which the API does not expose. | C§6.5.3; ADR-3 P-12 |
| V10 | The tool (`tools/symbol_overview/`) uses only the standard library and does not import `quasar_market_data`. It makes at most 4 calls in flight; runs **one task per symbol** through `pool.map`; checks the refresh deadline (60 s, measured from the start of the refresh) **once per symbol before its first call**; keeps the whole `Coverage` object of every answer (runs included) until all symbols are judged, then judges; and caches the latest finished snapshot, a page error included, for 15 s; nothing else is kept. `refresh.deadline_hit` is true only when a candle symbol was not asked. | `tools/symbol_overview/refresh.py` |
| V11 | `http_fetch` opens a **new connection for every call** and closes it; `timeout_s` (10 s) applies to **each socket operation, not to the whole call**; the body is capped at 2 000 000 bytes; the allowed paths are a fixed set. | `tools/symbol_overview/fetch.py` |
| V12 | The existing validation accepts a candle answer only if `T0 ≤ as_of ≤ T0 + 60 s + 10 s` (`REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS`), where `T0` is the `as_of_ms` of `/health/ingest`. The window `[S, E)` is derived from `T0` alone. | `tools/symbol_overview/validate.py`, `window.py`, `model.py` |
| V13 | On the VPS the API has no published port and is reached through an SSH tunnel; no CORS, no authentication. The API is one `uvicorn` process with the library-default thread pool (not checked), a 5 s SQLite busy timeout and a 576 MiB container limit that also counts page cache. | C§7; `compose.yaml`; S§4.4 V18, V20, V21 |
| V14 | **Upper bounds** of an answer body with the API's compact serializer (`separators=(",", ":")`, `ensure_ascii=False`), computed on the shape of 6.3: 8 640 runs of the longest state name give about **0.74 MB**, 10 000 runs about **0.86 MB**; these inputs cannot occur (adjacent runs differ in state); an alternation of two states gives about 0.70 MB and 0.81 MB. A 64-character symbol adds about 50 bytes. The independent review recomputed 743 581 and 860 543 bytes. | session computation; the review |
| V15 | Existing benchmarks follow one pattern: a generated database in a fresh temp dir, not part of `make check`, a `bench-*` Makefile target and a README entry (`bench-health` has a 600 series × 100 000 rows `target` profile; `bench-health-oi` has 600 candle and 600 open interest series; `bench_indicators` measures peak memory with `wait4`). The user does not run benchmarks on the VPS (ADR-3 A-1). | `Makefile`; `tests/api/`; ADR-3 A-1 |
| V16 | CLAUDE.md: a public contract or a database schema is not changed without explicit approval; accepted ADR decisions are not reversed silently; commands not configured are not invented. | `CLAUDE.md` |
| V17 | The page already depends on `GET /v1/instruments`, whose contract section 6.4 is still marked "PROPOSED … needs approval" although it is implemented (S§4.5 V23–V25; S§9 B1 treated it as confirmed). That is outside this project. `/v1/instruments` lists every **configured** instrument (`newest_open_time_ms` may be `null`) and never a stored series that is not configured (INV-067). | C§6.4; INV-067; S§9 |

## 5. The slot model: `O`, `N`, runs and counts (formal)

Normative for the contract text (6.6), the API tests and the page validator. Derived from V2 and V6 only.

**Notation.** A window is `W = {S' + i·tf : 0 ≤ i < n}` with `n ≤ 10 000`; the page uses `n = 8 640`, `S' = S + tf`, `E' = E + tf`, so `W = [S', E')` in `time_ms` (8.1). `O` and `N` are `series.oldest_time_ms` and `series.newest_time_ms`; `O ≤ N`; both are rows (V6). `σ(T)` is the state of slot `T` at the answer's `A`.

**Properties of every answer the API produces.** Each follows from the order of V2 and from `O` being the minimum and `N` the maximum row.

| # | Property |
|---|---|
| P1 | `σ(O)` and `σ(N)` are `present` or `conflicted` (a row exists; stored facts win over the clock). A run that contains the slot `O` or the slot `N` is therefore a `conflicted` run. |
| P2 | For `T < O` there is no row, so `σ(T) ∈ {conflicted, not_yet_expected, no_longer_recoverable, missing}`: never `present`, never `due`/`stale` (those need `T > N ≥ O`). |
| P3 | For `T > N`: `σ(T) ∈ {conflicted, not_yet_expected, no_longer_recoverable, due, stale}`: never `present`, never `missing` (rule 5 precedes rule 6). |
| P4 | `due` and `stale` occur only for `T > N`. `missing` occurs only for `T < N`, and `T ≠ O`. So every `missing` run ends at or before `N`, and every `due`/`stale` run starts after `N`. |
| P5 | `conflicted` can occur at **any** position, with or without a row at `T` (V7). It never depends on `O` or `N`, and a conflict record is never used to derive them. |
| P6 | `not_yet_expected` can occur before `O` and before `N` (C§6.5.3: with `c > 1` rows arrive in groups), but only as the **last** states in time order, see P8. |
| P7 | Runs are the maximal runs of consecutive non-`present` slots of one state, ascending; **touching runs (`prev.end == next.start`) have different states**; runs that do not touch (a `present` slot lies between) may have the same state; `present` is never listed. Counts and runs come from one slot sequence: per state, the sum of run `slots` equals that state's count; the six non-present counts and `present_slots` sum to `slots`; `present_slots = slots − Σ run slots`; `gap_free ⇔ present_slots == slots`. |
| P8 | **Order.** Among slots with no row and no conflict, the state never decreases with `T` in the order `no_longer_recoverable < missing < stale < due < not_yet_expected`. (`no_longer_recoverable` iff `T < A − H`; `not_yet_expected` iff `T + expected_after_ms > A`; between them `missing` needs `T < N` and `stale`/`due` need `T > N`; `due` iff `A ≤ T + expected_after_ms + lag`, so `stale` slots have smaller `T` than `due` slots.) Hence, with every `conflicted` run removed, the states of the remaining runs are non-decreasing in this order. |

**Position of `O` relative to the window.**

| Case | Meaning | Consequence |
|---|---|---|
| `O < S'` (before) | the first stored row is older than the window | no slot of `W` precedes `O`; no pre-history in the window |
| `S' ≤ O < E'` (inside) | | slots `[S', O)` precede `O`; by P2 every one of them is in some run |
| `O ≥ E'` (after) | the first stored row is a period that had not started at `T0` | **impossible with consistent clocks** (a closed period starts before `T0`); the API has no `stored_ahead` field (V2), so the page treats `N ≥ E'` as `clock_inconsistent` (8.5) instead of a window full of pre-history |

**Classes the page derives (all from the answer, no estimate):**

| Class | Definition | Problem? |
|---|---|---|
| **before the first stored row** (Π) | `T < O` and `σ(T) ∈ {missing, no_longer_recoverable}` | no (neutral; V9 says what this does **not** claim) |
| **hole inside the history** (H) | `σ(T) = missing` and `T > O` | yes: reason `missing` |
| **unrecoverable hole inside the history** (U) | `σ(T) = no_longer_recoverable` and `O < T < N` | reason `unrecoverable` (8.4, needs approval) |
| **after the end of the stored history** | `no_longer_recoverable` with `T > N` (the series stopped more than `H` ago) | no separate reason: the series is also `stale` after the horizon edge, and `stale` is the reason |
| pending | `not_yet_expected` anywhere | no |
| conflict | `conflicted` anywhere | yes: reason `conflict` |
| lag | `due` / `stale` (only after `N`) | `behind` / `stale` |

## 6. OI-6a — the API: `GET /v1/open-interest/coverage`

### 6.1 Decision: a new endpoint, not a change of the existing one (my proposal, U1)

A new endpoint modelled on `GET /v1/coverage` (V4). `GET /v1/open-interest` does not change. Reasons: it adds without changing an approved response (INV-091 stays true); up to 10 000 runs in a response that also pages 1000 rows would mix two jobs; it mirrors how candles split rows (`/v1/candles`) from coverage (`/v1/coverage`). The cost is one more public endpoint and contract section 6.6. **If the measurement (7.3, G0) says a per-symbol endpoint cannot scale, 6.6 is reconsidered before it is approved**, not after.

### 6.2 Request

`venue`, `symbol`, `period`, `start`, `end`, **all required**, no other parameter (`limit`, `as_of` and anything else: `400 invalid_parameter`). `start` and `end` are aligned `time_ms` values; `end > start`; at most **10 000 slots**; `end` may be one period past the maximum valid time (as in C§5.1). `venue` is `binance-usdm` and `period` is `5m` in v1.

### 6.3 Response `200` (illustrative shape; the contract section is the authority)

```json
{
  "venue": "binance-usdm",
  "symbol": "BTC/USDT:USDT",
  "period": "5m",
  "time_semantics": "period_end",
  "start": 1792672500000,
  "end": 1792674600000,
  "as_of_ms": 1792674530000,
  "rule": { "cadence": 1, "expected_after_ms": 500000, "lag_threshold_ms": 60000, "retrieval_horizon_ms": 2505600000 },
  "series": { "oldest_time_ms": 1792643700000, "newest_time_ms": 1792673400000 },
  "coverage": {
    "slots": 7, "present_slots": 2, "conflicted_slots": 1, "not_yet_expected_slots": 1,
    "no_longer_recoverable_slots": 0, "due_slots": 1, "stale_slots": 1, "missing_slots": 1, "gap_free": false
  },
  "runs": [
    { "state": "conflicted", "start": 1792672800000, "end": 1792673100000, "slots": 1 },
    { "state": "missing", "start": 1792673100000, "end": 1792673400000, "slots": 1 },
    { "state": "stale", "start": 1792673700000, "end": 1792674000000, "slots": 1 },
    { "state": "due", "start": 1792674000000, "end": 1792674300000, "slots": 1 },
    { "state": "not_yet_expected", "start": 1792674300000, "end": 1792674600000, "slots": 1 }
  ]
}
```

The example is **illustrative and computed by hand**, on the window of C§6.5.4: seven slots; rows for the 1st (`…72500000`), the 2nd (conflicted) and the 4th (`…73400000`, the newest); the 3rd has no row and lies before the newest (`missing`); the 5th is `stale`, the 6th `due`, the 7th `not_yet_expected`. The `present` slots 1 and 4 are not listed. (The independent review recomputed all five runs and the counts.)

- Field names and meanings are those of C§6.5.4 minus `rows`, `next` and `coverage.span` (no paging: the span is the window). The `coverage` block has **eight integer counts** (`slots` and seven `*_slots`) **and `gap_free`**, nine fields, as in C§6.5.6 without `span`. **My call:** `gap_free`, not `fully_present`, the name the open interest family already uses.
- A run's `start` is the `time_ms` of its first slot, `end` the `time_ms` of its last slot plus `tf` (exclusive), `slots` its length. P1 to P8 of section 5 hold and are the contract's statements.

### 6.4 Behavior

- **Errors and order of checks** are those of C§6.5.5: `400`, `404 open_interest_disabled`, host clock (`500`), `503 not_ready`, `404 unknown_series` (message and `details` of OA11), `500 internal_error`.
- **Cost:** one read transaction; only `time_ms` of rows and of conflict records in the window are read, never values; an unaligned stored `time_ms` in the window is `500 internal_error`, never a wrong count (as in C§6.5.5).
- **`rule`** comes from the same place as for `/v1/open-interest` (C§6.5.7), so the API and the Collector agree on the cadence (INV-090).
- **Nothing is fabricated:** a slot with no row is never `present`; no value is returned (INV-088).
- **Pure part:** a pure function groups the `open_interest_slot_states` sequence into runs and counts, apart from the reader. Names and locations follow the existing pattern in `domain/` and `api/` (builder).

### 6.5 Documents and artifacts the slice changes

All of these are public-contract or accepted-decision texts and are **approved together, as text, before OI-6a is implemented** (V16):
- `docs/contracts/api-v1.md`: the new section **6.6**; and the edits that 6.6 forces in already approved text: the sentence "open interest has no separate coverage endpoint in v1" in 6.5.6; the `404` rows of the section 2 error table that name only `/v1/open-interest`; the reserved-endpoint table of section 6; one row of the section 8 traceability table; the "Status" line.
- ADR-3 **amendment A-2** (also touching P-11, which lists the API): OA7 left per-run listing for later; the user asked for it on 2026-11-03; the P-16 row OI-6 becomes OI-6a and OI-6b; no rule of P-1 to P-15 changes.
- `README.md`: the open interest reading paragraph, the symbol overview section (OI-6b) and the benchmark entry.
- `docs/invariants.md`: rows for P1 to P8 and for the unchanged existing bodies.
- **A benchmark harness** (7.3) with a `bench-*` Makefile target (V15).
No schema change, no migration, no configuration key.

## 7. Performance: scaling, acceptance criteria and gates

### 7.1 The arithmetic of one refresh (paper model, no measurement yet)

A refresh makes 2 sequential head calls (`/health/ingest`, `/v1/instruments`), then (8.1) a **candle phase** of one call per stored symbol and an **open interest phase** of one call per symbol, on a pool of `w = 4` workers (V10). With a mean per-call latency `L` (service time plus connection setup, V11) the wall time is approximately `D ≈ (2 + ⌈N/4⌉ · k) · L`, with `k = 1` for candles today and `k = 2` with open interest.

| N | calls now → with OI | `D` now | `D` with OI | max `L` for `D ≤ 60 s` (deadline) | max `L` for `D ≤ 45 s` (criterion AC1) |
|---|---|---|---|---|---|
| 100 | 102 → 202 | 27·L | 52·L | 1 153 ms | 865 ms |
| 300 | 302 → 602 | 77·L | 152·L | 394 ms | 296 ms |
| 600 | 602 → 1 202 | 152·L | 302·L | 198 ms | 149 ms |

(Floored, so that `D` does not exceed the limit.) Adding open interest **halves** the latency headroom. Today's candle-only refresh at 600 symbols needs `L ≤ 394 ms`; with open interest it needs `L ≤ 198 ms` to finish before the deadline at all. The head calls are not equal to `L`: `/health/ingest` now also carries the open interest object and `/v1/instruments` grows with `N`; the benchmark reports them separately (AC3). Whether the real `L` is below these numbers is **not known**: no coverage benchmark exists (S§4.4 V19).

What dominates `L` is not known either, and two facts matter: the tool opens a **new connection per call** (V11), and on the VPS every call crosses an **SSH tunnel** (V13), so `L` is the service time plus at least the tunnel's round trips for connection setup and the request. At 600 symbols and 300 calls per worker, 100 ms of extra latency per call alone costs 30 s. A loopback benchmark cannot see this; AC7 adds one measured input for it.

**Effect of the deadline, with the two phases.** Candles run first and finish under the deadline exactly as today. Open interest gets what remains. At `N = 600` and `L = 300 ms` the candle phase takes about 45.6 s, leaving about 14 s: about 190 symbols get an open interest answer and about 410 are `unknown (deadline)` **for open interest only**; every candle state is the same as in a candle-only refresh. The refresh is bounded by the deadline plus the calls in flight, and one call is not itself strictly bounded by 10 s (V11), so the worst case is the deadline plus the slowest in-flight call; the benchmark reports the maximum observed `D`.

**Effect of the snapshot cache and the polling.** A finished snapshot, partial or an error, is served for 15 s; a page polls 60 s after its previous answer (S§5.10); refreshes are single-flight. One tab therefore loads the API for `D` out of every `D + 60 s`; many tabs at different phases at worst start a new refresh as soon as the cache is 15 s old, i.e. `D` out of every `D + 15 s` (for `D = 45 s`, 75 % of the time with 4 concurrent requests). AC4 measures that sustained case.

**Body size** is not the bottleneck: the upper bounds are about 0.74 MB for the page's window and 0.86 MB for 10 000 runs (V14), under the tool's 2 000 000-byte cap with a margin of 2.3× or more. A typical answer has a handful of runs.

### 7.2 Acceptance criteria for a refresh (my proposal of the numbers, U5)

Measured at `N = 300` and `N = 600` (and informatively at `N = 100`) on a generated test database (never production data), with the tool's constants unchanged (4 workers, 10 s per socket operation, 60 s deadline). "Fresh refresh" means a new refresher object each time, so the snapshot cache is not used; the operating system's file cache is whatever a warm machine has, and is reported.

| # | Criterion | Threshold |
|---|---|---|
| AC1 | wall time `D` of a fresh refresh, **maximum over ≥ 10 consecutive refreshes** | ≤ 45 s (75 % of the deadline: headroom for a slower disk and the tunnel) |
| AC2 | failed calls (non-200, transport error, `503`) and `unknown (deadline)` symbols in those refreshes | 0 |
| AC3 | the duration of **each call** at 4 concurrent calls, per endpoint (including the two head calls): p50, p95, p99, max and mean | reported; diagnostic. The mean (`L_mean`) feeds AC7; pass or fail is AC1 and AC7 |
| AC4 | refreshes back to back for ≥ 10 minutes (the sustained case of 7.1) **with a concurrent writer** inserting `N` candle and `N` open interest rows per 5 minutes in the Collector's transaction shape: p99 of an unrelated `GET /health/ready` during the run; number of `503 database_unreadable` | p99 ≤ 1 s; 0 |
| AC5 | peak resident set size of the API process during AC4 (the `wait4` pattern of `bench_indicators`, units normalised between macOS and Linux) | ≤ 432 MiB (75 % of the 576 MiB limit; the container limit also counts page cache, so this is a proxy) |
| AC6 | largest answer body and its decode time | ≤ 1 MB (V14); decode time reported |
| AC7 | **with the tunnel:** `L_tunnel`, the median of ≥ 20 trivial calls (for example `/health/ready`) through the user's real tunnel, supplied by the user; prediction `D_pred = (2 + ⌈N/4⌉·2) · (L_mean + L_tunnel)` | `D_pred` satisfies AC1 |
| AC8 | *(G2 only, the real refresh)* peak resident set size of the tool at `N = 600`, typical profile (stress profile: reported) | ≤ 256 MiB |

On loopback AC1 may pass trivially; the **verdict of G1 is AC1 together with AC7**, and AC7 needs one number from the user, not a VPS benchmark (ADR-3 A-1). The prediction is labelled as a prediction.

### 7.3 The benchmark and the gates

- **The harness** follows V15 (generated database in a fresh temp dir, not part of `make check`, a `bench-*` target, a README entry) and is delivered by **OI-6a**. Its database holds `N` candle series and `N` open interest series, each with at least the 30-day window (candles with the depth of the `bench-health` `target` profile), a realistic few holes and conflict records, and one stress profile where every answer has the maximum number of runs (informative). The concurrent writer of AC4 is part of it. Its driver **imports** the tool's `window_for` and uses the tool's `http_fetch` for the existing paths; only the new path is requested with its own `http.client` call, because the tool's allowed-path set is not changed in OI-6a. It reproduces the tool's call order (the two phases of 8.1, 4 workers, a new connection per call). Pure parts (percentiles, window, call order) are unit-tested like `bench_open_interest_write.py`. This is a copy of a call pattern and can drift; G2 is its check, and the ratio of G2 to the G1 prediction is reported.
- **Gate G0 (before contract section 6.6 is submitted for approval).** The harness is run first, with `GET /v1/coverage` standing in for both calls (same cost class: a primary-key range read of at most 10 000 slots; **an assumption, labelled as one**). The numbers are shown with 6.6, so that a scaling problem can change the design of 6.6 (per-symbol or batch) **before** it is frozen. G0 is informative: it adds nothing to the endpoint by itself.
- **Gate G1 (before OI-6b implementation is approved).** OI-6a is merged; the benchmark has been run with the real endpoint at `N = 300` and `N = 600` on the user's machine and the numbers (machine, disk, SQLite version, date, sample sizes) are recorded in this document; AC1 to AC7 are evaluated; **the user reads them and then approves the implementation of OI-6b.** Without G1 no OI-6b plan is written.
- **Gate G2 (before OI-6b is merged).** The same criteria, AC8 included, are re-measured with the real refresh. **A failed G2 stops the merge**: the options of 7.4 apply, each re-measured, and the user decides.

### 7.4 Decision rule: is the per-symbol endpoint enough? (decided on the numbers, not now)

| Measured result | What follows |
|---|---|
| AC1 to AC7 pass at 600 | the per-symbol endpoint is enough; **no batch endpoint**. |
| pass at 300, fail at 600 | the user chooses: a supported-universe limit for the page, one of the cheaper options below, or a design for a batch endpoint. |
| fail at 300 | the same choice, with the cheaper options tried first. |

The cheaper options, in order of cost, each re-measured before the next: (a) connection reuse per worker in the tool (V11: removes the per-call setup; no API change; it touches the candle path of the tool, so it needs its own approval); (b) a higher worker count within a stated bound (more API load, AC4 re-measured); (c) asking open interest only for symbols the page needs. A **batch endpoint is a public contract change**: it would get its own design, an ADR amendment and the user's approval, and is never added because a criterion failed. This section is a rule, not a prediction: I make no claim now about which row will apply.

## 8. OI-6b — the page

### 8.1 One refresh, in two phases (additive to S§5.2)

1. `GET /health/ingest` as today, plus the `open_interest` key (V8): `null` → open interest is **disabled**: no open interest call is made and the page shows one sentence in place of the second strip and the second summary row. An object → **enabled**; its `as_of_ms` must equal `T0` and its `rule` must be well-formed. Any other value, or an object that fails these checks, makes the open interest part `unknown` for all symbols (`invalid:schema`); **the candle part is unaffected**.
2. `GET /v1/instruments` as today.
3. **Candle phase.** Per instrument with a stored candle, `GET /v1/coverage` as today: the same code path, the same per-symbol deadline check, the same results. When the candle phase is over, every candle outcome is final.
4. **Open interest phase** (only when enabled). For **every** instrument, `GET /v1/open-interest/coverage` for the window `[S + tf, E + tf)` with `S`, `E` from S§5.3. The shift is because the slot `T` is a period end (V1): slots `S + tf … E` describe exactly the periods `[S, E)` the candle window covers, so bucket `k` is the same hour in both strips. The window has 8 640 slots. **My call:** every instrument gets the call, because open interest `no_data` can only be learned from `404 unknown_series` (V8's `behind` list is capped).

**Why two phases (a decision forced by the review).** If a symbol's two calls were one task under one deadline, the open interest calls would use up the budget the candle calls need today, and symbols reached late would lose their **candle** answer too (7.1: between 198 and 394 ms per call at 600 symbols, candle-only finishes and the combined refresh does not). That would turn candle states into `unknown` because of open interest, which section 3 forbids. With the phases, candle behavior is identical to a candle-only refresh whatever open interest costs, and open interest gets the remaining time of the same single deadline.

The allowed paths gain `/v1/open-interest/coverage` and nothing else. At most 4 calls in flight in total, one pool for both phases.

### 8.2 Failure handling and the partial refresh

- **One deadline, 60 s from the start of the refresh, checked before every call** in both phases. No call starts after it. (Today it is checked once per symbol.) The check keeps every answer's `as_of` inside the bound of 8.3 in all but pathological cases (a call that starts just before the deadline and connects or reads for many seconds: the 10 s timeout is per socket operation, V11); such a case can be rejected as `clock_inconsistent`, which fails closed.
- **Per part.** The candle part and the open interest part of a symbol are judged independently. A failed candle call does not prevent the symbol's open interest call, and the reverse. Open interest symbols not reached by the deadline are `unknown (deadline)` **for open interest only**. Calls in flight at the deadline finish by their own timeout and their valid answers are used.
- **A partial snapshot is a result, not an error:** HTTP 200, cached 15 s like any finished snapshot. **`refresh.deadline_hit` keeps its meaning (a candle symbol was not asked, V10)**; open interest adds `refresh.oi_deadline_hit` and the counts `refresh.candle_not_asked` and `refresh.oi_not_asked`.
- **No other retention.** The only retained snapshot is the latest finished one for 15 s (V10). There is no "last good" fallback and no stale-while-revalidate; a page-level failure of `/health/ingest` or `/v1/instruments` still draws no symbol states (S§5.9). Open interest never borrows a previous refresh's answer.
- **Never OK when missing or invalid.** The only path to an open interest `ok` is a validated answer (8.5) with no real finding. Every other path is `unknown`, `no_data` or "disabled" in words: a transport failure, a non-200, a body over the cap, bad JSON, an invalid answer, a deadline, a `404 open_interest_disabled` that contradicts the health answer, a missing or malformed `open_interest` key. A page that receives an overview without the open interest block (an older tool) shows "open interest is not reported by the tool", never a green strip.
- **Body size.** Upper bounds 0.74 MB and 0.86 MB (V14) against the 2 000 000-byte cap; `MAX_BODY_BYTES` is unchanged. A test builds the largest feasible body with the API's own serializer and asserts the API-side size and the tool-side decode.
- **Memory, and where the judging happens.** For open interest the worker calls the pure judging function and returns the judged report (state, reasons, strip, dates); the open interest runs are not kept until the end. This differs from the candle path, which keeps its `Coverage` objects and judges after `pool.map` (V10); the candle path is left as it is, and G2 reports the tool's peak memory (AC8).

### 8.3 Snapshot consistency: the clocks of one refresh

| Clock | What it is | Relation |
|---|---|---|
| `T0` | `as_of_ms` of `/health/ingest`; also `health.open_interest.as_of_ms` (V8, same transaction) | the only clock the window is derived from |
| `A_c(i)` | `as_of_ms` of symbol `i`'s candle coverage, read once at that request | `T0 ≤ A_c(i)` |
| `A_o(i)` | `as_of_ms` of symbol `i`'s open interest coverage, at its own request | `T0 ≤ A_o(i)` |

A refresh has up to `1 + 2N` different server clocks, all inside `[T0, T0 + D]`, and with two phases every `A_o` is later than every `A_c`. Consequences and rules:
- Each state is true **at its own answer's `as_of_ms`** (S§5.5 L3). The two parts of one symbol, and different symbols, are judged up to `D` apart. A slot `not_yet_expected` at `T0` may be `due` at `A`; a `due` may become `stale` during a long refresh: a flicker the page shows as the API says (S§7), not a bug.
- The window is fixed from `T0`. If a refresh crosses the end of the period that contains `T0`, the new current period lies outside the window; no state is wrong, the window just lacks it.
- **Time-consistency checks, with no decision clock in the page** (they are **assumptions of consistent clocks**, 8.5, U6): `T0 ≤ A ≤ T0 + REFRESH_DEADLINE_MS + CALL_TIMEOUT_MS` for every answer (the existing constants, V12; they bound consistency and decide no state); `health.open_interest.as_of_ms == T0`; `N < E'` (a row for a period that had not started at `T0` means the host clock and the Collector's disagree; the answer is `clock_inconsistent`). **No comparison between `A_c(i)` and `A_o(i)`**, and none against the browser's or the tool's clock. The tool's monotonic clock only decides when to stop starting calls (8.2).
- Each symbol's answers carry their `as_of_ms` in the overview object; the page header shows `generated_at_ms = T0`.

### 8.4 Judging one symbol's open interest (mirror of S§5.4, with the classes of section 5)

Input: a validated answer, `O`, `N`, the window.

**Step A, before the first stored row (Π).** `missing` and `no_longer_recoverable` slots before `O` are not problems. Slots before `O` in other states stay findings (`conflicted`) or pending (`not_yet_expected`, P6). **This is "before the first stored row", not "before collection started" (V9):** the page never writes the second and never estimates a start from configuration. The cost of the rule is stated: a catch-up that did not reach back far enough, or lost first rows, looks the same as a late start; the page cannot tell them apart and says only what the API says.

**Step B, reasons** (open interest has no `unavailable` and no `stored_ahead`, V2):

| Code | Headline state | When | Text |
|---|---|---|---|
| `no_data` | `no_data` | `404 unknown_series` | "no open interest stored (the API cannot tell whether some should have been)" |
| `invalid:<r>`, `transport:<r>`, `deadline` | `unknown` | validation or the call failed | the reason |
| `stale` | `stale` | `stale_slots > 0` | "N slots overdue, last row …" |
| `conflict` | `conflict` | `conflicted_slots > 0` | "N slots in conflict" |
| `missing` | `holes` | class H > 0 | "N slots without a row (runs: K)"; says that Binance omits periods without trades and the API cannot tell that from a lost row (C§6.5.3) |
| `due` | `behind` | `due_slots > 0` | "behind by N slots" |
| `unrecoverable` | see U2 | class U > 0 | "N slots inside the stored history have no row and are older than the retrieval horizon (`H` from the answer), so they can no longer be fetched" |

- **`unrecoverable` is a change of product behavior and needs the user's approval (U2).** The fact that decides it: the window is 30 days and the horizon 29, so `no_longer_recoverable` slots exist only for `T < A − H`, **about 288 of the 8 640 slots (one day) at the start of the window**. A hole is `missing` (headline `holes`) for 29 days, `no_longer_recoverable` for about one day, and then leaves the window. The reason, a strip character and possibly a state would therefore cover a one-day band.
  - **Option C (DECIDED by the user on 2026-11-03):** the reason `unrecoverable` is an **information line** on the symbol and the strip shows `r`; it maps to **no headline state** and no summary tile; a symbol with only that finding stays `ok`, in the "ok" group, with the note.
  - **Option A:** the reason has its own headline state `unrecoverable`, the **least severe** problem state (order `no_data, unknown, stale, conflict, holes, behind, unrecoverable, ok`), an open interest summary tile, and a place in the problem list.
  - **Option B:** strip only; no reason (revision 1).
  Under A and C the headline of a symbol with other reasons is decided by them as before.
- `not_yet_expected` and "after the end of the stored history" (section 5) are never reasons. `ok` means the answer validated and no real finding exists; it does not say the history is long enough or that collection began when it should have.
- The tool compares no millisecond value against a threshold and uses no clock of its own (S§3).

### 8.5 Validation of an open interest answer

Any failure is `unknown` with a reason code for that symbol's open interest only. A `404` with error code `unknown_series` is `no_data`; any other non-200 is reported with its `error.code`. The structural and position checks are **derived from section 5 and cannot reject an answer the API produces**. The clock checks **assume consistent clocks** and can reject a valid answer after a wall-clock step backwards, or when the host clock lags the Collector's by more than a period; they fail closed (U6).

- **Every response:** 200, JSON, UTF-8, at most 2 MB (S§5.6). Code `bad_status` / `api_error` / `bad_content_type` / `too_large` / `bad_json`.
- **Echo** (`echo`): `venue`, `symbol`, `period`, `start`, `end` equal the request; `time_semantics == "period_end"`.
- **Types and ranges** (`schema`): the nine `coverage` fields; `O ≤ N`, both aligned and in the valid range; `A` in the valid range; the four `rule` fields non-negative integers.
- **Counts** (`counts`): `slots == 8640`; the counts sum to `slots`; `gap_free == (present_slots == slots)` (P7).
- **Runs, structure** (`runs`): state in the six non-present states; aligned, ascending, non-overlapping, inside the window; `slots == (end − start) / tf`; **touching runs (`prev.end == next.start`) have different states** (P7; two runs separated by a `present` slot may share a state); per state, the sum of run slots equals its count.
- **Runs, position** (`inconsistent_runs` for the first rule, `runs` for the others):
  - a run that contains the slot `O` or the slot `N` is `conflicted` (P1);
  - every slot before `O` lies in a run whose state is not `due`/`stale` (P2);
  - every slot after `N` lies in a run whose state is not `missing` (P3);
  - `due` and `stale` runs start after `N`; `missing` runs end at or before `N` (P4);
  - **order (P8):** with `conflicted` runs removed, the run states are non-decreasing in `no_longer_recoverable < missing < stale < due < not_yet_expected`;
  - `conflicted` runs are accepted at any position (P5).
- **Against the other answers:** `rule.cadence`, `rule.expected_after_ms`, `rule.lag_threshold_ms` equal `health.open_interest.rule` (`rule_mismatch`); `health.open_interest.rule.lag_threshold_ms == health.rule.lag_threshold_ms` (C§6.5.7, OA3); the clock checks of 8.3 (`clock_inconsistent`).
- **Not adopted, on purpose:** re-deriving each run's state from the answer's own `A` and `rule` (for example "`not_yet_expected` iff `start + expected_after_ms > A`"). It would close a few remaining false accepts, but it makes the page a second implementation of P-7 that can drift from the API, and a mislabelled but order-consistent answer is then an API bug the page cannot see. Also not checked: `retrieval_horizon_ms` against anything, and `N` against `/v1/instruments` (that value is the newest *candle*).
- **Reason codes:** only codes of the existing closed set (`INVALID_CODES`) are used; nothing is added to `model.py`.

### 8.6 The strip (mirror of S§5.7)

720 characters, bucket `k` = slots `[S' + 12k·tf, S' + 12(k+1)·tf)`. The alphabet of S§5.7 is reused for the shared states (`_`, `.`, `n`, `d`, `m`, `c`, `s`); `u` does not occur; **one character is added: `r`, no longer recoverable.** The worst state in a bucket wins, in the order `_ < . < r < n < d < m < c < s` (`r` outranks `.` so a permanent hole stays visible; it is below every state that is a problem). A bucket that ends at or before `O` starts as `_`; a `missing` or `no_longer_recoverable` run is clipped to start at `O` (Step A), so `r` marks only class U and slots after `N`. No per-slot loop over 8 640 slots × symbols. A symbol whose open interest is `unknown` or `no_data` has no strip and gets the hatch of S§5.7.

### 8.7 The overview object (additive to S§5.8)

Every existing key keeps its name, meaning and value (`refresh.deadline_hit` included, 8.2). Added: at the top level an open interest block with a status (`disabled`, `unknown`, or enabled with a **summary** of counts by state in the shape of the candle `summary`; under U2 option A with the key `unrecoverable`); in `refresh` the keys of 8.2; per symbol an open interest block with `state`, `reasons`, `oldest_time_ms`, `newest_time_ms`, `as_of_ms`, `retrieval_horizon_ms` and `strip`, or `null` when disabled. Exact keys: builder. The symbol's `state` stays the candle headline.

### 8.8 What the page shows (layout left to the builder)

- **Summary:** the candle tiles as today (candle-only counts); below them one labelled row of tiles for open interest. Disabled: one line "Open interest is not enabled in this deployment" instead. Unknown: a visible warning.
- **Problem list (user's decision 2, details mine, U8):** a symbol is listed when its candle state **or** its open interest state is a problem state. Columns: candle state, open interest state, symbol, reasons, each reason prefixed "candles" or "open interest". **Order:** by the worse of the two states (the rank of 8.4), then the candle state, then symbol. The "ok" group holds symbols where **both** are `ok` and is labelled "(candles and open interest)", so the candle tile `ok` can be larger than the group by design.
- **Heatmap:** two labelled strips per symbol on the same horizontal scale, candles above open interest, each with its own "history since" date, the open interest one labelled "first stored row". Tooltips name the hour and the state in words; colors never rely on hue alone. The legend gains `r`, and `_` for open interest reads "before the first stored row".
- **Partial refresh:** a visible line for each of `deadline_hit` and `oi_deadline_hit`, with the counts of symbols not asked (additive, U7).
- **Page text is English, as the page is today** (U3).

### 8.9 Regression guarantees

- **Candle results do not depend on open interest.** The candle phase is today's code path. A test with a slow fake API: the candle part of the overview is identical to a candle-only refresh against the same fake, whatever the open interest calls cost.
- **Open interest disabled** (`open_interest: null`): the existing keys and values of the overview are unchanged, the existing tests of the tool pass **without edits**, and the fake API counts **zero** open interest calls.
- **The API:** the existing tests of `/v1/candles`, `/v1/coverage`, `/v1/instruments`, `/v1/open-interest` and `/health/ingest` (including the existing golden `tests/api/golden/health_ingest_s17.json`) pass without edits. OI-6a adds no new golden for them.
- **Open interest enabled — the intended changes to existing page elements** (user's decision 2): the problem list and the "ok" group take open interest into account, and the problem table gains columns (8.8). Nothing else of the candle elements moves, changes meaning or is restyled; the candle headline, candle summary tiles, candle reasons, candle strip and candle columns are unchanged in every case, including every open interest failure.
- OI-6a ships without OI-6b; OI-6b is not written before G1 (section 10).
- No new configuration key, no migration; the harness and its Makefile target are tooling, not product configuration.

## 9. Tests (CLAUDE.md: pytest, mocked, never production data)

**OI-6a (pure functions, reader, endpoint)**
- Runs: only present slots → no runs; every state in one window; **touching** runs of different states do not merge; **two runs of one state separated by a `present` slot stay two runs**; first and last slot; a 10 000-slot window alternating present and `missing` gives 5 000 runs, one alternating two non-present states gives 10 000 runs, and the body is under 1 MB in both (built with the API's serializer).
- P1 to P8 as property-style tests over generated windows: for random `stored`, `conflicts`, `A`, `N`, `O` and rule, every produced answer satisfies P1 to P8 (this is what supports "the structural checks cannot reject an API answer").
- Counts equal the per-state sums of runs; counts equal `/v1/open-interest`'s `coverage` counts for the same one-page window and the same `A`.
- **Conflict without a row** (V7): a conflict at `T < O`, at `O < T < N`, and at `T > N`, with no row at `T`, all give `conflicted` and are counted; `O` and `N` do not change.
- Boundaries: unaligned, `end ≤ start`, 10 001 slots, an extra or missing parameter → `400`; disabled → `404 open_interest_disabled` without opening the database; unknown series → `404`; a database before migration 2 → `503`; a bad host clock → `500`; an unaligned stored `time_ms` → `500`; the order of checks.
- Read-only, one snapshot, no value column read (the traced-statement pattern of INV-092), off the event loop. The existing endpoint tests pass unedited (8.9).
- Benchmark harness: percentile, window, call-order and writer-shape tests (7.3).

**OI-6b (page, fake API as in `tests/tools/symbol_overview/`)**
- **`O` against the window** (section 5): `O < S'` (`S' − tf`, far before): no pre-history, no clipping; `O == S'`; `O == S' + tf` (one pre-history slot); `O` in the middle; `O == E` (the last slot; every earlier slot is in runs); `O ≥ E'` (and `N ≥ E'`) → `unknown (clock_inconsistent)`, never an `ok` strip of `_`.
- **Pre-history and unrecoverable:** a `missing` run ending exactly at `O` is excluded and clipped; a `missing` run `[O − tf, O + tf)` → `unknown (inconsistent_runs)`; a `no_longer_recoverable` run ending at `O` is `_`; one starting at `O + tf` is class U → reason `unrecoverable`, strip `r`; `no_longer_recoverable` after `N` is not a reason; a `not_yet_expected` run before `O` is accepted and is not pre-history; the headline of a series whose only finding is class U follows the decision of U2 (A: `unrecoverable`; C: `ok` with the note); `missing` and class U together give headline `holes` and both reasons.
- **Conflict without a row** before `O`, between `O` and `N`, and after `N`: accepted, reported as `conflict`, never excluded as pre-history.
- **Contradictions rejected (each `unknown` with the stated code, never `ok`):** a non-`conflicted` run containing `O` or `N` (`inconsistent_runs`); a `due`/`stale` run starting at or before `N`; a `missing` run ending after `N`; a slot before `O` or after `N` not in any run; a `due` run before `O`; touching runs of one state; **order violations: `[not_yet_expected][missing]`, `[due][stale]` after `N`, `[missing][no_longer_recoverable]`** (`runs`); counts that do not sum; per-state run sums that differ; a run outside the window, unaligned or unordered; wrong echo; `rule` different from `health.open_interest.rule`; `health.open_interest.as_of_ms != T0`; `A < T0`; `A > T0 + 70 s`.
- **No false rejection:** two runs of one state separated by a present slot are accepted; an answer with `A == T0 + 70 s` exactly is accepted; every answer generated by the API's pure function (the same generated windows as OI-6a, as test data) passes the structural checks.
- Reasons and headline: stale + conflict + holes together; `due` alone is `behind`; `not_yet_expected` alone is `ok`; each symbol counted once; the candle `state` never changes because of open interest.
- **Failures and the deadline:** every status and transport failure of the open interest call affects only that part; a failed candle call does not skip the open interest call; **candles run first: with a slow fake API (fake clock) the candle part is identical to a candle-only refresh and the unreached open interest symbols are `unknown (deadline)`**; no call starts after the deadline in either phase; `deadline_hit` stays candle-only, `oi_deadline_hit` and the not-asked counts are set; a partial snapshot is 200 and cached 15 s; no previous snapshot is served in its place.
- **Health variants:** `null` → zero open interest calls and the disabled sentence; a malformed object or a missing key → the whole open interest part `unknown`, the candle part intact; a page given an overview without the open interest block shows the "not reported" line.
- **Bounds:** never more than 4 calls in flight; only the four allowed paths; strip length 720; bucket `k` of both strips is the same hour for the same data; the largest feasible body decodes under the cap; **structurally, the worker's open interest result holds no runs** (a type and size assertion; there is no memory-threshold test).
- Manual check, documented in the README: the tool against a real API on a test database with a symbol with a partial open interest history, one with a hole, one with an unrecoverable hole, one never stored, and one with open interest disabled. `make check` passes with the standard-library AST test.

## 10. Slices and gates

| Step | Content | Gate |
|---|---|---|
| OI-6a, 1 | the benchmark harness, **G0** run against `/v1/coverage` as a proxy | numbers shown with the contract text |
| OI-6a, 2 | contract section 6.6 and the edits of 6.5, ADR-3 amendment A-2, README, invariants rows | **the user approves the texts** before the endpoint is implemented |
| OI-6a, 3 | the endpoint and its tests | merged |
| **G1** | the benchmark with the real endpoint at `N = 300` and `N = 600`, numbers recorded here, AC1 to AC7 evaluated, the rule of 7.4 applied | **the user reads the numbers and approves the implementation of OI-6b; no OI-6b plan before that** |
| OI-6b | the page changes, README entry, tests | OI-6a merged and G1 passed |
| **G2** | the same criteria and AC8 re-measured with the real refresh | a failure stops the merge (7.3) |

Each slice: failing tests first, a plan reviewed independently, the implementation reviewed independently, all findings applied, then a pull request, squash-merged. No release tag until the user says to deploy. ADR-3 P-14's outage rules are untouched: there is no migration.

## 11. Risks and accepted limits

- **The target scale is unproven.** At 600 symbols the refresh needs a mean call latency under 198 ms to finish at all and under 149 ms to meet AC1 (7.1); today's candle-only refresh has the same unproven status at 394 ms. G0 and G1 exist to replace this paragraph by numbers. Open interest may be `unknown (deadline)` for many symbols while the candle part is complete (8.1).
- **The tunnel and the per-call connection are the likely first-order costs** (V11, V13) and are invisible to a loopback benchmark; AC7 needs one measurement from the user, and the G1 verdict rests on it.
- **The benchmark is not production.** One machine, a generated database, a modelled writer; the VPS is slower and the user does not benchmark it (ADR-3 A-1). AC1's 25 % headroom and AC7 are the mitigation, not a proof.
- **The start of collection is unknown** (V9). Step A is a rule about stored rows; it can hide a catch-up that did not reach back. The page says "before the first stored row".
- **Open interest `no_data` has no cause** (like candles, S§5.5 L1).
- **No `stored_ahead` field for open interest** (V2). The page's `N ≥ E'` rule catches the gross case only; a host clock behind by less than a period can still delay `stale`. The clock checks can also reject a valid answer after a wall-clock step back (U6).
- **A hole is not always a fault.** Binance omits periods without trades; a quiet symbol can show `holes` and later `unrecoverable` (the text says so).
- **Version skew.** Before OI-6a is deployed, every open interest call answers `404`; the page shows `unknown` for open interest and keeps the candle part.
- **One more public endpoint** to keep stable once approved (6.1), and edits to approved contract text (6.5).
- An open interest series of a symbol that is **not configured** (retired) is not listed by `/v1/instruments` (INV-067, V17) and is not shown; configured symbols are always asked.
- The per-call timeout is per socket operation, not total (V11): the worst refresh is the deadline plus the slowest in-flight call; a total bound per call is not part of this design.
- The page depends on `/v1/instruments`, whose contract section is still marked PROPOSED (V17); out of scope.

## 12. Left to the builder, and what only the user can decide

**Left to the builder:** names and locations of the pure functions, the reader and the harness; the exact keys of the open interest part of the overview object; colors and layout within 8.8; test organisation; the plans.

**For the user** (answered by approving this document or by changing a line; **U2 and U5 need an explicit answer**):

| ID | Question | Proposal |
|---|---|---|
| U1 | The endpoint: a new `GET /v1/open-interest/coverage` (6.1), or `runs` added to `/v1/open-interest`? | the new endpoint |
| **U2** | **Product behavior:** how are unrecoverable holes **inside the stored history** (class U) shown? They exist only in about the oldest day (≈ 288 of 8 640 slots) of the 30-day window, after 29 days as `holes` (8.4). **A:** a reason plus its own, least severe headline state, a tile and a place in the problem list. **B:** strip only, no reason. **C:** a reason as an information line, strip `r`, no headline state, no tile. | **C — decided by the user on 2026-11-03** (changed from A in revision 3: the band is one day) |
| U3 | A page text in English (as built) or Czech (as the old design says)? | English |
| U4 | Issue tracker: split #6 into OI-6a and OI-6b, or comment on #6? I change nothing on GitHub without asking. | you decide |
| **U5** | The numbers of the acceptance criteria (AC1 ≤ 45 s, AC4 p99 ≤ 1 s, AC5 ≤ 432 MiB, AC8 ≤ 256 MiB, "≥ 10 refreshes, ≥ 10 minutes", 7.2), and the extra gate G0 (a proxy run before the contract text is approved, 7.3). | as written |
| U6 | The page's clock checks (`T0 ≤ A ≤ T0 + 70 s`, `health.open_interest.as_of_ms == T0`, `N < E'`, 8.3) assume consistent clocks and may reject a valid answer after a clock step or a lagging host clock. Open interest has no `stored_ahead` field to flag it as candles do. | keep |
| U7 | The additive "refresh incomplete" lines when a deadline is hit (8.8). | keep |
| U8 | The problem list and "ok" group take open interest into account (your decision 2); the sort key (worse of the two, then candle state, then symbol) and the group label are my details (8.8). | as written |

The contract text (6.6 and the edits of 6.5) is approved separately, as text, before OI-6a is implemented (V16).

## 13. What changed

**Revision 2** (the user's targeted review):
1. Performance (section 7): the arithmetic for N = 100, 300, 600; the connection-per-call and the SSH tunnel named as likely dominant costs; criteria AC1 to AC7; a mandatory benchmark at 300 and 600 as gate G1 before OI-6b is approved; a decision rule for "per-symbol or batch" that adds no batch endpoint automatically.
2. Pre-history (section 5): `O`, `N`, runs and counts defined formally; the positions of `O` against the window; validation rewritten from the properties; conflicts without a row analysed.
3. No longer recoverable (8.4): Π, H and U separated; a reason `unrecoverable` proposed and marked as needing approval; the unknown start of collection stated as a limit and not estimated.
4. Snapshot consistency (8.3), failure handling (8.2), body size and regression guarantees (8.9) written.

**Revision 3** (the independent review of revision 2; every finding was checked against the code or the contract before it was applied):
1. **Blocker fixed:** open interest calls shared the candle budget and could turn candle states into `unknown (deadline)`. The refresh now runs a candle phase first, as today, then an open interest phase under the same single deadline (8.1, 8.2, 7.1, 8.9, tests).
2. `refresh.deadline_hit` keeps its candle-only meaning; open interest gets its own flag and counts (8.2, 8.7, 8.8).
3. "Adjacent runs have different states" now says **touching** runs; the OI-6a test of 5 000 alternating runs no longer contradicts it (P7, 8.5, 9).
4. A no-clock **order rule (P8)** closes false accepts such as `[not_yet_expected][missing]`; the wording of P6 corrected; the optional re-derivation from `A` and `rule` considered and not adopted, with the reason (8.5).
5. The claim "no answer the API can produce is rejected" narrowed: structural checks cannot reject; clock checks assume consistent clocks (8.3, 8.5, U6).
6. The list of changed documents completed: the sentence in C§6.5.6 that says there is no separate coverage endpoint, the error table, the reserved-endpoint table, the traceability table, README, ADR-3 P-11; the `/v1/instruments` PROPOSED status recorded as a fact (6.5, V17).
7. The golden-body guarantee reworded to what exists (one golden); "byte for byte" replaced by "existing keys and values unchanged" (8.9).
8. The change to existing candle UI elements (problem list, "ok" group, columns) stated as the intended consequence of decision 2, with a defined sort and label (8.8, 8.9, U8).
9. The false statement about symbols not listed by `/v1/instruments` corrected (INV-067, V17, section 11).
10. Benchmark made measurable: a concurrent writer in AC4, "fresh refresh" defined, per-call latency defined, RSS measured with the existing `wait4` pattern, a tool memory threshold (AC8), the memory test replaced by a structural one, a proxy run G0 before the contract text is frozen, the harness importing the tool's window code, a rule for a failed G2 (7.2, 7.3, 9).
11. The one-day band of class U (≈ 288 slots) stated for the user; my recommendation for U2 changed from A to C (8.4, U2).
12. V9 corrected: the catch-up depth is the fixed 29-day horizon, not configuration.
13. "Required for correctness" softened; the per-call timeout caveat stated (8.2).
14. The table rounded down to values at which `D` does not exceed the limit (1 153, 394, 198 ms); the head calls noted as unequal to `L` (7.1).
15. Body sizes given as rounded upper bounds, with the review's recomputation (V14).
16. Wording: six non-present counts plus `present_slots`; eight integers plus `gap_free` (P7, 6.3).
17. The reason code of each new check named from the existing closed set; no new code (8.5).
18. Where open interest is judged (in the worker) stated (8.2).

## 14. Measurements

### G0 — the refresh against `/v1/coverage` as a stand-in (2026-11-03)

**What was run:** `make bench-overview SERIES=<N> OVERVIEW_PROFILE=target OI_MODE=proxy` for `N = 300` and `N = 600`, one after the other. `proxy` means the second call of every symbol is `GET /v1/coverage` with the candle window instead of `GET /v1/open-interest/coverage`, which does not exist yet; **the assumption that the two cost about the same is not verified** (same cost class: a primary-key range read of at most 10 000 slots; the open interest endpoint reads two tables). G1 replaces it with the real endpoint.

**Setup:** Apple M5, 10 cores, 32 GiB, internal SSD (APFS), macOS 27.0.1 arm64, Python 3.12, SQLite 3.53.1, loopback only (no SSH tunnel). A generated database in a fresh temp dir: `N` candle series of 100 000 rows (the `bench-health` depth) and `N` open interest series of 8 640 rows (every 7th series with a few holes and one conflict record, one series with three days of open interest); `quasar-api serve` with open interest enabled, budget 400, so the cadence is `c = 3` at 300 symbols and `c = 5` at 600. The operating system's file cache is warm after the build; nothing was dropped. Tool constants unchanged: 4 workers, a new connection per call, deadline 60 s. 10 fresh refreshes (the first is not discarded), then a sustained run of 10 minutes: a refresh, a 15 s pause, a refresh, … with a `/health/ready` probe every 0.5 s and a concurrent writer inserting one candle and one open interest row per symbol every 300 s (two ticks in the run). The raw JSON lines are not committed; the tables below are the record.

| | N = 300 | N = 600 |
|---|---|---|
| refresh wall time, 10 refreshes (AC1, limit 45 s) | 12.6 to 16.6 s | 19.7 to 21.4 s |
| failed calls, symbols not asked (AC2) | 0, 0 | 0, 0 |
| candle call: p50 / p95 / p99 / max / mean | 93 / 118 / 129 / 186 / 94 ms | 66 / 78 / 85 / 108 / 67 ms |
| second call (proxy): p50 / p95 / p99 / max / mean | 89 / 122 / 132 / 145 / 92 ms | 66 / 78 / 86 / 115 / 67 ms |
| `/health/ingest` mean, `/v1/instruments` mean (10 calls) | 8.1 ms, 3.2 ms | 12.0 ms, 4.0 ms |
| largest body (AC6, limit 1 MB), decode time | 32 117 bytes, 0.2 ms | 64 217 bytes, 0.3 ms |
| sustained: refreshes, refresh time, calls failed or not asked | 25, 8.3 to 16.0 s, 0 | 19, 16.9 to 20.0 s, 0 |
| `/health/ready` p99 during the sustained run, `503` count (AC4) | 12.0 ms, 0 | 11.0 ms, 0 |
| API peak resident set (AC5, limit 432 MiB) | 77.9 MiB | 76.9 MiB |
| writer: rows attempted, errors | 1 200, 0 | 2 400, 0 |

**Evaluation:** AC1, AC2, AC4, AC5 and AC6 pass at both sizes; AC3 is diagnostic; **AC7 is open** until the user supplies `L_tunnel`.

**What this shows and does not show**
- The paper model of 7.1 agrees with the measurement: `(2 + ⌈N/4⌉·2) · L` gives 152 × 92.7 ms ≈ 14.1 s at 300 and 302 × 67 ms ≈ 20.2 s at 600, against 12.6 to 16.6 s and 19.7 to 21.4 s measured. At 600 the longest refresh (21.4 s) is 2.1 times under the 45 s criterion on loopback.
- **The per-call mean was lower at 600 (67 ms) than at 300 (93 ms).** I have no explanation. The 300 run overlapped with light work on the same machine (editing documents and one test run of about 2 s); I do not claim that explains a 40 % difference. The latency is therefore **not** safe to extrapolate from `N`; G1 repeats both sizes with the real endpoint.
- **The tunnel is the open term.** With the measured means, the criterion `D_pred ≤ 45 s` allows an extra latency per call of at most about 82 ms at 600 symbols (149.0 − 67.0) and about 203 ms at 300 (296.1 − 92.7). A real SSH tunnel with a new connection per call may or may not fit; only `L_tunnel` answers that.
- One machine, a warm file cache, a modelled writer with two bursts in ten minutes: contention is sampled, not stress-tested; the VPS is slower and is not measured (ADR-0003 A-1). The stress profile (every answer with the largest number of runs) was not run at G0; it needs the real endpoint.
- **G0 is informative and gates nothing.** Nothing in it reopens 6.1 (a per-symbol endpoint) or 7.4.
