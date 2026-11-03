# ADR-0003: Open interest (Binance USDT-M, 5m)

- **Status:** Accepted (approved by the user on 2026-11-02: revision 4 as written, including the proposals for the
  open decisions O-1 to O-5). Revisions 2 to 4 answer the user's reviews (listed at the end). The API response
  schema (P-11) is contract section 6.5 of `docs/contracts/api-v1.md`, approved by the user on 2026-11-03 and implemented by OI-5. Amendment A-1 (2026-11-02, approved by
  the user): the 100-row transaction criterion of P-9 is checked from production measurements after OI-4, not by
  a benchmark in OI-2 (see the revision history). Amendment A-2 (2026-11-03, approved by the user with contract section 6.6): a per-run gap listing for open interest and the split
  of the OI-6 slice (see the revision history).
- **Date:** 2026-11-02
- **Scope:** collecting, storing and serving Binance USDT-M open interest history at a 5m period for the
  configured symbols. It is the first extension dataset (`docs/architecture.md` §7); funding, liquidations and
  order books stay out of scope. The candle (OHLCV) contracts, tables, schedule and guarantees are not changed.
- **Related:** ADR-0001 (time trust P-4, decimal grammar P-7, venue and symbol P-8), ADR-0002 (schema governance
  P-9, immutability P-4, conflicts P-5, migration P-8), `docs/contracts/api-v1.md`, `docs/follow-ups.md` F-8
  (no 418/429 handling today), spec `2026-10-20-market-data-v1-design.md` §3.

## How to read this ADR

Every factual statement carries its source:

- **[M]** measured in this project's probe (Part 3, E-n), with its sample size;
- **[D]** documented by Binance (Part 3, E-1); not verified by us unless also [M];
- **[C]** a property of this repository's current code, checked on 2026-11-02 (file named);
- **[P]** a design proposal or assumption. A [P] is never evidence.

Part 1 is the request. Part 2 holds the proposals that need approval. Part 3 is the evidence.

## Context

The user asked on 2026-11-02 to start collecting open interest alongside the candles. The architecture names open
interest as an extension dataset that "needs its own identity, timestamp, units, provenance and quality semantics"
and says a failure in it "must not compromise basic OHLCV collection" (§7). Adding it needs new tables (ADR-0002
P-9) and a new read endpoint (API contract: every endpoint needs its own approved section).

Binance USDT-M offers two public open interest endpoints [D]:

| Endpoint | CCXT raw method | What it returns |
|---|---|---|
| `GET /fapi/v1/openInterest` | `fapiPublicGetOpenInterest` | the current value only, with a millisecond `time`; no history |
| `GET /futures/data/openInterestHist` | `fapiDataGetOpenInterestHist` | a history at a fixed `period` (5m … 1d), "latest 1 month" |

## Part 1 — Request

| ID | Request |
|---|---|
| R-A | Collect open interest for the symbols the Collector already collects (user, 2026-11-02). |
| R-B | The user asked to proceed without a requirements interview. Every choice below is a proposal. |
| R-C | Review of revision 1 (user, 2026-11-02): resource isolation, restart and catch-up, rate budget, conflict integrity, coverage states, deployment, tests, timing. Constraints: no new abstractions without a demonstrated need; raw decimal text, fail closed, immutable storage and a separate dataset are kept; slices OI-1 to OI-6 are kept; OHLCV contracts are not changed. |

## Part 2 — Proposals

### P-1 Source: the history endpoint, raw, period 5m

Use `GET /futures/data/openInterestHist` through `fapiDataGetOpenInterestHist` with `period=5m`. Values stay the
strings Binance sends [M E-2]; the unified `fetch_open_interest_history` is not used (it returns floats).

*Rejected:* sampling `/fapi/v1/openInterest` ourselves. It would be our own dataset (the value at the instant our
request arrived), it cannot be backfilled or rechecked, and an outage would leave holes nobody can fill.

### P-2 Fields and units

| Raw field | Stored as | Meaning [D E-1] | Unit |
|---|---|---|---|
| `timestamp` | `time_ms` | "End time of the period, in milliseconds." | UTC ms |
| `sumOpenInterest` | `open_interest` | "total open interest" | base asset (e.g. BTC), inferred [M E-3] |
| `sumOpenInterestValue` | `open_interest_value` | "total open interest value" | USDT, inferred [M E-3] |
| `CMCCirculatingSupply` | not stored | "circulating supply provided by CMC" | — |
| `symbol` | checked, not stored | market id | — |

- Both values are stored as canonical decimal text (ADR-0001 P-7: trailing fractional zeros removed, no
  rounding) and must be `>= 0`. `open_interest_value` is Binance's figure and is **never computed** by us.
- `CMCCirculatingSupply` is third-party data (CoinMarketCap), not market data of this venue.
- *Limitation:* Binance does not state the units. They are inferred from one ratio check [M E-3], like ADR-0001 P-2.

### P-3 Identity and time semantics

Identity is `(venue, symbol, period, time_ms)`; `period` is `5m`; `time_ms` is aligned to the period [M E-2].

- `time_ms` is the **end** of the period [D E-1]; a candle is keyed by its **open** time. The row with
  `time_ms = T` belongs to the period `[T − 5m, T)`, i.e. to the candle with `open_time_ms = T − 5m`. The column is
  named `time_ms`, not `open_time_ms`, so nobody joins the two by equal timestamps by accident.
- Whether the value is the open interest at the instant `T` or a statistic over the period is not documented.
  We store and serve "the value Binance reports for the period ending at `T`" and claim nothing more.

### P-4 Eligibility: when a row may be stored

A row with end time `T` is eligible iff `T + publication_delay_ms ≤ server_lower_bound` (ADR-0001 P-4 bound and
its time-trust gates). With untrusted time nothing new is stored. A row returned before it is eligible is dropped.

- `publication_delay_ms`, proposed **15 000** [P]. Observed: no row was returned before its period ended and no row
  changed after it first appeared [M E-5, E-6, 18 + 303 rows]. The margin guards against a row offered early or
  revised in its first seconds. It is a margin, **not a proven bound**.
- Eligibility is separate from **when to ask** (P-10). A row that is eligible but not yet published is simply
  absent from the response; it is never inferred.

### P-5 Request rules (the endpoint differs from klines)

Request behavior [M E-4]:

1. When a range holds more rows than `limit`, Binance returns the **newest** `limit` rows of the range (klines
   return the oldest). Paging forward with `startTime` and `limit` alone would silently skip rows.
