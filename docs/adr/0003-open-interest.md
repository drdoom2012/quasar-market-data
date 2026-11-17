# ADR-0003: Open interest (Binance USDT-M, 5m)

- **Status:** Accepted (approved by the user on 2026-11-02: revision 4 as written, including the proposals for the
  open decisions O-1 to O-5). Revisions 2 to 4 answer the user's reviews (listed at the end). The API response
  schema (P-11) is contract section 6.5 of `docs/contracts/api-v1.md`, approved by the user on 2026-11-03 and implemented by OI-5. Amendment A-1 (2026-11-02, approved by
  the user): the 100-row transaction criterion of P-9 is checked from production measurements after OI-4, not by
  a benchmark in OI-2 (see the revision history). Amendment A-2 (2026-11-03, approved by the user with contract section 6.6): a per-run gap listing for open interest and the split
  of the OI-6 slice (see the revision history). Amendment A-4 (2026-11-04, by the user's explicit instruction): the retrieval horizon `H` is the new configuration key `backfill_days` (1 to 30, a larger value stops the process at start), no longer the constant 29 days (see the revision history). Amendment A-3 (2026-11-03 approved, 2026-11-04 applied, ADR-0004 O-6): the open interest task shares the Collector's `ExtensionPause` with funding, so a `429` or `418` seen by either dataset stops both. Amendment A-5 (2026-11-14, approved by the user with ADR-0005): one stamp per transaction taken by the writer, a single-row confirmation after each commit (P-9), schema version 4 (see the revision history). Amendment A-6 (2026-11-16, approved by the user; request `docs/requests/2026-11-16-quasar-fresh-open-interest.md`): the per-symbol retry reserve becomes one shared reserve of at most `R = 50` retries per round (P-10), the new required key `[open_interest].symbols` (`"all"` or a list) fixes the open interest symbols (P-13), `/health/ingest` gains the additive `collected` object and `/v1/instruments` the additive field `open_interest_period` (P-11), and the backfill gate counts a slow previous round in full (P-10); design `docs/superpowers/specs/2026-11-16-adr-0003-a6-fresh-open-interest-design.md`; no schema change (see the revision history). OI-6a (the endpoint, contract section 6.6) and OI-6b (the overview page) are implemented on one branch at the user's request (one pull request, not two); the OI-6b gate of P-16 (the benchmark numbers read and approved by the user) is recorded in the OI-6 design, section 14: the numbers are measured, the user's reading of them is pending.
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
- No request uses a `startTime` older than the **retrieval horizon** `H` before the current server lower bound. `H` is
  the configured `backfill_days` (P-13, amendment A-4; **1 to 30 days**, the shipped files use **29**, one day of
  margin under the observed 30/31-day edge of Binance, which may itself vary [P]). The request check of the
  adapter is the largest value the file allows (30 days), so a smaller configured `H` is enforced by the plan
  (P-12), not by a second constant.

### P-6 Schema version 2

*Amended by A-5 (2026-11-14): `ingested_at_ms` is the stamp of the row's write transaction, taken by the writer under the lock, and schema version 4 adds a registration trigger on both tables of this section (ADR-0005 P-2, P-3).*

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

*Amended by A-5 (2026-11-14): a candle write waits for at most one open interest transaction plus its single-row confirmation (ADR-0005 P-6).*

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
value is a config error at start. (The floor of 100 was proposed when the configuration had 10 symbols: it kept them
at `c = 1` and the backfill share at 25 or more per 5 min. After A-6, `b = 100` keeps up to 36 symbols at `c = 1`; the
shipped files list 200 at `b = 400`.)

**Cadence `c`: each symbol is asked every `c` periods, not every period** [P]. Stored data stays at 5m granularity:
one request returns every 5m row of its range, and the range is far below the 500-row limit (below). `c` is not
configured; it is derived from the number of open interest symbols `S` (P-13: `len(symbols)`, or the venue's count for
`"all"`) **and the configured budget `b`** by one pure domain function, so the Collector and the API (which reads the
same config file, P-7) get the same value. *Amended by A-6 (2026-11-16): the retry reserve is shared by the round, no
longer one per symbol.*

`steady(b) = floor(3b / 4)`;
`n = ceil(S / c)`, `reserve(n) = min(R, n)` with `R = 50`, `round_cost(S, c) = n + reserve(n) + 2`;
`c(S, b)` = the smallest integer `c ≥ 1` with `round_cost(S, c) ≤ steady(b)`.

That keeps rounds plus retry reserve plus time within three quarters of the budget and leaves **at least
`b − steady(b)`** (≥ 25) per 5 min for the backfill. Symbols are assigned to rounds deterministically: sort the
open interest symbols, symbol at index `i` is asked in the round of boundary `B` iff `(B / 5 min) mod c == i mod c`.
Every round asks `ceil(S / c)` or `floor(S / c)` symbols.

- **Never worse than before A-6.** `n + min(50, n) + 2 ≤ 2n + 2`, so for every `(S, b)` the new `c` is at most the one
  of the rule `2 × ceil(S / c) + 2 ≤ steady(b)` (one reserved retry per symbol), every configuration accepted before
  stays accepted, and `c` changes only where `n > 50`.
- **Round rule.** A symbol whose request fails gets its one retry (same `RETRY_DELAY_MS`, same deadline rule) only if
  it obtains a **retry ticket** from the round; the round hands out at most `reserve(n)` tickets. Otherwise it is not
  retried, its outcome is `retry_reserve_exhausted`, and it waits for its next request (`c` periods later; with
  `c = 1` the overlap `[B − 10 min, B]` still contains `B` in the next two rounds) or the backfill. What uses a ticket
  is exactly what used a retry before: a request error, `SourceError`, a parse error and a write failure including
  `busy`. A `429` pause, a refusal and an absent row use none.
- **Atomic accounting.** The tickets are a counter owned by one round's state, never shared between rounds or tasks. A
  ticket is taken by one synchronous call that checks and increments with no `await` between the two; it runs inside
  the attempt's in-flight slot, after the pause and stop checks and before the request. The round runs on one event
  loop, so concurrent symbol coroutines cannot interleave inside the call and the count never exceeds `reserve(n)`. A
  ticket is **never returned**: a retry cancelled at the deadline after taking its ticket still counts. That errs
  towards fewer retries, never more, so the starts of a round never exceed `round_cost`. A change that moves the round
  onto threads must replace the counter with a lock; a test pins the single-loop assumption.
- **Constant, not configured.** `R = 50` is a domain constant beside the cap `c ≤ 12`, so the Collector and the API
  keep deriving the same `c` from the same file. Retries follow request errors only, which are rare in an ideal run;
  under a broad outage 50 retries in 5 minutes do not help, and the overlap fetches the rows again. In a partial
  outage the reserve favours symbols early in sorted order; that is accepted.
- **Backfill.** The share is `b − round_cost(S, c)`, at least `b − steady(b)` (≥ 25) in every accepted case. The
  backfill gate leaves `reserve(n) + 2` headroom for the next round instead of `n + 2`.

Allocation inside `b` per 5 min:

| Share | Requests per 5 min | Use |
|---|---|---|
| Rounds | ≤ `ceil(S / c)` | one request per symbol of this round |
| Retry reserve | ≤ `min(50, ceil(S / c))` | at most one retry per symbol, only after an error, from the shared reserve of the round |
| Time | ≤ 2 | server time |
| Backfill | `b − round_cost(S, c)`, ≥ `b − steady(b)` | P-12 catch-up |

`c` is capped at **12** (one request per symbol per hour). The Collector refuses to start (config error, fail
closed) when `c(S, b) > 12`. The largest accepted `S` is therefore `12 × (steady(b) − 52)` when `steady(b) ≥ 102`
(`n ≥ 50` at `c = 12`), otherwise `12 × floor((steady(b) − 2) / 2)`:

| `b` | `steady(b)` | Max `S` | `c = 1` up to `S` | `S = 10` | `S = 200` | `S = 522` | `S = 600` |
|---|---|---|---|---|---|---|---|
| 400 (default) | 300 | 2 976 | 248 | `c = 1`, backfill 378 | `c = 1`, backfill 148 | `c = 3`, backfill 174 | `c = 3`, backfill 148 |
| 200 | 150 | 1 176 | 98 | `c = 1`, backfill 178 | `c = 3`, backfill 81 | `c = 6`, backfill 61 | `c = 7`, backfill 62 |
| 100 (minimum) | 75 | 432 | 36 | `c = 1`, backfill 78 | `c = 6`, backfill 30 | refused | refused |

(Before A-6 the largest `S` was 1 788 at 400, 888 at 200 and 432 at 100, and `c = 1` reached `S = 149` at 400. Checked
by two independent throwaway scripts over `S = 1..3999`; the tests re-derive the table.)

Planned latency at `b = 400` (definition and limits under **Timing**; `p` is the symbol's position in its round,
`p ≈ n / 4.2` s at worst in an ideal run):

| `S` | `c` | Rounds + retries + time | Backfill share | Planned, publication ≤ 200 s | Planned, later publication |
|---|---|---|---|---|---|
| 10 | 1 | 22 | 378 | 200 s + `p` | 500 s + `p` |
| 200 (the shipped subset, D-3) | 1 | 252 | 148 | 200 s + `p` | 500 s + `p` |
| 248 (largest at `c = 1`) | 1 | 300 | 100 | 200 s + `p` | 500 s + `p` |
| 300 | 2 | 202 | 198 | 500 s + `p` (8 min) | 800 s + `p` (13 min) |
| 600 | 3 | 252 | 148 | 800 s + `p` (13 min) | 1 100 s + `p` (18 min) |

(For `S = 300`, `c = 1` needs `300 + 50 + 2 = 352 > 300`, so `c = 2`.)

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
  the existing bounded backoff [C `RetryPolicy`], only before the round deadline, and only while a retry ticket of
  the round is left (A-6, above).
- **Round deadline** = `B + 300 s + round_offset_ms − 15 s`, i.e. 15 s before the next round is due. Work still
  running at the deadline is cancelled; writes are idempotent so cancellation is safe.
- **Overlap of two rounds.** Two open interest rounds never run concurrently. If round `B` is still running when
  round `B + 5 min` is due (it cannot, given the deadline, unless the deadline is missed by a slow cancellation), the
  later round **starts after** the earlier finishes, with its own deadline unchanged, and the count of skipped or
  late rounds is logged. A round that would start after its own deadline is skipped and logged. The symbols of a
  skipped round are covered by their next request (the range overlaps by `c + 1` periods), otherwise by the
  backfill.
- **Planned latency** from period end to stored row (*amended by A-6*). It assumes an **ideal run**: the symbol's
  request starts at its round's wake time, the round itself starts at its wake time (a round that starts late is
  outside it), the request succeeds on the first attempt, is not paused by a `429`, and its rows are written without
  yielding. `p` is the position of the symbol in its round: the time from the round's wake to the stored row of that
  symbol. It is bounded by the round deadline (285 s after the wake); in an ideal run it is about `n / 4.2` s at worst
  (measured rate, see "R1 latency" below). The definition replaces "request and write time are ignored".
  - publication within `round_offset_ms` (200 s; all 18 observed rows [M E-5]): the row's period end can fall just
    after the symbol's request, so it waits for the next one: `(c − 1) × 5 min + 200 s + p`;
  - publication later than that but before the symbol's next request: `c × 5 min + 200 s + p`;
  - later still: the request after that (overlap `c + 1`), otherwise the backfill.

  A row becomes readable 2 s after it is stored (`READABLE_BOUND_MS`, ADR-0005).
- **No latency is guaranteed.** Request errors, a `429` pause (up to 1 h), a round that misses its deadline,
  yielding to candle writes (P-9) or a stopped open interest task each add delay without a fixed bound; after the
  overlap is exhausted the row waits for the backfill, and after `H` it is lost. What **is** guaranteed is the
  honesty of the read side: until a row is stored, its slot is reported as `not_yet_expected`, `due`, `stale`,
  `missing` or `no_longer_recoverable` (P-7), never as `present`, and a late row is never fabricated.
- **`expected_after_ms`** for coverage (P-7) is the planned latency for a late publication without `p`,
  `c × 5 min + round_offset_ms` [P] (500 s for `c = 1`, 1 700 s for `c = 5`); the contract field is unchanged
  (A-6). It is an expectation used to tell `not_yet_expected` from `due`, not a promise. `lag_threshold_ms` absorbs
  `p` **only while `p ≤ lag_threshold_ms`** (60 000 in both shipped files). At `S = 248`, `p ≈ 59 s` sits on that
  edge, so a late-published row would briefly count as `stale` in health; this is a second reason to keep `S` well
  below 248.
- **Pace rule (A-6, decision D-7).** The backfill gate leaves out the previous round's starts when that round
  **began** on schedule (`_previous_round_if_on_schedule` checks the first start of the span with a 5 s tolerance).
  If that round's starts were spread out, for example by timeouts, and the following idle window filled the backfill
  share, the window is full at the wake and our k-th start waits for the previous round's k-th start to expire: the
  round copies the previous round's slow pace on a healthy network, and the next round copies ours in turn, for as
  long as the catch-up keeps the backfill saturated. A-6 therefore counts the previous round in full also when its
  span (`_last_round_span`, from the round's start to its end) exceeds the **pace limit**
  `pace_limit_ms(n) = ceil(n / MAX_IN_FLIGHT) × 600 ms` (`MAX_IN_FLIGHT = 2`): 60 s at `n = 200`, 39.6 s at
  `n = 131`, against the measured maximum of 30.8 s. This extends the existing exception for a late previous round to
  a slow one. After a late or slow round the next idle window starts no page (a slow round of 207 starts plus
  `round_cost` 252 exceeds 400, so the gate cannot keep `round_cost` free; the next round competes only with the
  previous round's own starts, which began 300 s earlier); the allocation above
  (`b − round_cost(S, c)` ≥ `b − steady(b)`) is unchanged and is delivered after every on-schedule, on-pace round, as
  it is today after every on-schedule round.

**R1 latency at the shipped subset (A-6).** Scope: `S = 200`, `b = 400`, so `c = 1`, `n = 200`, `reserve = 50`,
`round_cost = 252`, backfill share 148. The request of Sentaris Engine (R1): the row of period end `e` readable by
`e + 600 s`. "Readable" is the stamp plus 2 s. The figures are labelled by their kind: **measured** is read from the
local Collector, **estimated** is derived by linear extrapolation or from the code and is not a measurement.

*Measured* (local Collector `data/quasar.db`, `c = 4`, `n ≈ 131`, 78 rounds in 24 h, 2026-11-15/16; not at `n = 200`,
not at `c = 1`):

| Quantity | Value |
|---|---|
| First row after the wake | typically 0.7–1.3 s |
| Span of one round's rows | 21.4–30.8 s, mean 24.5 s (≈ 4.2–6 symbols/s) |
| Late round starts | +29.5 s (host stall), +80.6 s (first round after a restart) |
| Rows not yet published at their request | ≈ 1.8 % |
| Publication delay (E-5, 18 rows) | 29.4–186.8 s |

*Estimated.* Model: a successful request holds an in-flight slot for 0.48 s (the measured 4.2 symbols/s at 2 slots); a
failed request holds it for `f`. Every attempt, retry included, runs inside the round's semaphore. The adapter sets no
CCXT timeout, so a timed-out request holds its slot for CCXT's default `f = 10 s`
(`ccxt.async_support.binanceusdm().timeout = 10000`). A connection refusal or an HTTP error answer is fast
(`f ≈ 0.5 s`). With `F` failing first attempts and
`r = min(F, 50)` retries, a retry waits `RETRY_DELAY_MS` and then queues behind every first attempt of the round
(all `n` first attempts queue on the semaphore when the round starts, and it serves them in order), so only the
failing first attempts delay the last symbol. Its position is about

`p(F, f) ≈ ((n − F) × 0.48 s + F × f) / 2`, capped by the round deadline (285 s after the wake).

The retries run after it, still within the deadline: a row stored by a successful retry is at about
`p(F, f) + r × 0.48 s / 2`. (Correction of 2026-11-16, found by the simulation: the first estimate counted the
`r` retries before the last symbol, `((n − F) × 0.48 s + (F + r) × f) / 2`.)

A row published **before** its symbol's request in round `B` is readable by `e + 200 s + p + 2 s`. A row published
**after** it (≈ 1.8 % measured) is readable by `e + 500 s + p' + 2 s`, where `p'` is the position in the next round.
R1 therefore needs `p ≤ 396 s` on the first path, which always holds within the deadline, and `p' ≤ 98 s` on the
second.

| Scenario | Stored-and-readable by | R1 |
|---|---|---|
| Ideal, published before the request (`p ≤ 48 s`) | ≤ `e + 250 s`; *simulated (fake clock)*: `e + 250.0 s` | met (zero margin in the simulation) |
| Ideal, published after the request | ≤ `e + 550 s`; *simulated (fake clock)*: `e + 550.0 s` | met (zero margin in the simulation) |
| `F` fast failures (`f ≈ 0.5 s` of slot time), any `F ≤ 200`, reserve exhausted or not | last symbol `p ≤ 50 s`, every row the round stores (retries included) `p ≤ 63 s`: ≤ `e + 265 s` / `e + 565 s`; *simulated (fake clock)*: `F = 60` latest stored row `e + 262.6 s`, last symbol's late row `e + 550.6 s`; `F = 200` latest stored row `e + 264.0 s`; the last symbol's late row is bounded by `p' ≤ 50 s`, ≤ `e + 552 s`, since retries do not delay it (the last symbol, without a ticket, falls under the exhausted lines) | met |
| Slow failures in the round that serves the late path, `f = 10 s` (default timeout) | `p' ≈ 48 s + F × 4.76 s`: met while `F ≤ 10` (`p' ≈ 96 s`, `e + 598 s`); `F = 11` gives `p' ≈ 100 s`, `e + 602 s`; *simulated (fake clock)*: `F = 5` `e + 573.84 s`, `F = 10` `e + 597.6 s`, `F = 11` `e + 602.4 s` | met while `F ≤ 10` (`F = 10`: simulated `e + 597.6 s`, 2.4 s margin); **missed** from `F = 11` for late-published rows of symbols after the failures in sorted order; early rows met |
| Same with `f = 3 s` (only if a timeout were set; D-6 sets none) | `p' ≈ 48 s + F × 1.26 s`: met while `F ≤ 39` (`p' ≈ 97 s`); not simulated | missed above |
| Slow failures, `F ≥ 50` at `f = 10 s` | the last symbol's first attempt ends after the round deadline (`p ≈ 48 s + F × 4.76 s > 285 s`; `F = 49` gives 281.2 s, `F = 50` 286 s); unfinished symbols end `deadline`, their rows come from the next round (`e + 500 s + p'`). Below that, the retries can still reach the deadline after the last symbol; their rows also come from the next round. *simulated (fake clock)*: the edge is at `F = 50` (`F = 49`: last symbol readable by `e + 483.28 s`; `F = 50`: `deadline`); `F = 60`: the unfinished last symbol's early row `e + 550.0 s` from the next round | early rows met if the next round is healthy; late rows missed |
| Reserve exhausted, the unretried symbol's next request succeeds | its row comes from the next round (overlap `[B − 10 min, B]`): `e + 500 s + p'`; *simulated (fake clock)*: `e + 550.0 s` | met while `p' ≤ 98 s` |
| Exhausted, and the next request also fails | `e + 800 s + p`; *simulated (fake clock)*: `e + 850.0 s` | **missed**, fail closed (`series_stale`) |
| Limiter contention: backfill saturated, previous round on schedule **and on pace** (pace rule above) | our k-th start waits at most until the previous round's k-th start leaves the window: `p' ≤ 60 s + 5 s`; *simulated (fake clock)*: `e + 550.0 s` (no start waited: the previous round's k-th start leaves the window as our k-th is due) | met (≤ `e + 567 s`) |
| Previous round late (> 5 s) or slow (span above the pace limit) | the backfill gate counts that round in full and starts no page, so the round competes only with the previous round's own starts, which began 300 s earlier; a late start adds its own delay. Corner case: two consecutive fully retried rounds (252 + 252 > 400) can still make the second wait, as the late-round exception already can. A late start `L` gives `p' ≈ L + 48 s`; retries run after the last symbol and do not shorten the limit, fast first-attempt failures before it shorten it by at most 2 s. *simulated (fake clock)*: 30 s late `e + 580.0 s`, 50 s late `e + 600.0 s`, 51 s late `e + 601.0 s` | met up to a late start of 50 s; the observed 30 s stall fits, the 80.6 s start after a restart misses |
| The round's server-time call fails (round `untrusted`, nothing stored) | rows come from the next round; *simulated (fake clock)*: early rows `e + 550.0 s` | early rows met (`e + 550 s`); late rows missed |
| A `429` pause (≥ 300 s, shared with funding) | at least one round lost | **missed** for the pause; fail closed |
| An open interest write waiting for candle writes while it holds its slot (P-9) | bounded only by the round deadline (for example candle backfill after a restart) | **not bounded** by A-6; missed when the wait is long |

*Simulated (fake clock)* values come from `tests/collector/test_open_interest_latency.py` (A-6 Task 5): the real
task and budget on a model of the network (a success holds its slot 0.48 s, a fast failure 0.5 s and a slow one 10 s,
each the whole slot time), not a measurement of Binance. The ideal lines are met with **zero margin** in the
simulation (`e + 250.0 s`, `e + 550.0 s`): on the model both slots finish together. The simulation reads the stamp of
the recording writer; the real writer keeps stamps unique (`max(wall, last + 1)`), which adds 1 ms per stamp that
falls in the same millisecond as the previous one (`e + 250.001 s` and `e + 550.001 s` with the real writer on the
same model). A late start of 50 s is also met with zero margin (`e + 600.0 s`). A threshold the correction moved is
pinned by a pair of tests, one at it and one above it (`F = 10` / `11` at `f = 10 s`, the deadline edge `F = 49` /
`50`, a late start of 50 s / 51 s).

For the shipped subset (`S = 200`): `c = 1`, `p ≈ 48 s`. Rows published within 200 s (about 98 % in the measurement)
are stored by about `e + 250 s`; late-published rows by about `e + 550 s`, 50 s inside R1. A round that starts 30 s
late still fits (`p ≈ 78 s`, `e + 580 s`); the 80 s start after a restart does not (`e + 630 s`), and those rows
fail closed in the consumer (`series_stale`). Health side effect: `expected_after_ms + lag_threshold_ms = 560 s`; a
late-path row readable after `e + 560 s` shows its series as `stale` in `/health/ingest` for those seconds, which is a
correct reading of the rule.

Even with an unlimited budget, 522 symbols in one round would store a late-published row only about `e + 626 s`
(`e + 500 s + p + 2 s` at `p ≈ 124 s`), so a subset is needed for the timing as well as for the budget.

**Verification.** (1) A deterministic simulation on the fake clock (`tests/collector/test_open_interest_latency.py`)
runs the real task and the real budget with a fake source, at `S = 200`, `b = 400`: each line of the table has a test
asserting its bound or, for a "missed" line, that the row is readable only after `e + 600 s`; a threshold is pinned by a met / missed pair. It is a
measurement of the code on a model of the network, not of Binance. (2) After deployment a soak measurement from production,
`readable_by_ms − time_ms` per row over 24 h and the history call durations of the hourly measurement line (`request_ok_*_ms`, `request_failed_*_ms`, README), is
recorded here as evidence E-7 (**measured**). It is not a release gate: no latency is guaranteed.

### P-11 API

- `GET /v1/open-interest?venue&symbol&period&start&end&limit` returns rows ordered by `time_ms`, both values as
  decimal strings, `conflicted` per row, a page `coverage` block with the states of P-7, and
  `series.oldest_time_ms` / `newest_time_ms`; paging, limits and errors mirror `GET /v1/candles`. The response
  states `time_semantics: "period_end"`.
- `GET /health/ingest` gains an **additive** `open_interest` object: `status`, `series_count`, `current`, `due`,
  `stale`, `behind`, `conflicted_series` (P-8). Existing fields keep their meaning (contract: new fields may be added
  within `/v1`).
- `GET /v1/coverage`, `GET /v1/candles` and `GET /v1/indicators` are unchanged.
- **Amendment A-6:** two additive fields. `/health/ingest` `open_interest` gains `collected`, which judges only the
  series of the open interest symbols of the configuration file (P-13), so retired series cannot fill `behind` or
  move `status`; `GET /v1/instruments` gains `open_interest_period` per entry, the configured period for an open
  interest symbol and `null` otherwise, so a consumer can tell "not collected" from "broken". Every existing field keeps
  its meaning; the one changed value is `behind[].configured` with a list (it is true only for an open interest
  symbol). No new endpoint; the contract sections are 6.4.2 and 6.5.8.
- **Amendment A-2:** `GET /v1/open-interest/coverage` (contract section 6.6) lists the gaps that the page `coverage`
  block only counts: counts and runs of the P-7 states for a window of at most 10 000 slots, from stored times, no
  values. `GET /v1/open-interest` does not change.
- The exact schema is written as a new section of `docs/contracts/api-v1.md` and approved before OI-5.

### P-12 Restart reconcile and bounded catch-up

There is no checkpoint table; the stored rows are the facts, as for candles (S15 D1) [C].

**On start of the open interest task** (deterministic: the same stored rows, the same `as_of` and the same config
give the same plan):

1. Read, per series of an open interest symbol (P-13), the stored `time_ms` within `[as_of − H, as_of]` and the conflicted slots. One
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
  rounds (they stop 15 s before the next round is due, like the candle backfill's idle window [C S16 D5]). Symbols
  that are not open interest symbols (P-13) get no page, so series of symbols that left the list are not completed
  (A-6). The gate leaves `reserve(n) + 2` starts free for the next round and counts a late or slow previous round
  in full (P-10, pace rule).
- **Idempotence:** each page is an ordinary write; re-running a page after a crash, a cancellation or an unknown
  outcome adds nothing. Nothing is remembered between process runs except the stored rows, so a restart simply
  re-derives the holes.
- **Errors:** a page that fails (network, `SourceError`, parse error, write failure) is retried at most 3 times in
  the same process run with doubling backoff (1, 2, 4 rounds). After that its series' catch-up is suspended until
  restart and logged. A `429`/`418` pauses all pages (P-10).
- **Progress per cycle** re-derives the holes from the stored rows at the start of every idle window, like the
  candle backfill [C S16], so a hole filled by a round is never requested again.
- **Bound:** a fresh 29-day catch-up is 17 pages per series: 170 pages for 10 symbols (one idle window at the
  378 share), 10 200 pages for 600 symbols (about 5.7 hours at the 148 share). Rounds keep the newest data current
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
backfill_days = 29           # integer, validated 1 <= value <= 30 (A-4): the retrieval horizon H of P-5
symbols = "all"              # A-6: the string "all", or a non-empty list such as ["BTC/USDT:USDT", "ETH/USDT:USDT"]
```

Absent means disabled, so existing configuration files and `config/quasar.compose.toml` keep working. Inside the
table every key is required and unknown keys are an error (S20 rule) [C `config/settings.py`]. The key `symbols`
(amendment A-6, decision D-1) is required and fixes the open interest symbols: `"all"` is every symbol of the venue
(the behaviour before A-6), a list is exactly those symbols. The list is stated in the file and never derived at run
time; its order does not matter (symbols are sorted for the rounds). `S` in P-10 is the number of open interest
symbols. The cadence `c` is derived, not configured (P-10). Validation at start: `budget_per_5min` an integer in `[100, 400]`; **`backfill_days` an integer in `[1, 30]`: a larger value is a configuration error, the process exits with code 2 and starts nothing, also when the table is disabled (A-4)**; `c(S, budget_per_5min) ≤ 12`
(at most 2 976 symbols at 400, 1 176 at 200 and 432 at 100); `round_offset_ms` in `[60 000, 280 000]`; **`symbols` the
string `"all"` or a non-empty list of strings without duplicates, each exactly (case-sensitive) a symbol of the
configured venue, because open interest is never collected for an instrument without candles; any other value (another
string, an empty list, a number) or a missing key is a configuration error, the process exits with code 2, also when
the table is disabled** (as for `backfill_days`). A failed check names the key and the computed values. Removing a
symbol from the venue list therefore requires removing it from this list too, or the processes do not start. A file
that enables or contains `[open_interest]` and lacks `symbols` stops both processes at start; both shipped files add
the key.

*Shipped subset (A-6, decision D-3).* The shipped files list the **200 symbols with the largest 24 h quote volume**,
chosen once from the public Binance ticker (no credentials) and written into both files, never derived at run time
(`S = 200` gives `c = 1` at `b = 400`, P-10). The list itself is the `symbols` array of the files; the revision
history (A-6, item 2) records the snapshot time and the rule.

*Symbols that leave the list.* Their stored series stay (data is never deleted). They are no longer requested and the
backfill plans no holes for them (P-12); `GET /v1/open-interest` and its `/coverage` keep answering `200` for them,
and their slots after the newest row become `due`, then `stale`, judged with the list's rule. `/health/ingest`
`open_interest` keeps counting them (`scope: "stored_series"`) with `configured: false`; the additive `collected`
object (P-11, contract 6.5.8) judges only the listed series.

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
| Rate limiting | `c(S, 400)` for S = 1, 149, 150, 248, 249, 300, 522, 600, 2 976 is 1, 1, 1, 1, 2, 2, 3, 3, 12 (A-6; the values before A-6 for 150, 300, 522, 600 were 2, 3, 4, 5); S = 2 977 is refused | OI-1, A-6 Task 1 |
| Rate limiting | lower budgets: `c(10, 100) = 1`, `c(36, 100) = 1`, `c(37, 100) = 2`, `c(432, 100) = 12`, S = 433 at b = 100 refused; `c(600, 200) = 7` (A-6; 9 before); the backfill share is ≥ `b − floor(3b / 4)` in every accepted case | OI-1, A-6 Task 1 |
| Rate limiting | `budget_per_5min` 99, 401, 0, -1, 250.0, `"400"` and a missing key are config errors naming the key; 100 and 400 are accepted | OI-4 |
| Rate limiting | the limiter built from `b = 100` never allows more than 100 starts in any 300 s, with rounds, retries, time and backfill all competing (fake clock) | OI-3/OI-4 |
| Rate limiting | the API and the Collector derive the same `c` from the same config file, for b = 100, 200, 400 | OI-4/OI-5 |
| Rate limiting | every symbol is asked exactly once per `c` rounds, and no 5-minute window exceeds `ceil(S / c) + min(50, ceil(S / c)) + 2` round, retry and time starts (fake clock, S = 600, b = 400 and S = 600, b = 200; the bound before A-6 was `2 × ceil(S / c) + 2`) | OI-4, A-6 Task 3 |
| Late publication | ideal run (no errors, instant writes), `c = 5`: a row published after its symbol's request is stored by the symbol's next request, within the planned `c × 5 min + 200 s + p` (A-6) | OI-4, A-6 Task 5 |
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
| Rate limiting (A-6) | exhaustive over every integer `b` in `100..400` and every `S` from 1 to the largest accepted plus 1: `c` is minimal for `round_cost(S, c) ≤ steady(b)`; `1 ≤ c ≤ 12` and `CadenceError` exactly above the closed-form largest `S`; the new `c` never exceeds the one of the pre-A-6 rule (kept in the test as the oracle); the backfill share ≥ `b − steady(b)` ≥ 25; `round_cost` plus share equals `b`; `c` is non-decreasing in `S` and non-increasing in `b`. Stays in the default suite | A-6 Task 1 |
| Rate limiting (A-6) | atomic reserve: 200 symbol coroutines fail at the same instant on the fake clock with a source that yields between the limiter grant and the answer → exactly 50 tickets taken, at most 50 retry requests reach the source, 150 symbols end `retry_reserve_exhausted`; `try_take_retry` returns `True` 50 times then `False`, never decrements, is not a coroutine function, a ticket of a retry cancelled at the deadline stays taken, a new round starts at 0 | A-6 Task 3 |
| Rate limiting (A-6) | with `n = 200` and 60 injected request errors exactly 50 retries start and 10 symbols end `retry_reserve_exhausted`; with 10 errors all 10 are retried; a `429` pause, a refusal and an absent row take no ticket; a write `busy` error takes one; the `oi-round` line reports `retry_reserve_exhausted=10` | A-6 Task 3 |
| Pacing (A-6) | with the budget counted in the source and the backfill share saturated: after an on-schedule, on-pace previous round the next round's starts wait at most 5 s; after a slow one (span above `pace_limit_ms(n)`) the backfill gate starts no page, the next round competes only with the previous round's own starts (which began 300 s earlier) and its starts never wait; a slow round followed by a healthy network does not make the round after it slow; `pace_limit_ms(200) = 60 000`, `pace_limit_ms(131) = 39 600`; the `oi-round` line shows `span_ms` and `pace=ok` or `pace=slow` by the same rule, and the window after a late or slow round ends `end=previous-round` when it stopped only because that round was counted in full (as in the tested scenarios); other refusals keep `end=budget` | A-6 Task 4, final fix wave |
| Contention (A-6) | the hourly measurement line carries `request_ok_p50_ms`, `request_ok_p99_ms`, `request_ok_max_ms`, `request_failed_p50_ms`, `request_failed_p99_ms` and `request_failed_max_ms`: the CCXT history call alone, timed in the adapter after the slot, budget and pause waits (decision D-6) | A-6 Task 4 |
| Late publication (A-6) | latency simulation on the fake clock at `S = 200`, `b = 400`: each line of the table under P-10 "R1 latency" has a test asserting its bound, or for a "missed" line that the row is readable only after `e + 600 s`, with a met / missed pair at each threshold; the late path is exercised for the last symbol in sorted order | A-6 Task 5 |
| Configuration (A-6) | `symbols` missing, `""`, `"ALL"`, `[]`, `[1]`, a duplicate, a symbol not in the venue, a symbol in another case → config errors naming the key; `"all"` and a list accepted; checked also with `enabled = false`; the cadence error uses the list's `S` | A-6 Task 2 |
| Rate limiting (A-6) | the Collector and the API derive the same `c` and `expected_after_ms` from a file with a list | A-6 Task 6 |
| Subset (A-6) | only listed symbols are requested in rounds and backfill (the CLI wiring builds the task from the list); an unlisted venue symbol with stored rows answers `200` with `due`/`stale` after its newest row and appears in `behind` with `configured: false` | A-6 Task 3, Task 6 |
| Health (A-6) | `collected`: 200 listed series and 322 retired, stale series → the outer object unchanged (`behind` full of retired series, `status: "behind"`) and `collected.status: "current"`; one listed series stale → first entry of `collected.behind`, `collected.status: "behind"`; a listed symbol without a row → `never_stored: 1` and `series_count + never_stored = symbol_count`; a conflict on a retired series counts only in the outer object; with `symbols = "all"` the outer fields are byte-identical to a golden captured from `main` before A-6 on the same seeded database, the same `as_of_ms` and the same file without the key; with `"all"`, `collected` equals the outer counts when every stored series has the configured venue, a venue symbol and the configured period | A-6 Task 6 |
| Instruments (A-6) | `open_interest_period` is `"5m"` for listed symbols on every timeframe entry, `null` for the others, `null` everywhere when `[open_interest]` is disabled or absent; the other fields and the order are unchanged | A-6 Task 7 |
| Overview (A-6) | an instrument with `open_interest_period: null` shows "not collected", is not ranked or coloured as a problem, gets no open interest coverage request, and the candle summary's state keys are unchanged; the symbol detail view shows "not collected" instead of handling `unknown_series` | A-6 Task 9 |

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
budget" (evidence: the rate limiting tests). Amendment A-6 adds INV-146 to INV-151 (the shared reserve, the listed
symbols only, atomic tickets, `collected`, `open_interest_period`, the pace rule) and extends INV-087 and INV-090 to
the key `symbols` and the new formula.