2. A `startTime` inside a period also returns that period's row, whose `timestamp` is before `startTime`.
3. A `startTime` 31, 35 or 40 days back is rejected with `-1130 parameter 'startTime' is invalid`; 29 and 30 days
   back were accepted. The documented retention is "latest 1 month" [D].
4. `endTime` is inclusive: an aligned `[startTime, endTime]` returned `(endTime − startTime) / 5m + 1` rows.
5. `limit` 501 and 600 were accepted although the documented maximum is 500 [D]; we never rely on that.

Rules [P]:

- Every request sends both `startTime` and `endTime`, aligned to the period, covering **at most `limit ≤ 500`
  period ends including both bounds**. Truncation therefore cannot happen even if Binance changed its rule.
- The whole response is a parse error (fail closed, nothing of it stored) when: a row lies outside
  `[startTime, endTime]`; a `timestamp` is unaligned, duplicated or not an int; a `symbol` differs from the request;
  a value is not a string in the decimal grammar; there are more rows than period ends in the range; the response
  is not a list. Missing rows are **not** an error (P-7).
- No request uses a `startTime` older than the **retrieval horizon** `H = 29 days` before the current server lower
  bound [P: one day of margin under the observed 30/31-day edge, which may itself vary].

### P-6 Schema version 2

One new migration, `002_open_interest.sql`, following ADR-0002 exactly (STRICT, natural key, `WITHOUT ROWID`,
immutability triggers, conflicts table, the same `<decimal-shape>` check):

```sql
CREATE TABLE open_interest (
  venue               TEXT    NOT NULL CHECK (length(venue) BETWEEN 1 AND 32),
  symbol              TEXT    NOT NULL CHECK (length(symbol) BETWEEN 1 AND 64),
  period              TEXT    NOT NULL CHECK (length(period) BETWEEN 2 AND 3),
  time_ms             INTEGER NOT NULL CHECK (time_ms BETWEEN 946684800000 AND 4102444800000),
  open_interest       TEXT    NOT NULL CHECK (<decimal-shape>),
  open_interest_value TEXT    NOT NULL CHECK (<decimal-shape>),
  ingested_at_ms      INTEGER NOT NULL,
  PRIMARY KEY (venue, symbol, period, time_ms)
) STRICT, WITHOUT ROWID;
-- + open_interest_no_update / open_interest_no_delete triggers (ADR-0002 P-4)

CREATE TABLE open_interest_conflicts (
  conflict_id         INTEGER PRIMARY KEY,
  venue               TEXT    NOT NULL,
  symbol              TEXT    NOT NULL,
  period              TEXT    NOT NULL,
  time_ms             INTEGER NOT NULL,
  open_interest       TEXT    NOT NULL CHECK (<decimal-shape>),
  open_interest_value TEXT    NOT NULL CHECK (<decimal-shape>),
  first_seen_at_ms    INTEGER NOT NULL
) STRICT;
CREATE UNIQUE INDEX open_interest_conflicts_observation ON open_interest_conflicts
  (venue, symbol, period, time_ms, open_interest, open_interest_value);
CREATE INDEX open_interest_conflicts_slot ON open_interest_conflicts (venue, symbol, period, time_ms);
-- + open_interest_conflicts_no_update / _no_delete triggers
```

- No table for "exchange has no data": nothing can be confirmed as such (P-7).
- The v1 tables are not touched; the migration is additive. Size estimate (not measured): 70–90 bytes per row.

### P-7 Coverage states

For one open interest slot `T` at an assessment time `as_of`, the first matching state wins. The table says, for
each state, what we actually know; none of them claims a cause we cannot observe.

| # | State | Condition | What it says | What it does **not** say |
|---|---|---|---|---|
| 1 | `conflicted` | one or more `open_interest_conflicts` rows for `T` | Binance sent two different values for `T` (P-8). | which value is right |
| 2 | `present` | an `open_interest` row for `T` | we stored a value Binance reported for `T` | that it will never change |
| 3 | `not_yet_expected` | `T + expected_after_ms > as_of` | by our rule (P-10) Binance is not yet expected to have published `T` | that Binance has not published it: we did not look |
| 4 | `no_longer_recoverable` | `T < as_of − H` and no row | the slot is older than our retrieval horizon (P-5), so we will never request it | that the data existed, or that Binance would refuse it (the edge is at ~30 days [M], `H` is 29 [P]) |
| 5 | `missing` | otherwise | we hold no row and could still request it | why: Binance documents that periods without trades are omitted [D], and a row can also be late or lost. We cannot tell these apart from a response. |

Notes:

- `missing` is split like the candle coverage at the series' newest edge: a missing slot after the newest stored row
  is `due` until `T + expected_after_ms + lag_threshold_ms`, then `stale` (existing `ExpectationRule` semantics [C
  `domain/timed_coverage.py`]). Before the newest stored row it is plain `missing`.
- The review asked for `not-yet-published`. This ADR uses **`not_yet_expected`** instead because the state is a rule
  about expectations, not an observation of Binance; it is also the name the candle coverage uses for the same idea.
  Open decision O-3.
- A slot moves from `missing` to `no_longer_recoverable` only by the passage of time. It is never re-labelled
  `present` by inference.
- *Not proposed:* proving "no trades" from a stored candle with `volume = 0` for the same period. It would let a
  `missing` slot become an explained `unavailable` one; it needs evidence that Binance omits exactly those periods
  and is a later ADR amendment if wanted.

### P-8 Integrity: conflicts and their effect on health and the API

A re-delivered row that differs from the stored one is rejected and recorded in `open_interest_conflicts`
(`INSERT … ON CONFLICT DO NOTHING`); the stored row is never updated (immutability triggers). No resolution path in
v1, as for candles (ADR-0002 Q3). Observed revisions so far: none [M E-5, E-6].

Effect of a conflict [P]:

- **Collector log:** one line per newly recorded conflict with venue, symbol, `time_ms`, both value pairs, and a
  per-round count of conflicts (new or repeated). The same wrong value seen again adds no row and no log line.
- **API `/v1/open-interest`:** a row in a conflicted slot is returned with its **first stored** values and
  `conflicted: true`; the page's coverage counts `conflicted_slots` and its gap-free flag is false. Consumers must
  treat a conflicted row as untrusted (same wording as the candle contract §1 point 5).
- **API `/health/ingest`:** the `open_interest` object (P-11) carries `conflicted_series`, the number of series with
  at least one conflict whose slot is inside the last 24 hours, and the `open_interest.status` is `degraded` when it
  is not 0. [P: the candle part of `/health/ingest` does not count conflicts today [C `api/health_reader.py`];
  that is not changed here.]
- Nothing in the open interest part changes the existing top-level fields of `/health/ingest`, so candle health
  keeps its meaning.

### P-9 Isolation from candle collection

The goal: enabling open interest must not make a candle round later, or make a candle fail. Two kinds of
isolation are kept apart.

**Task isolation** (what happens on failure):

- Open interest runs as its own asyncio task in the Collector process, started **after** the candle loop's first
  round and its post-check have finished, so the candle restart catch-up runs undisturbed. Candle startup never
  waits for any open interest work.
- Any exception in the open interest task, including one it considers fatal, stops **only** that task. It logs the
  error and exits; it is not restarted inside the process (a restart of the process restarts it). The candle loop is
  not cancelled or informed.
- The reverse does not hold: a candle fatal error ends the Collector process [C `collection_loop.py`,
  `FatalLoopError`] and therefore open interest too. Restart reconcile (P-12) recovers both.
- The API cannot see the Collector's in-process state. A stopped open interest task becomes visible **through the
  data**: its series go `due` then `stale` in `/health/ingest` (P-11), plus the Collector log.

**Resource isolation** (what open interest may take from candles):

| Shared resource | Rule [P] | Why |
|---|---|---|
| CCXT client | Open interest uses **its own** `binanceusdm` client instance (own `load_markets`, own throttle queue). | CCXT throttles per client instance; a shared client would put open interest calls in the same queue as klines [C `binance_usdm.py` creates one client per source]. |
| Request limiter | Open interest never acquires the candle `RateLimiter`; it has its own budget (P-10). | The candle limiter is one FIFO lock for all symbols [C `rate_limiter.py`]; waiting in it would delay klines. |
| Server time | Open interest takes its own `fapiPublicGetTime` observations through its own client and never calls the candle loop's `SharedServerTime`. At most one per open interest round, plus at most one per backfill slice. | `SharedServerTime` serialises refreshes behind a lock [C `shared_server_time.py`]. `/fapi/v1/time` costs weight 1 [D, not re-read here] in the IP weight budget shared with klines; ≤ 2 per 5 minutes is negligible but is counted (P-10). |
| Concurrency | At most **2** open interest requests in flight (candles: `max_concurrency`, 4 today). | Bounds sockets and event-loop work. |
| Database writes | Its own writer connection on its own single worker thread, separate from `AsyncCandleWriter` [C `async_candle_writer.py`], under the contention rules below. The candle writer keeps its settings (`busy_timeout` 5 000 ms [C `storage/connection.py`]). | SQLite has one writer lock. Two writer connections in one process stay under the single-logical-writer rule (architecture §4.1: the Collector process is the writer). |
| Database reads | Restart reconcile and hole reads for open interest run on the open interest thread, bounded to the retrieval horizon (≤ 8 352 slots per series), never on the candle writer's thread. | WAL readers do not block the writer; the cost stays off the candle path. |
| Time window | The open interest round wakes at `B + 200 s` (P-10); the candle round wakes at `B + 15 s`. The candle round's work therefore never overlaps an open interest round by schedule. The candle backfill (idle window) can overlap; it is already lower priority. | Temporal separation instead of a priority scheduler (no new abstraction). |

**Database contention: open interest yields to candles** [P]. Three rules: small bounded transactions, yield while
the candle writer is active, measure. No watchdog and no `Connection.interrupt()`: a running transaction is never
cut short, and no bound is claimed for how long a `COMMIT` takes.

1. **Small transactions.** One open interest transaction writes at most **100 rows** (a round's rows for one
   symbol are at most 25; a 500-row backfill page is written as up to 5 transactions). Each transaction is atomic
   and idempotent, so a page interrupted between its transactions is completed by the next try.
2. **Yield to the candle writer.** A counter `candle_write_active`, incremented and decremented around the existing
   `_WriteWatch.write` [C `collection_loop.py`], is > 0 while a candle write (round or candle backfill) is queued or
   running. The candle path only updates the counter and never waits for open interest. The open interest writer
   checks it immediately before each `BEGIN` and waits while it is > 0. The fetched, validated rows wait in memory
   (at most one round's or one page's rows). If the round deadline (P-10) passes first, they are dropped unwritten
   and logged; the overlapping range or the backfill fetches them again.
3. **Busy is not an error for candles.** The open interest connection uses `busy_timeout` **1 000 ms**; an
   `SQLITE_BUSY` makes that transaction fail and be retried at the next opportunity (idempotent). The candle
   connection keeps its settings.
4. **Reads.** Open interest reads (restart reconcile, hole derivation) run one series at a time, each in its own
   short read transaction, never held across an `await`.
5. **Measure.** Logged per hour: the number and total time of yields, open interest transaction duration
   (count, p50, p99, max), `SQLITE_BUSY` count on the open interest connection, and candle write duration
   (p50, p99, max) measured around the same `_WriteWatch.write` call.

What this bounds, and what it does not: the check in rule 2 and the start of a candle write can race, so a candle
write can still wait for **at most one** open interest transaction of at most 100 rows that has already begun. How
long that transaction takes, `COMMIT` sync included (`synchronous=FULL`), is **not bounded by this design**; it is
measured. The acceptance criterion is p99 of a 100-row open interest transaction ≤ **50 ms** on the target disk,
taken from the Collector's hourly measurement line (rule 5) during the soak after OI-4 is deployed (amendment A-1;
until OI-4 nothing writes open interest). If it fails, the 100-row cap is lowered; the rules stay. OI-2 ships a
standard-library benchmark for a local or optional check.

**Acceptance criterion** for OI-4 [P]: in a test with a fake slow and failing open interest source, the candle
round's `lateness_ms` and outcomes are the same with open interest enabled and disabled. In the soak test, the
candle `lateness_ms` p99 with open interest enabled must not exceed the value measured without it by more than
1 000 ms.

### P-10 Request budget, rate limiting and timing

**What is known.** The page states "IP rate limit 1000 requests/5min" and "IP Weight 0" [D]. It does not say
whether the 1 000 are shared by all `/futures/data/*` endpoints, whether the window is rolling or fixed, or whether
responses carry a usage header. We assume the worst: shared by every `/futures/data/*` caller on the IP, rolling
window, no usage header [P].

**Budget.** The open interest limiter is a sliding-window counter of request **starts** over the last 300 s,
with a ceiling of `b = budget_per_5min` from the configuration (P-13) [P]. The same limiter covers open interest's
`/fapi/v1/time` calls. Allowed values: **`100 ≤ b ≤ 400`** (400 is 40 % of the documented 1 000). Any other
value is a config error at start. (The floor of 100 is a proposal: it keeps today's 10 symbols at `c = 1` and the
backfill share at 25 or more per 5 min.)