## Consequences

- One additive schema version bump with an announced API outage and no rollback by redeploy (P-14).
- Open interest coverage can have holes that candles do not: omissions Binance documents, and anything older than
  29 days that was never stored (P-7, P-12).
- Candle contracts, tables, schedule and limits do not change. Open interest takes its own client, budget, writer
  connection and time slot (P-9).
- At the architecture's 600-symbol target each symbol is asked every 15 minutes (`c = 3`, A-6; `c = 5` before); the
  stored data stays at 5m and the planned latency in an ideal run is up to about 18 minutes, `1 100 s + p` (P-10).
  No latency is guaranteed; the read side reports what is not yet stored. Up to 248 symbols keep `c = 1` at `b = 400`. A lower
  `budget_per_5min` raises `c`.
- With the shipped subset of 200 symbols (A-6) `c = 1`: a row published within 200 s is stored by about `e + 250 s`
  and a late-published row by about `e + 550 s` in an ideal run, estimated and labelled as such (P-10, "R1 latency").
  Rounds that start late, slow failures, a `429` pause and long writer waits can miss `e + 600 s`; the consumer fails
  closed on those rows.
- The open interest symbols are a list in the file (P-13). Removing a symbol from the venue list requires removing it
  from this list too. Series of symbols that leave the list stay stored, go `stale` and are no longer collected; the
  `collected` object keeps health about the collected symbols readable (P-11).