**Cadence `c`: each symbol is asked every `c` periods, not every period** [P]. Stored data stays at 5m granularity:
one request returns every 5m row of its range, and the range is far below the 500-row limit (below). `c` is not
configured; it is derived from the number of configured symbols `S` **and the configured budget `b`** by one pure
domain function, so the Collector and the API (which reads the same config file, P-7) get the same value:

`steady(b) = floor(3b / 4)`;
`c(S, b)` = the smallest integer `c ≥ 1` with `2 × ceil(S / c) + 2 ≤ steady(b)`.

That keeps rounds plus retry reserve plus time within three quarters of the budget and leaves **at least
`b − steady(b)`** (≥ 25) per 5 min for the backfill. Symbols are assigned to rounds deterministically: sort the
configured symbols, symbol at index `i` is asked in the round of boundary `B` iff `(B / 5 min) mod c == i mod c`.
Every round asks `ceil(S / c)` or `floor(S / c)` symbols.

Allocation inside `b` per 5 min:

| Share | Requests per 5 min | Use |
|---|---|---|
| Rounds | ≤ `ceil(S / c)` | one request per symbol of this round |
| Retry reserve | ≤ `ceil(S / c)` | at most one retry per symbol per round, only after an error |
| Time | ≤ 2 | server time |
| Backfill | `b − 2 × ceil(S / c) − 2`, ≥ `b − steady(b)` | P-12 catch-up |

`c` is capped at **12** (one request per symbol per hour). The Collector refuses to start (config error, fail
closed) when `c(S, b) > 12`. The largest accepted `S` is therefore `12 × floor((steady(b) − 2) / 2)`:

| `b` | `steady(b)` | Max `S` | `S = 10` | `S = 600` |
|---|---|---|---|---|
| 400 (default) | 300 | 1 788 | `c = 1`, backfill 378 | `c = 5`, backfill 158 |
| 200 | 150 | 888 | `c = 1`, backfill 178 | `c = 9`, backfill 64 |
| 100 (minimum) | 75 | 432 | `c = 1`, backfill 78 | refused |

Planned latency at `b = 400` (definition and limits under **Timing**):

| `S` | `c` | Rounds + retries + time | Backfill share | Planned, publication ≤ 200 s | Planned, later publication |
|---|---|---|---|---|---|
| 10 (today) | 1 | 22 | 378 | 200 s | 500 s |
| 149 | 1 | 300 | 100 | 200 s | 500 s |
| 300 | 3 | 202 | 198 | 800 s (13 min) | 1 100 s (18 min) |
| 600 | 5 | 242 | 158 | 1 400 s (23 min) | 1 700 s (28 min) |

(For `S = 300`, `c = 2` gives `2 × 150 + 2 = 302 > 300`, so `c = 3`.)

**Range per request.** The request for boundary `B` asks for `[B − 2c × 5 min, B]`, i.e. `2c + 1` period ends
(3 for `c = 1`, 11 for `c = 5`, 25 at most). It covers the `c` periods since the symbol's previous request plus
another `c + 1` periods of overlap, so a row that was not yet published at one request is fetched by the next
request of that symbol without any retry. Far below 500 rows; the request count, not the row limit, is the binding
constraint.

**Other consumers of the same IP.** A local limiter cannot see them. Therefore [P]:

1. The 60 % headroom is reserved for anything else on the IP. This is an **operational requirement**, written into
   the README for both deployments: nothing else on that IP may call `/futures/data/*` more than 600 times per
   5 minutes (in practice: no second Collector with open interest enabled, and the operator knows what else runs).
2. Binance's answer is the authority. A `429` from the open interest endpoint stops **all** open interest requests
   (rounds and backfill) for `max(Retry-After, 300 s)`; repeated `429` doubles the pause up to 1 h. A `418` stops
   open interest for the rest of the process run (until restart) and logs an error. Candle collection is not paused
   by an open interest `429` (its endpoint has its own limits [D]).
3. Whether a `418` caused by open interest would also ban klines for the IP is **not known**. Point 2 exists to
   make that case unlikely; it cannot rule it out.
4. Two Collectors on the same IP sharing one budget (a coordinator) are **not** proposed: there is one Collector per
   deployment (architecture §4.1), and a coordinator would be a new component.

**Timing.** Measured publication: rows appeared **29.4–186.8 s** after their period end, 18 rows, one afternoon
[M E-5]. Revision 1 proposed a first attempt at `T + 60 s` and retries until `T + 280 s`; that would have spent most
requests on rows not yet published (14 of 18 appeared after 60 s), and a row published after 280 s would wait for
the backfill. Revision 2 removes the dependence on "absent yet" retries:

- **Round schedule.** The open interest round for boundary `B` wakes at `B + round_offset_ms`, proposed
  `round_offset_ms = 200 000` [P: above the observed maximum of 187 s, with 13 s margin; the margin is thin and that
  is acceptable because of the next point].
- **Overlapping range.** Each round asks its symbols for `[B − 2c × 5 min, B]` (Budget above). A row that was not
  yet published at one request of a symbol is fetched by its next request, `c` periods later, without any retry.
  Rows already stored are identical no-ops (writer idempotence).
- **Absence is not an error.** A round that does not get the row for `B` records nothing and does not retry. Only a
  request error (network, `SourceError`, parse error, write failure) uses the one retry from the retry reserve, after
  the existing bounded backoff [C `RetryPolicy`], and only before the round deadline.
- **Round deadline** = `B + 300 s + round_offset_ms − 15 s`, i.e. 15 s before the next round is due. Work still
  running at the deadline is cancelled; writes are idempotent so cancellation is safe.
- **Overlap of two rounds.** Two open interest rounds never run concurrently. If round `B` is still running when
  round `B + 5 min` is due (it cannot, given the deadline, unless the deadline is missed by a slow cancellation), the
  later round **starts after** the earlier finishes, with its own deadline unchanged, and the count of skipped or
  late rounds is logged. A round that would start after its own deadline is skipped and logged. The symbols of a
  skipped round are covered by their next request (the range overlaps by `c + 1` periods), otherwise by the
  backfill.
- **Planned latency** from period end to stored row. It assumes an **ideal run**: the symbol's request starts at
  its round's wake time, succeeds on the first attempt, is not paused by a `429`, and its rows are written without
  yielding. Request and write time are ignored.
  - publication within `round_offset_ms` (200 s; all 18 observed rows [M E-5]): the row's period end can fall just
    after the symbol's request, so it waits for the next one: `(c − 1) × 5 min + 200 s`;
  - publication later than that but before the symbol's next request: `c × 5 min + 200 s`;
  - later still: the request after that (overlap `c + 1`), otherwise the backfill.
- **No latency is guaranteed.** Request errors, a `429` pause (up to 1 h), a round that misses its deadline,
  yielding to candle writes (P-9) or a stopped open interest task each add delay without a fixed bound; after the
  overlap is exhausted the row waits for the backfill, and after `H` it is lost. What **is** guaranteed is the
  honesty of the read side: until a row is stored, its slot is reported as `not_yet_expected`, `due`, `stale`,
  `missing` or `no_longer_recoverable` (P-7), never as `present`, and a late row is never fabricated.
- **`expected_after_ms`** for coverage (P-7) is the planned latency for a late publication,
  `c × 5 min + round_offset_ms` [P] (500 s for `c = 1`, 1 700 s for `c = 5`). It is an expectation used to tell
  `not_yet_expected` from `due`, not a promise.

### P-11 API

- `GET /v1/open-interest?venue&symbol&period&start&end&limit` returns rows ordered by `time_ms`, both values as
  decimal strings, `conflicted` per row, a page `coverage` block with the states of P-7, and
  `series.oldest_time_ms` / `newest_time_ms`; paging, limits and errors mirror `GET /v1/candles`. The response
  states `time_semantics: "period_end"`.
- `GET /health/ingest` gains an **additive** `open_interest` object: `status`, `series_count`, `current`, `due`,
  `stale`, `behind`, `conflicted_series` (P-8). Existing fields keep their meaning (contract: new fields may be added
  within `/v1`).
- `GET /v1/coverage`, `GET /v1/candles` and `GET /v1/indicators` are unchanged.
- **Amendment A-2:** `GET /v1/open-interest/coverage` (contract section 6.6) lists the gaps that the page `coverage`
  block only counts: counts and runs of the P-7 states for a window of at most 10 000 slots, from stored times, no
  values. `GET /v1/open-interest` does not change.
- The exact schema is written as a new section of `docs/contracts/api-v1.md` and approved before OI-5.

### P-12 Restart reconcile and bounded catch-up

There is no checkpoint table; the stored rows are the facts, as for candles (S15 D1) [C].

**On start of the open interest task** (deterministic: the same stored rows, the same `as_of` and the same config
give the same plan):

1. Read, per configured series, the stored `time_ms` within `[as_of − H, as_of]` and the conflicted slots. One
   bounded read per series on the open interest thread; a failing read stops the open interest task (fail closed)
   and leaves candles running.
2. Derive the **holes**: slots in `[as_of − H, newest expected]` that are neither `present` nor `conflicted`.
   Slots older than `as_of − H` are `no_longer_recoverable` and are never requested.
3. Order them into pages of ≤ 500 period ends. Order key: **(page start ascending, symbol ascending)**. The oldest
   holes come first because they are closest to leaving the retrieval window; the newest ones are also covered by
   the rounds.
4. Log a reconcile summary: per series the stored count, holes, no-longer-recoverable count, and the time until
   the oldest hole leaves the window.

**Catch-up execution:**