- The reserve is shared, so under a broad outage the symbols early in sorted order are retried and the rest wait for
  their next request or the backfill; after a slow round the next idle window gets no backfill (P-10).
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
| O-5 | Latency at scale: is a planned latency of up to ~28 minutes (ideal run, no guarantee) acceptable at 600 symbols and `b = 400` (`c = 5`)? The alternative is a higher budget share than 40 % (O-2). | accept (P-10). **Answered by A-6 (2026-11-16):** the 40 % share is kept; the shared retry reserve lowers `c` to 3 at 600 symbols (up to `1 100 s + p`, about 18 minutes), and the request for a row stored by `e + 600 s` is met in an ideal run by a configured subset of 200 symbols with `c = 1` (estimated, P-10 "R1 latency"; P-13), not by a higher budget |

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
- **Amendment A-3** (approved by the user on 2026-11-03 as ADR-0004 O-6, applied on 2026-11-04 by the FR-5 and A-3
  branch, issue #21): P-10 point 2 said a `429` pauses "all open interest requests" and a `418` stops open interest,
  with a gate private to this dataset. Funding shares the IP, so a `429` seen by funding must also stop open interest
  and the reverse. The gate is replaced by `ExtensionPause` (`collector/extension_pause.py`): one instance per
  `quasar-collector run` process, passed to both datasets' sources, consulted before every extension request and set
  by whoever receives the answer; the doubling counter is shared. What does not change: the pause rule (at least
  300 s and the `Retry-After`, doubling to 1 h, a `418` for the process run), each dataset's own request budget and
  limiter (no shared token bucket), the schedule, the tables, the contract; candle collection neither reads nor is
  paused by it. Observable differences: a `429` or `418` log line of the open interest task names the shared pause
  (`oi-warning round-paused ... paused (429 seen by an extension dataset)` also appears when funding set the pause and
  open interest sent nothing), the task's fatal line names `ExtensionStopped`, and an open interest `429` right after
  a funding `429` with no success in between pauses 600 s. `fetch-oi-once` creates its own pause. INV-082 and INV-132.
- **Amendment A-4** (2026-11-04, by the user's explicit instruction "chci to konfigurace max. hodnota 30 jinak to spadne",
  made in the branch of FR-6 and delivered with its pull request): the retrieval horizon `H` of P-5 was the constant
  `RETRIEVAL_HORIZON_MS` of 29 days. It is now the required key `backfill_days` of `[open_interest]`, an integer in
  `[1, 30]` (P-13). A value outside the range is a configuration error: `quasar-collector run` and `quasar-api serve`
  exit with code 2 and start nothing, also when the table is disabled. The shipped files use **29**, the earlier
  value. The Collector plans holes and requests only within `H` (P-12); the API derives `rule.retrieval_horizon_ms`
  from the same key, so the states `no_longer_recoverable` and the contract's `retrieval_horizon_ms` follow the
  file (contract 6.5.7); the adapter refuses any request older than 30 days. What does not change: the slot states, the
  cadence, the budget, the schema, the endpoints. **A value of 30 sits on the edge** Binance was observed to accept
  (E-4: 30 days accepted, 31 rejected, and the edge may vary with the server clock): a refused request is a failed
  page and is retried, so use 30 only knowingly. Existing configuration files that enable open interest must add the
  key (every key is required, P-13); `config/quasar.toml` and `config/quasar.compose.toml` do.
- **Amendment A-5** (2026-11-14, approved by the user together with ADR-0005, row readability):
  1. **Stamp.** One stamp per page (`open_interest_task.py:805`, `fetch_open_interest_once.py:131`) is replaced by
     one stamp per transaction. The writer takes it after `BEGIN IMMEDIATE`, and the caller no longer passes
     `ingested_at_ms`. The rows of a split page therefore no longer share a stamp taken before the first transaction
     (ADR-0005 P-3).
  2. **Not stored.** A transaction older than `B − M` at `COMMIT` is rolled back and restamped. After the retry limit
     the writer raises a new "not stored" error, which the task treats as a failed page and retries as today. It is
     never fatal.
  3. **P-9 isolation.** After each committed data transaction the writer takes the lock a second time for a
     single-row confirmation. A candle write therefore waits for at most one open interest transaction **plus its
     confirmation**, not one transaction. The P-9 benchmark (`make bench-oi-write`) measures the pair (ADR-0005 P-6,
     RB-4).
  4. **P-6 schema.** `open_interest` and `open_interest_conflicts` gain the registration trigger of schema version 4
     (ADR-0005 P-2). The deployment follows P-14, schema 3 → 4.
  5. **Unchanged:** the identity, the eligibility, the slot states, the cadence, the budget, the endpoints. The
     contract gains `readable_by_ms` and `readable_by_unconfirmed` per row (contract §4.1, §6.5.4).
- **Amendment A-6** (2026-11-16, approved by the user; request `docs/requests/2026-11-16-quasar-fresh-open-interest.md`
  from Sentaris Engine: R1 a row stored by `T + 600 s` in an ideal run with `c = 1`, R2 keep `rule.cadence` and
  `rule.expected_after_ms`; design `docs/superpowers/specs/2026-11-16-adr-0003-a6-fresh-open-interest-design.md`,
  revision 4; plan `docs/superpowers/plans/2026-11-16-adr-0003-a6-fresh-open-interest.md`). The user stated that
  nothing else on the Collector's IP calls `/futures/data/*`, chose a shared retry reserve plus an explicitly
  configured symbol subset, and kept the budget share (at most 400, 40 %, O-2), the backfill guarantee, deterministic
  configuration and the existing schema. Decisions of 2026-11-16: **D-1** approved, the required key
  `symbols = "all" | [...]`; **D-2** revised by the user, the retired-series masking of open interest health is
  resolved in A-6, keeping historical data and existing contract semantics, by the additive `collected` object;
  **D-3** approved, 200 symbols by 24 h quote volume;
  **D-4** approved, `R = 50`; **D-5** the overview tool reads the API field `open_interest_period`, not the file;
  **D-6** no change of the CCXT request timeout, the durations of the CCXT history calls (`request_ok_*_ms`,
  `request_failed_*_ms`, without slot, budget or pause waits) are measured in the hourly measurement line and the
  timeout is revisited after the soak; **D-7** the pace rule is adopted.
  1. **Shared retry reserve.** The per-symbol reserve `n` is replaced by `min(50, n)` per round, handed out as atomic
     tickets; a symbol without a ticket ends `retry_reserve_exhausted` and waits for its next request. `c` is never
     above the old one (and differs only where `n > 50`); the largest `S` is 2 976 at 400 (was 1 788), 1 176 at 200 (888), 432 at 100 (unchanged); the
     backfill share is `b − round_cost(S, c)` (P-10, P-12, P-13).
  2. **Open interest symbols.** `[open_interest].symbols` (`"all"` or a list) is required; `S` is its count; the
     rounds and the backfill request only these symbols (P-13, P-12). The shipped files list 200 symbols by 24 h
     quote volume. Recorded when the files were written (2026-11-16): the snapshot time is 2026-11-16T22:05Z (UTC);
     the rule is the 200 linear USDT perpetual swaps of the venue list with the largest 24 h `quoteVolume` of one
     public `fetch_tickers` call (`binanceusdm`, no credentials), tickers mapped to symbols through the CCXT markets,
     ties broken by symbol, listed sorted in both files; the list itself is the `symbols` array of the files.
  3. **Planned latency** includes the round position `p`; the ideal run assumes the round starts at its wake time;
     `expected_after_ms` and `rule.cadence` do not change. Measured and estimated figures and the table of
     scenarios, with the missed and unbounded lines, are in P-10 ("R1 latency").
  4. **Pace rule (D-7).** The backfill gate counts a previous round in full also when its span exceeds
     `pace_limit_ms(n) = ceil(n / MAX_IN_FLIGHT) × 600 ms`; the allocation of P-10 is unchanged.
  5. **API (additive).** `/health/ingest` `open_interest` gains `collected` (the listed series only) and
     `/v1/instruments` gains `open_interest_period`; the value of `behind[].configured` follows the list. With
     `symbols = "all"` the outer health fields equal those of a build before A-6 reading the same file without the key
     wherever the cadence `c` is unchanged (for example when `ceil(S / c) ≤ 50`; `c` can also be unchanged with more
     than 50 symbols per round, for example `S = 200` at `b = 200`); `c`, `rule.expected_after_ms` and the states
     derived from them follow the new formula otherwise (for example `c` goes from 4 to 3 at the 522 venue symbols of
     the shipped files at 400) (contract 6.4.2, 6.5.7, 6.5.8).
  6. **Unchanged:** the schema (no migration), the row identity, the eligibility, the slot states, `readable_by_ms`,
     the endpoints and their fields, the candle path, funding rate, `ExtensionPause`, the budget range `[100, 400]`,
     and `fetch-oi-once` (it takes `--symbol` and never reads the list). New invariants INV-146 to INV-151.
  7. **Release.** With the next open interest release, together with funding rate. Both processes read the same
     file (the existing `sha256` check); the new file must be in place before either restarts, because both exit with
     code 2 on an `[open_interest]` table without `symbols` (README, "Deployment (S21)"). No schema change, no
     migration. A deployment that already collected open interest for every venue symbol (v0.2.0 ships the table
     enabled without `symbols`: 522 symbols) keeps those series: after A-6 the 322 unlisted ones are retired, the outer
     `open_interest` fields count them (`status` stays `behind`), and consumers that need the listed universe read
     `collected`.