- Pages run only from the backfill share of the budget (P-10, 100–378 per 5 min depending on `S`) and only between
  rounds (they stop 15 s before the next round is due, like the candle backfill's idle window [C S16 D5]).
- **Idempotence:** each page is an ordinary write; re-running a page after a crash, a cancellation or an unknown
  outcome adds nothing. Nothing is remembered between process runs except the stored rows, so a restart simply
  re-derives the holes.
- **Errors:** a page that fails (network, `SourceError`, parse error, write failure) is retried at most 3 times in
  the same process run with doubling backoff (1, 2, 4 rounds). After that its series' catch-up is suspended until
  restart and logged. A `429`/`418` pauses all pages (P-10).
- **Progress per cycle** re-derives the holes from the stored rows at the start of every idle window, like the
  candle backfill [C S16], so a hole filled by a round is never requested again.
- **Bound:** a fresh 29-day catch-up is 17 pages per series: 170 pages for 10 symbols (one idle window at the
  378 share), 10 200 pages for 600 symbols (about 5.4 hours at the 158 share). Rounds keep the newest data current
  meanwhile.

**Holes Binance does not serve (answered, but without the row).** Binance documents that periods without trades
have no record and are never backfilled [D]; such a slot stays a hole forever and, without a rule, would be asked
for again in every idle window. Rule [P], in process memory only, no new table:

- A slot is **tried** when a page that covers it succeeds (a valid response, all of its transactions committed) and the slot is
  still absent afterwards. Errors are not tries (they follow the error rule above).
- After the 1st try a slot waits **1 h**, after the 2nd **6 h**; after the **3rd** try it is not requested again
  **in this process run**. A page may span cooling slots between expired ones; they are fetched incidentally (and
  stored if present), but their try count and cooldown do not change. It stays `missing`
  in coverage until it leaves the horizon and becomes `no_longer_recoverable` (P-7). It is never recorded as
  unavailable, never filled, and nothing is written to the database about it.
- Pages are built only from slots whose cooldown has expired, using the same order key (page start ascending,
  symbol ascending), so the plan stays deterministic given the stored rows, the clock and the earlier responses.
- The memory is kept as runs of consecutive slots per series with a try count and a next-allowed time, so it is
  bounded by the number of distinct holes (at most `H` / 5 min = 8 352 slots per series).
- **Restart** clears the memory: every hole within the horizon gets one new try at most per restart, which is
  bounded by the catch-up budget and needs no stored state. A crash loop therefore costs at most one catch-up per
  start, within the budget.
- The Collector logs, per idle window, the number of slots that reached 3 tries in this run, per series. This
  makes "probably not served by Binance" visible to an operator without claiming it.
- Overlapping round requests (P-10) are not affected: they ask for the newest `2c + 1` periods regardless of tries.
- **Urgency signal:** if, at the start of an idle window, a hole will leave the window (`T < as_of − H + 1 day`) and
  cannot be fetched within the remaining budget before then, the Collector logs a warning naming the series and the
  slots that will be lost. It does not exceed the budget to save them.

**Long outage:** after more than `H` (29 days) without collection, the slots older than `as_of − H` become
`no_longer_recoverable` and stay visible as such in coverage; the candles of the same period can still be
backfilled. No row is fabricated for them.

### P-13 Configuration

A new **optional** top-level table `[open_interest]` in `config/quasar.toml` [P]:

```toml
[open_interest]
enabled = true
period = "5m"
publication_delay_ms = 15000
round_offset_ms = 200000
budget_per_5min = 400        # integer, validated 100 <= value <= 400
```

Absent means disabled, so existing configuration files and `config/quasar.compose.toml` keep working. Inside the
table every key is required and unknown keys are an error (S20 rule) [C `config/settings.py`]. Open interest is
collected for every symbol of the venue; there is no second symbol list. The cadence `c` is derived, not
configured (P-10). Validation at start: `budget_per_5min` an integer in `[100, 400]`; `c(S, budget_per_5min) ≤ 12`
(at most 1 788 symbols at 400, 432 at 100); `round_offset_ms` in `[60 000, 280 000]`. A failed check names the key
and the computed values.

### P-14 Deployment and migration impact

Facts [C]: the API's `verify_schema` requires the applied migrations to be **exactly** `1..SCHEMA_VERSION` of its
own build (`storage/schema.py`); the migration runner refuses a database newer than its code
(`storage/migrate.py`); only the Collector migrates.

Consequences:

- An API built for version 1 answers `503 not_ready` as soon as the Collector has applied migration 2.
- An API built for version 2 answers `503 not_ready` until the Collector has applied migration 2.
- Restarting both services from one Compose project therefore gives an API outage from the moment the first of the
  two events happens until both have happened, plus the API start time. `docker compose up` does not order them
  (`compose.yaml` has no `depends_on` between the services [C]). Not zero-downtime. Not measured; expected seconds
  to a minute, longer if the Collector migration waits on a lock.
- **No rollback by redeploy.** After migration 2 a Collector of the previous release refuses to start ("database
  is newer than the code"). Going back means restoring a backup taken before the migration, which loses the candles
  collected since.

**Proposal [P]: accept a short, announced API outage.** Rationale: one consumer environment, the API already
answers `503` with a documented meaning, consumers already must treat `503` as "retry later" (contract §6.2).
Procedure, written into the README for the release:

1. Take a consistent backup of the database (WAL checkpoint, copy).
2. Deploy both images of the release (`docker compose up -d`).
3. Wait until `/health/ready` answers 200 and the Collector log shows `migrated to version 2`.
4. Rollback only by restoring the backup with the previous release.

*Alternative (not proposed, open decision O-1):* a two-step backward-compatible rollout. Release N (still schema 1)
ships an API that accepts an explicitly listed set of compatible versions `{1, 2}`; release N+1 adds migration 2.
No outage, at the cost of an ADR-0002 P-8 amendment ("equals" becomes "is in a compatible set") and one extra
release.

### P-15 Tests (regression set, all with mocked exchange responses)

Each slice adds its tests first. Live tests stay under `make live`, never in `make check`.

| Area | Test (behavior that must fail when the guard is removed) | Slice |
|---|---|---|
| Unusual paging | a range of > `limit` period ends is never requested (the adapter splits or refuses) | OI-3 |
| Unusual paging | a fake that returns the **newest** `limit` rows of an over-long range: no row is skipped silently | OI-3 |
| Unusual paging | a row before `startTime` (mid-period start), after `endTime`, unaligned, duplicated, wrong symbol, non-string value, more rows than slots → the whole page is rejected, nothing stored | OI-1/OI-3 |
| Unusual paging | a `startTime` older than `as_of − H` is never sent; a `-1130` answer is a `SourceError`, not a crash | OI-3 |
| Late publication | row `B` absent at round `B`, present at round `B + 5 min` → stored once, no retry used, no error logged | OI-4 |
| Late publication | row absent for 3 rounds → `due` then `stale` in coverage; the backfill fetches it later | OI-4/OI-5 |
| Late publication | a row returned before `T + publication_delay_ms ≤ server_lower_bound` is not stored; untrusted time stores nothing | OI-1/OI-4 |
| Changed history | a re-delivered row with a different value → one conflict row, the stored row unchanged, `conflicted: true` in the API, `conflicted_series` in health | OI-2/OI-5 |
| Changed history | the same differing value delivered twice → still one conflict row | OI-2 |
| Changed history | `UPDATE`, `DELETE`, `INSERT OR REPLACE` on both tables abort (immutability) | OI-2 |
| Rate limiting | the sliding window never allows more than `budget` starts in any 300 s (property test with a fake clock) | OI-3 |
| Rate limiting | rounds pre-empt backfill: with the budget nearly used, a round request is granted and a backfill page waits | OI-4 |
| Rate limiting | `429` pauses rounds and backfill for `max(Retry-After, 300 s)`; repeated `429` doubles up to 1 h; `418` stops the task; candle requests continue throughout | OI-3/OI-4 |
| Rate limiting | `c(S, 400)` for S = 1, 149, 150, 300, 600, 1 788 is 1, 1, 2, 3, 5, 12; S = 1 789 is refused | OI-1 |
| Rate limiting | lower budgets: `c(10, 100) = 1`, `c(36, 100) = 1`, `c(37, 100) = 2`, `c(432, 100) = 12`, S = 433 at b = 100 refused; `c(600, 200) = 9`; the backfill share is ≥ `b − floor(3b / 4)` in every accepted case | OI-1 |
| Rate limiting | `budget_per_5min` 99, 401, 0, -1, 250.0, `"400"` and a missing key are config errors naming the key; 100 and 400 are accepted | OI-4 |
| Rate limiting | the limiter built from `b = 100` never allows more than 100 starts in any 300 s, with rounds, retries, time and backfill all competing (fake clock) | OI-3/OI-4 |
| Rate limiting | the API and the Collector derive the same `c` from the same config file, for b = 100, 200, 400 | OI-4/OI-5 |
| Rate limiting | every symbol is asked exactly once per `c` rounds, and no 5-minute window exceeds `2 × ceil(S / c) + 2` round, retry and time starts (fake clock, S = 600, b = 400 and S = 600, b = 200) | OI-4 |
| Late publication | ideal run (no errors, instant writes), `c = 5`: a row published after its symbol's request is stored by the symbol's next request, within the planned `c × 5 min + 200 s` | OI-4 |
| Late publication | the next request of that symbol fails (injected error, then `429`): the row is stored by a later request or the backfill; until then its slot is `not_yet_expected`, `due` or `stale`, never `present`, and no row is fabricated | OI-4/OI-5 |
| Backfill holes | a slot answered without its row 3 times (cooldowns 1 h, 6 h) is not requested again in the run; it stays `missing`; nothing is written about it | OI-4 |
| Backfill holes | a page that fails with an error does not count as a try for its slots | OI-4 |
| Backfill holes | restart → each such slot gets at most one new try; a cooling slot fetched incidentally keeps its count | OI-4 |
| Restart | stop mid-catch-up and restart → converges, a second start adds nothing | OI-4 |
| Restart | unknown write outcome, then restart → re-derived holes, no duplicate, no conflict | OI-4 |
| Restart | same stored rows, same `as_of` → the same ordered page plan (determinism) | OI-4 |
| Long outage | stored history ending 40 days before `as_of` → slots older than `as_of − H` are `no_longer_recoverable`, never requested; the rest are requested oldest first | OI-4/OI-5 |
| Long outage | holes about to leave the window and not fetchable within budget → the warning names them | OI-4 |
| Independence | open interest source that raises on every call, hangs, or returns `429`: candle rounds collect the same candles with the same `lateness_ms` bound as with open interest disabled | OI-4 |
| Contention | candle write in flight (fake writer blocked on an event) → no open interest transaction begins until it ends; order checked on a recorded timeline | OI-4 |
| Contention | a 500-row backfill page → 5 transactions of ≤ 100 rows; no transaction ever holds more than 100 rows; a failure after the 2nd leaves 2 committed batches and the next try completes the page without duplicates or conflicts | OI-2/OI-4 |
| Contention | real SQLite file: an open interest transaction held open (injected delay) while a candle write starts → the candle write waits for that one transaction only, then commits; no second open interest transaction starts while the candle write is queued | OI-2 |
| Contention | `SQLITE_BUSY` on the open interest connection → that transaction fails, is retried later, no conflict rows, no error reaches the candle loop | OI-2/OI-4 |
| Contention | yielding until the round deadline → rows dropped unwritten and logged; the next overlapping request stores them | OI-4 |
| Contention | the hourly measurement line contains yields, open interest transaction p50/p99/max, busy count and candle write p50/p99/max | OI-4 |
| Contention | measurement (target disk, from the hourly line, amendment A-1): p99 of a 100-row open interest transaction ≤ 50 ms; recorded with the disk, sample size and date | soak |
| Contention | soak: candle write duration p99 with open interest enabled ≤ the value without it + 100 ms | soak |
| Independence | candle fatal error ends the process; open interest leaves no half-written page (atomic per page) | OI-4 |
| Independence | `[open_interest]` absent → no open interest client is created, no request is made, candle behavior is byte-identical in the existing test suite | OI-4 |
| Overlap | a round still running at the next round's due time → the next starts after it, never concurrently; a round past its deadline is skipped and logged | OI-4 |
| Deployment | API built for version 2 against a version 1 database → `503`; migration 1→2 on a copy of a v1 database with candles keeps every candle row (checksum of the table before and after) | OI-2 |

### P-16 Slices

| Slice | Content | Gate |
|---|---|---|
| OI-1 | domain `OpenInterest` value object, validation, raw row parser, eligibility (pure) | this ADR |
| OI-2 | migration 002, open interest writer with conflicts, its own connection and thread, transactions of ≤ 100 rows; measure the 100-row transaction (P-9) | this ADR (P-6, P-8, P-9) |
| OI-3 | adapter method on its own CCXT client, request rules (P-5), sliding-window budget and `429`/`418` handling (P-10); `fetch-oi-once` CLI command | this ADR |
| OI-4 | open interest task: rounds with overlapping range, restart reconcile, catch-up, config table, isolation (P-9, P-10, P-12, P-13) | this ADR |
| OI-5 | `GET /v1/open-interest`, `/health/ingest` extension | contract section approval |
| OI-6a | `GET /v1/open-interest/coverage` (counts and runs), its contract section 6.6, a refresh benchmark harness (amendment A-2) | the user approves the contract text first |
| OI-6b | symbol overview page shows open interest coverage (strip, summary, reasons) | OI-6a merged and the benchmark at 300 and 600 symbols read and approved by the user |

New invariant register rows mirror INV-007/008/009/012 for open interest and add: "open interest never delays or
stops candle collection" (evidence: the independence tests above) and "open interest requests stay within the
budget" (evidence: the rate limiting tests).

## Consequences

- One additive schema version bump with an announced API outage and no rollback by redeploy (P-14).
- Open interest coverage can have holes that candles do not: omissions Binance documents, and anything older than
  29 days that was never stored (P-7, P-12).
- Candle contracts, tables, schedule and limits do not change. Open interest takes its own client, budget, writer
  connection and time slot (P-9).
- At the architecture's 600-symbol target each symbol is asked every 25 minutes (`c = 5`); the stored data stays at
  5m and the planned latency in an ideal run is up to about 28 minutes (P-10). No latency is guaranteed; the read
  side reports what is not yet stored. Today's 10 symbols keep `c = 1`. A lower `budget_per_5min` raises `c`.
- Holes Binance does not serve cost at most three requests per process run each, plus one per restart (P-12).
- Open interest writes can be delayed or dropped for a round in favour of candle writes; they are fetched again
  (P-9).
- Measurement debt: one machine, 7 boundaries, three symbols, one afternoon, a 32-minute recheck. The soak test
  re-measures publication time, settling, revisions, the 30-day edge and the 100-row transaction time.

## Open decisions (for the user)

| ID | Question | Proposal in this ADR |
|---|---|---|
| O-1 | Deployment: accept a short API outage, or a two-step compatible rollout with an ADR-0002 P-8 amendment? | accept the outage (P-14) |
| O-2 | Budget: default and maximum 400 of 1 000 per 5 min (40 %), minimum 100, leaving at least 60 % for other users of the IP. | 400 (P-10) |
| O-3 | Name of the "not yet expected" state: `not_yet_expected` (rule-based, same as candles) or the review's `not_yet_published`? | `not_yet_expected` (P-7) |
| O-4 | `conflicted_series` in `/health/ingest` for open interest makes open interest health stricter than candle health. Keep, or mirror candles (no conflict count)? | keep (P-8) |
| O-5 | Latency at scale: is a planned latency of up to ~28 minutes (ideal run, no guarantee) acceptable at 600 symbols and `b = 400` (`c = 5`)? The alternative is a higher budget share than 40 % (O-2). | accept (P-10) |

## Part 3 — Evidence

Probe of 2026-11-02 (Europe/Prague) with CCXT 4.5.89 from one macOS machine, against `fapi.binance.com`. Scripts
were throwaway in the session scratchpad and are not in the repository.

- **E-1 Official page** (`developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics`,
  read 2026-11-02 through a page-to-text fetch, quoted twice and consistent): path `/futures/data/openInterestHist`;
  `period` 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h and one more value not captured; `limit` default 30, max 500;
  "If startTime and endTime are not sent, the most recent data is returned."; "Only the data of the latest 1 month
  is available."; "IP rate limit 1000 requests/5min"; IP weight 0; "A period with no trades has no record and is
  omitted from the response. Such gaps are expected and are not backfilled."; `timestamp`: "End time of the period,
  in milliseconds." Not stated: whether the 1 000 requests are shared with other endpoints, the window type, a usage
  header, the units of the values, the behavior on `429`/`418` for this endpoint.
- **E-2 Shape and decimals.** 500 consecutive BTCUSDT rows: every `timestamp` an int aligned to 300 000 ms,
  contiguous (no omitted period); every value a string in the plain decimal grammar of ADR-0001 P-7, max 19
  characters, always 8 fractional digits. Keys: `symbol`, `sumOpenInterest`, `sumOpenInterestValue`,
  `CMCCirculatingSupply`, `timestamp`. LTCUSDT had the same shape (3 rows).
- **E-3 Units sanity check.** BTCUSDT row: `sumOpenInterest` `95677.779`, `sumOpenInterestValue`
  `7446123150.675` → ratio ≈ 77 825, the BTC price in USDT at the time. The snapshot endpoint returned
  `openInterest` `95574.470` a few seconds later: same order and unit. One row; a sanity check, not proof.
- **E-4 Request behavior** (BTCUSDT). `startTime` 29 and 30 days back accepted; 31, 35 and 40 days rejected with
  `-1130`. Range of 20 periods with `limit=3`: `startTime` only → the newest 3 rows (`−2, −1, 0`); `startTime`
  and `endTime` → the newest 3 rows of the range (`−7, −6, −5` for a range ending at `−5`). A `startTime` 1 ms after
  a period end also returned that period's row. Aligned `[start, end]` of 100 periods returned 101 rows (inclusive
  end). `limit=501` and `600` were accepted and returned that many rows.
- **E-5 Publication and settling.** First probe: the row for boundary `B` appeared 125–128 s after `B` (BTCUSDT,
  1 boundary). Extended probe: 6 consecutive boundaries (1793620800000 … 1793622300000) × BTCUSDT, ETHUSDT, LTCUSDT,
  each symbol polled about every 6 s (`limit=4`) for ~295 s after the boundary. All 18 rows appeared **after**
  their `timestamp`, between **29.4 s and 186.8 s** (BTCUSDT 52–171 s, ETHUSDT 65–187 s, LTCUSDT 29–157 s); 4 of 18
  appeared within 60 s. Per boundary the symbols appeared in varying order, spread over up to ~150 s. **0 changes**
  of any watched row after its first appearance. Offsets carry about ±6 s of poll granularity.
- **E-6 Recheck.** A fixed range of 101 rows per symbol (BTCUSDT, ETHUSDT, LTCUSDT) fetched again **32 minutes**
  later: same timestamps, **0 differences** (303 rows). Partial evidence, shorter than ADR-0001's 1 h recheck.

## Revision history

- **Revision 1** (2026-11-02): initial proposal.
- **Revision 2** (2026-11-02), after the user's review:
  1. isolation split into task and resource isolation with concrete limits (P-9); corrected revision 1's claim that a
     candle error does not stop open interest, and that health could report a stopped task;
  2. restart reconcile and bounded catch-up specified, oldest-first by retrieval deadline (P-12);
  3. budget of 400/5 min with shares, retry reserve, `429`/`418` handling and the operational rule for other
     consumers of the IP (P-10);
  4. conflicts' effect on logs, API and health (P-8);
  5. coverage states defined by what we know, including `no_longer_recoverable` (P-7);
  6. deployment: revision 1's "deploying together is enough" was wrong; outage and no-rollback stated, outage
     proposed, compatible rollout as O-1 (P-14);
  7. regression test set (P-15);
  8. timing: revision 1's `T + 60 s` / `T + 280 s` replaced by a `B + 200 s` round with a 3-period overlapping
     range and no retry on absence; overlap of rounds defined (P-10).
- **Revision 3** (2026-11-02), after the user's second review:
  1. scaling: derived cadence `c(S)` with deterministic symbol assignment and a `2c + 1` overlapping range replaces
     the 199-symbol ceiling; budget and worst latency tabulated for 10 to 600 symbols (P-10);
  2. backfill: in-memory try count with cooldowns (1 h, 6 h, stop after 3 tries per run) for holes answered without
     a row; no table, nothing marked as non-existent (P-12);
  3. SQLite contention: yield signals from the candle path, batch halving, 1 s interrupt watchdog and pause rules,
     with acceptance tests (P-9, P-15).
- **Revision 4** (2026-11-02), after the user's final review:
  1. budget: `c(S, b)` uses the configured `budget_per_5min` (`steady(b) = floor(3b / 4)`); allowed range
     100–400, validation and tests for lower budgets (P-10, P-13, P-15);
  2. latency: "worst latency" renamed **planned latency** of an ideal run; stated that no latency is guaranteed,
     only that unstored slots are reported honestly; tests split into ideal-run and failure cases (P-10, P-15);
  3. SQLite: revision 3's `candle_pressure`, batch halving, pause rules and the `Connection.interrupt()` watchdog
     removed; replaced by transactions of ≤ 100 rows, yielding while the candle writer is active, hourly contention
     measurement and a measured acceptance criterion (P-9, P-15).
- **Amendment A-1** (2026-11-02, approved by the user during OI-2): the user does not run the benchmark on the VPS.
  The P-9 criterion (p99 ≤ 50 ms for a 100-row transaction on the target disk) moves from OI-2 to the soak after
  OI-4, where it is read from the Collector's hourly measurement line on production data (P-9 rule 5). Rationale:
  until OI-4 nothing writes open interest, so candle collection cannot be affected before the measurement exists,
  and production data measures a grown database instead of an empty one. P-9 and the P-15 row are updated; the
  rules themselves are unchanged.
- **Amendment A-2** (2026-11-03, approved by the user together with contract section 6.6; the OI-6 design was approved
  on the same day): decision OA7 left a per-run gap listing for open interest "for a
  later section if wanted". The user asked for it when approving the OI-6 design (a strip with the position of holes
  on the symbol overview page, issue #6). P-11 gains `GET /v1/open-interest/coverage` (counts and runs of the P-7
  states for at most 10 000 slots, no values). The P-16 row OI-6 is split into OI-6a (the endpoint, its contract text
  and a benchmark harness) and OI-6b (the page); OI-6b is gated by a refresh benchmark at 300 and 600 symbols. Rules
  P-1 to P-15 are unchanged; no schema change, no migration, no configuration key. Design:
  `docs/superpowers/specs/2026-11-03-oi-6-open-interest-overview-design.md`.
