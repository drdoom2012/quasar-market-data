# ADR-0004: Funding rate history (Binance USDT-M)

- **Status:** Accepted (approved by the user on 2026-11-03: revision 4 as written, including the proposals for the open
  decisions O-1 to O-6). Revisions 2 to 4 answer the user's reviews (listed at the end). Nothing is implemented yet; the
  API response schema is a new contract section 6.7 that must be approved before FR-5. Amendment A-3 to ADR-0003 (O-6) is
  approved as a proposal here; the text of ADR-0003 has not been edited. The user asked for the spec on 2026-11-03 and
  opted out of a requirements interview, so the choices were proposals until this approval.
- **Date:** 2026-11-03
- **Scope:** collecting, storing and serving the **funding rate history** that Binance publishes for USDT-M perpetuals:
  one record per funding settlement, with the rate, the settlement time and the mark price. It is the second extension
  dataset (`docs/architecture.md` §7, after open interest). The candle and open interest contracts, tables, schedules
  and guarantees are not changed. Liquidations, order books and streaming stay out of scope.
- **Related:** ADR-0001 (time trust P-4, decimal grammar P-7, venue and symbol P-8, market check P-9), ADR-0002
  (schema governance P-9, immutability P-4, conflicts P-5, migration P-8), ADR-0003 (the isolation, writer and
  deployment rules that this ADR reuses by reference; one amendment, A-3, is *proposed* here and not applied),
  `docs/contracts/api-v1.md`, `docs/follow-ups.md` F-8.

## How to read this ADR

Every factual statement carries its source:

- **[M]** measured in this project's probe on 2026-11-03 (Part 3, E-n), with its sample size;
- **[D]** documented by Binance (Part 3, E-1); not verified by us unless also [M];
- **[C]** a property of this repository's current code or of the pinned CCXT 4.5.89, checked on 2026-11-03;
- **[P]** a design proposal or assumption. A [P] is never evidence.

Part 1 is the request. Part 2 holds the proposals that need approval. Part 3 is the evidence.

**Notation.** All times are integer milliseconds. `[a, b]` is a closed interval of integers; it is empty when `a > b`.
Two intervals **touch** when they overlap or when one starts exactly one millisecond after the other ends; the union of
two touching intervals is an interval. `m = floor(b / 2)` is the round share of the budget (P-9).

## Context

"Funding fees" are two different things, and only one of them belongs in this service:

| Thing | Where it lives | In scope |
|---|---|---|
| **Funding rate history**: the rate Binance applied at each settlement of a contract, public market data | `GET /fapi/v1/fundingRate` [D] | **yes** |
| **Funding payments**: what an account paid or received, which depends on its position size | private account endpoints (`fetchFundingHistory` in CCXT) | **no**: they need credentials, and `CLAUDE.md` forbids private credentials |

A consumer that wants the cost of a position multiplies the rate of each settlement by the position's notional value at
that settlement. This service supplies the rates and when they apply; it does not compute costs (NG-1).

What makes funding different from candles and open interest, and shapes every proposal below:

1. **There is no time grid.** A settlement happens every 8, 4 or 1 hour depending on the symbol, and the interval of one
   symbol changed up to eight times in the sampled history [M E-5]. The current interval is exposed
   (`GET /fapi/v1/fundingInfo`), the past ones are not. Absence of a row therefore cannot be detected from the
   stored rows alone: a missing 4h settlement looks like a legitimate 8h one. Coverage needs another mechanism (P-7).
2. **History does not expire.** BTCUSDT answers back to 2019-09-10 [M E-3]. Nothing becomes unrecoverable, so there is
   no retention horizon and no deadline pressure in backfill.
3. **The value can be negative**, which the existing decimal checks of the database do not allow (P-6).
4. **Two kinds of record share a settlement hour**: `Regular` and `Special` (an extra charge for stock dividends on
   TradFi perpetuals) [M E-6].
5. **An unknown symbol answers `200 []`, and an invalid request answers `200` with an error object** [M E-4]. Fail
   closed needs explicit guards (P-5, P-7).

## Part 1 — Request

| ID | Request |
|---|---|
| R-A | Prepare the spec for funding fees alongside the candles and open interest (user, 2026-11-03). |
| R-B | The user said to start without a requirements interview. Every choice below is a proposal; the interpretation of "fees" as the public rate history is mine (see Context). |
| R-C | Constraints inherited from `CLAUDE.md` and ADR-0001 to ADR-0003: Collector and API stay independent, SQLite is the boundary, exact decimal text, immutable storage, conflicts recorded and never overwritten, no fabricated rows, fail closed, restart-safe and idempotent, candle collection never delayed by the new dataset, no private credentials. |
| R-D | Review of revision 1 (user, 2026-11-03): measured evidence instead of placeholders; a formal verified span; a deterministic scheduler; separation of local and shared rate-limit guarantees; the status of the timestamp check; window-level API coverage; more tests; checked capacity maths. Constraints: no architecture rewrite, ADR-0001 to ADR-0003 principles and the slices FR-1 to FR-5 kept, no production code, no invented measurements, no new abstraction without a concrete benefit. |

**Non-goals.**

- NG-1 No funding cost, annualized rate, average or sum. The API returns rows; consumers compute.
- NG-2 No prediction: `lastFundingRate`, `nextFundingTime` and the estimated rate of `GET /fapi/v1/premiumIndex` are not
  collected (they are the current, unsettled estimate, not history, and the endpoint is weighted in the klines budget).
- NG-3 No interval table, caps or floors (`fundingInfo`): P-7 explains why coverage does not need them.
- NG-4 No WebSocket mark price stream, no other venue, no account funding payments.

## Part 2 — Proposals

### P-1 Source: the history endpoint, raw

Use `GET /fapi/v1/fundingRate` through `fapiPublicGetFundingRate` on a CCXT client of the Binance USDT-M source.
Values stay the strings Binance sends. The unified `fetch_funding_rate_history` is not used: it converts `fundingRate`
with `safe_number` (a float) and drops `markPrice` and `rateType` [C ccxt 4.5.89 `parse_funding_rate_history`; E-12].

*Rejected:* `fundingInfo` and `premiumIndex` as sources of history (NG-2, NG-3); sampling the current rate ourselves
(it cannot be backfilled and would be our own dataset).

### P-2 Fields and units

| Raw field | Stored as | Meaning [D E-1] | Unit |
|---|---|---|---|
| `fundingTime` | `funding_time_ms` | the time of the settlement, UTC ms; the page gives no definition beyond the name | UTC ms |
| `fundingRate` | `funding_rate` | the funding rate | a fraction per settlement (`0.0001` = 0.01 %), inferred [M E-2] |
| `markPrice` | `mark_price` | "mark price associated with a particular funding fee charge" | quote asset (USDT), inferred |
| `rateType` | `rate_type` | `Regular`, or `Special` "for the additional funding rate generated by stock dividends" | — |
| `symbol` | checked, not stored | market id | — |

- `funding_rate` is **signed** canonical decimal text (ADR-0001 P-7: trailing fractional zeros removed, no rounding; a
  zero is `0`, never `-0`; `canonical_decimal` already does both [C `domain/decimal_text.py`]). The raw
  `0.00010000` is stored as `0.0001`.
- `mark_price` is canonical decimal text **or NULL**. Binance sends `""` for every row settled before 2023-10-31 08:00
  UTC and a decimal after it [M E-7]. `""` is stored as NULL; a price is never computed, copied from a candle or
  filled in. Open decision O-4.
- The **length of the interval a rate applies to is not part of the row and is not stored.** `funding_rate` is per
  settlement, not per day or per year. A consumer must not assume 8 hours; the spacing between two consecutive rows is
  an observation, not a definition, and is wrong across a gap.
- Limitation: Binance does not document the units. They are inferred from the values [M E-2], like ADR-0001 P-2.

### P-3 Identity, time, and what is a contract and what is a heuristic

Identity is `(venue, symbol, funding_time_ms, rate_type)`.

- `funding_time_ms` is **stored as Binance reports it**, not rounded to the hour. Rounding would be our invention and
  would merge a Regular and a Special record of one hour.
- `rate_type` is part of the key because both kinds occur at the same hour of the same symbol with different
  `funding_time_ms` [M E-6], and nothing says they never coincide. With a key without it, a coincident pair would be
  recorded as a false conflict. Open decision O-2.
- A consumer joins a funding row to a candle by the hour it falls in, never by equal timestamps. The API does not do
  that for them.

**Hour alignment is a heuristic, not a contract, and is not a rejection criterion** (decision on review item 5).
What the parser may rely on:

| Property | Status | Use |
|---|---|---|
| a response is a list of objects with the five keys; ascending; `startTime` and `endTime` inclusive; at most `limit` rows | [D] (ascending, inclusive, `limit`) and [M E-4] | parse error if violated |
| `fundingTime` is an int inside the requested `[startTime, endTime]` | follows from the inclusive bounds [D]; also catches a unit error (seconds instead of milliseconds lies far outside any request) | parse error if violated |
| the key `(fundingTime, rateType)` is unique within a response | [M E-3, E-13: no duplicate in any sampled series]; without it a repeated key cannot be told from a conflict | parse error if violated |
| `rateType` is `Regular` or `Special` | [D] | parse error if violated (an unknown kind cannot be stored under the schema check and its meaning is unknown) |
| `fundingTime` lies within 5 s after a whole hour | **observed only**: 0 to 26 ms after the hour for 707 850 Regular rows of the last year over 938 symbols, 0 to 47 ms for 86 870 older ones, at most 1 003 ms for the 90 Special rows, none beyond 5 s, none before the hour [M E-6, E-13]; Binance does not document it | **never rejected**; a row outside the range is stored, counted as `off_hour_rows` in the hourly log line and logged at warning level |

Reasons for not rejecting on alignment: the identity, the span arithmetic (P-7), the eligibility rule (P-4) and the
paging rule (P-5) use exact milliseconds and need no alignment; the range check already catches every gross timestamp
error; and a hard rejection would turn one unusual but genuine settlement into a response that can never be accepted,
which stalls that series' span (`behind` in health) until a human acts. The cost of accepting is nil for correctness.
The 5 s of revision 1 had no source and is removed.

### P-4 Eligibility: when a row may be stored

A row with settlement time `T` is eligible iff `T + publication_delay_ms ≤ server_lower_bound` (ADR-0001 P-4 bound and
time-trust gates; untrusted time stores nothing).

- `publication_delay_ms`, proposed **30 000 ms** [P]. Observed: for the 20:00 UTC settlement the row was present at the first request after it for all three symbols polled, 2.3 to 3.4 s after the settlement, and 12 earlier requests (1.1 to 14.5 s before it) returned no row for it [M E-10]. That is one settlement, three symbols and a resolution of about 4 s: **thin evidence, not a distribution**. 30 s is about nine times the largest observed value and is chosen for margin. It is **not a proven bound**; a row published later than the delay would appear inside an already verified span and be reported as a late event (P-8).
- The same quantity bounds what a verified span may claim (P-7): the end of every forward request is
  `b = server_lower_bound − publication_delay_ms`, so a settlement that is not yet eligible is never inside a verified
  span, and every row of such a response satisfies the rule. A row beyond `b` cannot occur in a valid response (it is
  outside the request); the eligibility selection stays as a second guard (INV-071 style) and must drop nothing.
- Backward history requests end before the existing span and need neither a time observation nor a clock: everything
  they cover is older than a point that was already eligible, and their lower bound comes from the stored span (P-10).

### P-5 Request and paging rules

Request behavior [M E-4]:

1. Binance returns the **oldest** `limit` rows of a range, ascending (the opposite of open interest).
2. `startTime` and `endTime` are both inclusive.
3. `limit` up to 1000 is accepted when `startTime` is sent. Without `startTime` the answer is capped at 500 rows
   (default 100), although the page says "the most recent 200" [D, M].
4. An invalid parameter (`limit` 1001, a non-numeric `startTime`) answers **HTTP 200 with an error object**, not a list.
5. An unknown symbol answers `200 []`. `startTime > endTime` answers `200 []`.

Rules [P]:

- Every request sends `symbol`, `startTime`, `endTime` and `limit=1000`. A request without `startTime` is never made.
- A symbol is requested only if the adapter has verified its market as an active USDT-margined linear perpetual (the
  ADR-0001 P-9 check, INV-035), so rule 5 cannot turn a typo or a delisted contract into an empty "answer".
- The whole response is a parse error (fail closed, nothing of it stored, nothing verified) in the cases of the
  **contract** rows of P-3: not a list (this includes the error object of rule 4); a row outside `[startTime,
  endTime]` or with a non-int `fundingTime`; a repeated key; a `fundingTime` that decreases (a Regular and a
  Special row of one `fundingTime` are accepted in either order; wording amended 2026-11-04 with the user's approval,
  see the Amendment note below); `symbol` differs from the request; `fundingRate` not a string in the decimal grammar; `markPrice`
  neither `""` nor a non-negative decimal string; `rateType` neither `Regular` nor `Special`; more than `limit` rows.
- **Page coverage** (the interval one valid response verifies; request `[a, b]`, `n` rows, `last` the `fundingTime`
  of the last row):
  - `n < limit`: the page covers `[a, b]`. An empty answer covers `[a, b]` too.
  - `n = limit`: the page covers `[a, last − 1]` and the range continues with `a' = last` (the row at `last` is fetched
    again and is an idempotent no-op). This is correct whatever the order of two rows with the same `fundingTime`
    (a Regular and a Special one could in principle share it), which `last + 1` would not be. It always makes
    progress: keys are unique and `rateType` has two values, so 1 000 rows span at least 500 distinct
    milliseconds and `last > a`.
  - A range holding `r` rows needs `ceil(r / 999)` requests; exactly 1 000 rows in a range cost one extra request that
    returns one row (`r = 999`: 1 request, `r = 1 000`: 2, `r = 1 998`: 2, `r = 1 999`: 3).
- Pages of one range are consecutive: page `i + 1` starts at the `a'` of page `i`. Their covered intervals touch, so
  the union of the pages of a range that ended with `n < limit` is exactly the requested `[a, b]`.

### P-6 Schema version 3

One new migration, `003_funding_rate.sql`, following ADR-0002 exactly (STRICT, natural key, `WITHOUT ROWID`,
immutability triggers, conflicts table). The value check is a **signed** variant of the existing `<decimal-shape>`
(`-` allowed as the first character, `-0` and a bare `-` refused); the non-negative one of migrations 001 and 002 is
not reused because it would reject every negative rate. The v1 and v2 tables are not touched. (Schema version 2 is
open interest; this ADR therefore needs version 3 and is shipped after it.)

```sql
CREATE TABLE funding_rates (
  venue           TEXT    NOT NULL CHECK (length(venue) BETWEEN 1 AND 32),
  symbol          TEXT    NOT NULL CHECK (length(symbol) BETWEEN 1 AND 64),
  funding_time_ms INTEGER NOT NULL CHECK (funding_time_ms BETWEEN 946684800000 AND 4102444800000),
  rate_type       TEXT    NOT NULL CHECK (rate_type IN ('Regular', 'Special')),
  funding_rate    TEXT    NOT NULL CHECK (<signed-decimal-shape>),
  mark_price      TEXT             CHECK (mark_price IS NULL OR <decimal-shape>),
  ingested_at_ms  INTEGER NOT NULL,
  PRIMARY KEY (venue, symbol, funding_time_ms, rate_type)
) STRICT, WITHOUT ROWID;
-- + funding_rates_no_update / funding_rates_no_delete triggers (ADR-0002 P-4)

CREATE TABLE funding_rate_conflicts (
  conflict_id      INTEGER PRIMARY KEY,
  venue            TEXT    NOT NULL,
  symbol           TEXT    NOT NULL,
  funding_time_ms  INTEGER NOT NULL,
  rate_type        TEXT    NOT NULL,
  funding_rate     TEXT    NOT NULL CHECK (<signed-decimal-shape>),
  mark_price       TEXT             CHECK (mark_price IS NULL OR <decimal-shape>),
  first_seen_at_ms INTEGER NOT NULL
) STRICT;
CREATE UNIQUE INDEX funding_rate_conflicts_observation ON funding_rate_conflicts
  (venue, symbol, funding_time_ms, rate_type, funding_rate, COALESCE(mark_price, ''));
CREATE INDEX funding_rate_conflicts_event ON funding_rate_conflicts (venue, symbol, funding_time_ms);
-- + funding_rate_conflicts_no_update / _no_delete triggers

CREATE TABLE funding_rate_verified (
  venue          TEXT    NOT NULL CHECK (length(venue) BETWEEN 1 AND 32),
  symbol         TEXT    NOT NULL CHECK (length(symbol) BETWEEN 1 AND 64),
  from_ms        INTEGER NOT NULL CHECK (from_ms    BETWEEN 946684800000 AND 4102444800000),
  through_ms     INTEGER NOT NULL CHECK (through_ms BETWEEN 946684800000 AND 4102444800000),
  verified_at_ms INTEGER NOT NULL,
  CHECK (through_ms >= from_ms),
  PRIMARY KEY (venue, symbol)
) STRICT, WITHOUT ROWID;
CREATE TRIGGER funding_rate_verified_only_grows BEFORE UPDATE ON funding_rate_verified
  WHEN NEW.venue <> OLD.venue OR NEW.symbol <> OLD.symbol
    OR NEW.from_ms > OLD.from_ms OR NEW.through_ms < OLD.through_ms
    OR NEW.verified_at_ms < OLD.verified_at_ms
  BEGIN SELECT RAISE(ABORT, 'funding_rate_verified only grows'); END;
CREATE TRIGGER funding_rate_verified_no_delete BEFORE DELETE ON funding_rate_verified
  BEGIN SELECT RAISE(ABORT, 'funding_rate_verified is not deletable'); END;
```

- The unique index on conflicts uses `COALESCE(mark_price, '')` because SQLite treats NULLs as distinct in a UNIQUE
  index; `''` is not a valid canonical decimal, so it cannot collide with a price.
- No table for "exchange has no data" (the `unavailable_candles` of ADR-0002 P-6): nothing can be confirmed as such.
- **What the schema enforces about the span, and what it does not.** The triggers enforce that a span never shrinks and
  is never deleted or re-keyed. They cannot enforce that a growth is *adjacent* to what was verified, because a trigger
  does not see the page. Adjacency is enforced by the one SQL statement that may write the span (P-7, `merge`) and is
  tested negatively; the invariant register will say `ENFORCED` for monotonicity and name the single-writer function
  for adjacency, not claim more.
- Size estimate (not measured): 90 to 110 bytes per row; about 1 100 rows per year for an 8h symbol and 8 800 for a 1h
  symbol, so under 150 MB per year for the 659 USDT perpetuals of the exchange [M E-8].

### P-7 Coverage: the verified span (formal)

**The problem.** With no grid (Context 1) a stored row says nothing about its neighbours. Counting "missing slots" would
mean guessing the interval of every past moment, which Binance does not publish. A coverage based on guessed
intervals would be a fabricated fact.

**Definition.** Per series (venue, symbol) the table holds at most one **verified span** `[from_ms, through_ms]`, a
closed integer interval of settlement times. It states: *for every integer `x` in the interval, a valid response
(P-5) to a request whose range contained `x` was received, and all rows of that response are durably stored; the
responses together listed exactly the stored settlements of the interval and no other.* It is the exchange's list at
the time of asking (`verified_at_ms` is the latest such time). It is not a proof that the list is complete in the
world, and it can be contradicted later (P-8).

**Unit and success.** A *unit* is one page (forward requests, seeds) or the consecutive pages of one backward chunk
(below). Its covered interval `J = [a, e]` is the union of the covered intervals of its pages (P-5), which touch. A unit
is *successful* iff all of: every response of the unit passed the parser; the eligibility selection dropped no row;
every row transaction of the unit except the last is known to have committed; and the **final transaction** committed.
The final transaction contains the last chunk of rows of the last page (at most 100, ADR-0003 P-9) **and the `merge` of
`J`** in one `BEGIN … COMMIT`; if that page has no rows, it contains only the `merge`. Earlier row transactions are
separate and commit before the final one is begun (one writer thread, FIFO).

**`merge(J)`** is the only statement that writes the span:

```sql
INSERT INTO funding_rate_verified (venue, symbol, from_ms, through_ms, verified_at_ms)
  VALUES (:v, :s, :a, :e, :now) ON CONFLICT (venue, symbol) DO NOTHING;
UPDATE funding_rate_verified
   SET from_ms = min(from_ms, :a), through_ms = max(through_ms, :e), verified_at_ms = max(verified_at_ms, :now)
 WHERE venue = :v AND symbol = :s AND :a <= through_ms + 1 AND :e >= from_ms - 1;
-- then SELECT: the span must contain [:a, :e], otherwise ROLLBACK the final transaction and report
-- "not adjacent" (a planning bug or a state change); nothing of the final chunk is kept.
```

**Proof of the invariant** `complete = true ⇒ every part of the window was verified by a successful unit`.
Let `V` be the union of the covered intervals `J` of all successful units of the series.

1. *The span is inside `V` in every committed state.* Induction over committed transactions. The empty state has no
   span. Only `merge` changes the table (the triggers abort every other update, every delete and every replace). A
   `merge` of `J` commits only as the final transaction of a successful unit, so `J ⊆ V`. If a span `I ⊆ V` exists, the `WHERE` clause
   lets the update run only when `J` touches `I`, so `I ∪ J` is an interval and `⊆ V`; `min`/`max` give exactly that
   union. If no span exists, the `INSERT` creates `J ⊆ V`. A rejected `merge` leaves the table unchanged.
2. *Rows are durable before the span claims them.* The span growth for a page commits together with that page's last
   row chunk and after every earlier chunk has committed. A reader snapshot (WAL) that contains the growth therefore
   contains all rows of the page. A reader never sees a span without the rows it claims.
3. *`complete` needs the whole window inside the span.* The API sets `complete = true` only when `from_ms ≤ start`,
   `end ≤ through_ms` and no conflicted event lies in the window (P-13). By 1 every integer of `[start, end]` is in `V`.
4. *The statement depends on one function.* Step 1 relies on `merge` being the only writer and on its `WHERE`. The
   schema alone does not guarantee adjacency (P-6); the tests in P-15 do (a non-adjacent `merge`, a `merge` in a
   transaction that is rolled back, a span written by hand are all refused or detected).

**Crash, unknown outcome, restart.** The persistent state is the rows and the span; nothing else is a fact.

| Point of failure | State after | Consequence |
|---|---|---|
| after the response, before any row | nothing changed | the next planning repeats the request |
| after `k` of the earlier row chunks | `k` chunks stored, span unchanged | rows lie outside or inside the span without claiming more; the repeat finds them stored (no-op, same values, no conflict) |
| the final transaction fails or is cancelled, outcome certain | rows of earlier chunks stored, final chunk and span unchanged | as above |
| the final transaction's outcome is **unknown** (`COMMIT` error with the transaction state unknown) | atomic: both the last chunk and the growth, or neither | the page is **not** treated as successful in memory; the next planning reads the span and the rows from the database and decides; if the growth did commit, the repeat is a no-op (min/max are idempotent), if not, the page is repeated |
| the process ends after the commit, before in-memory bookkeeping | span grown | restart re-derives everything from the database |

Re-running a page any number of times converges to the same state: row inserts are `ON CONFLICT DO NOTHING` with a
read-and-compare (a differing value is a conflict, P-8), `merge` is a union.

**Forward and backward continuity.**

- *Forward* (rounds, catch-up after an outage): a request `[a, b]` with `a ≤ through_ms + 1`. Its pages are
  consecutive and each page's `J` touches the span grown by the previous page, so the span grows page by page.
- *Backward* (history): a request ending at `from_ms − 1`, so its covered interval touches the span from below
  **only when the last page of the chunk has been fetched**; a truncated chunk (`n = limit`) covers `[a, last − 1]`,
  which does not touch. Therefore a backward chunk is defined as **at most 90 days** (`HISTORY_CHUNK_MS`); the pages of
  the chunk are fetched consecutively and the union `[a, from_ms − 1]` is merged **once, with the last page's rows, in
  its final transaction**. The earlier pages' rows are stored but unclaimed. A crash loses only that in-memory
  progress (at most the pages of one chunk, 3 for a 1h symbol, 1 for an 8h one); the chunk is repeated
  idempotently. Because every chunk is merged on its own, history progress survives restarts and a crash loop cannot
  prevent it (each chunk that completes is kept).
- *Birth of a span*: a series without a span is **seeded** by a forward-style request `[b − recheck_ms, b]` whose
  first page creates the span; history then grows backward from there. A new symbol is therefore `current` after one
  request and its history arrives chunk by chunk (P-10).
- An **empty answer extends the span** (a quiet period is normal: an 8h symbol has no settlement in most hours;
  a young symbol has none before its listing). The guards against a wrong symbol are P-5 (market check) and the
  `quiet_series` health signal (P-13).

**What the API says** for a window `[start, end]`, defined over the whole window, not over a page (P-13):
`window_state` is `verified` when `from_ms ≤ start` and `end ≤ through_ms`, `partly_verified` when `[start, end]`
intersects `[from_ms, through_ms]` without being inside it, `unverified` otherwise (including no span);
`complete` is true only for `verified` with no conflicted event in the window.

| Says | Does **not** say |
|---|---|
| Binance listed exactly these settlements for the window when we asked | that Binance cannot add or revise a row later (P-8 detects it only if we ask again) |
| the window is older than we have collected (`unverified` before `from_ms`) | why: not yet backfilled, or not requested because of the configured depth |
| the window is newer than the last successful check (`unverified` after `through_ms`) | that nothing happened there |

*Rejected (kept as the alternative of O-1):* no span table; the API gives each row's `spacing_ms` and the consumer
decides. It adds no mutable table, but nothing can ever be called complete, which fails the "fail closed when data is
missing" rule for the main use (summing funding over a window).

*Deviation to note:* `funding_rate_verified` is the one table in this schema family that is updated in place. It holds
progress, not market data, and the trigger allows only growth. If the user prefers strict immutability, the
alternative is an append-only table with one row per merge, 1 to 24 rows per symbol per day (O-1).

### P-8 Integrity: conflicts, late events, and their effect

- A re-delivered row with the same key and different values is rejected and recorded in `funding_rate_conflicts`
  (`INSERT … ON CONFLICT DO NOTHING`); the stored row is never updated. A `mark_price` that was NULL and is now a
  price (or the reverse) is a conflict like any other difference. No resolution path in v1 (ADR-0002 Q3).
  Observed revisions so far: none in 86 870 rows rechecked after about an hour, and none in the 3 rows first seen at the 20:00 UTC settlement after 13 minutes [M E-11]; this is a short window and is no evidence about later revisions.
- **Effect:** the same as ADR-0003 P-8: one log line per newly recorded conflict, the row returned with its **first
  stored** values and `conflicted: true`, `conflicted_series` in `/health/ingest` and a `degraded` status for a conflict
  in the last 24 hours. A conflict does **not** block span growth (the rows are stored and the conflict is reported
  separately); it makes `complete` false for every window containing the event (P-13).
- **Recheck window.** Every round request starts `recheck_ms` (proposed 24 h) before `through_ms`. Re-delivered rows
  expose revisions; a row that is **new** and has `funding_time_ms ≤ through_ms` of the span as it was before the
  request is a **late event**: Binance listed it after we claimed the range was complete. It is stored (it is real
  data), logged at warning level with symbol and time, and counted in the hourly measurement line. The API does not
  mark it (it cannot tell it apart without another table); that is a known limit.

### P-9 Isolation, rate limiting and coordination

All of ADR-0003 P-9 applies unchanged in kind: funding runs as its own asyncio task started after the candle loop's
first round and post-check; any exception ends only that task; it uses its **own CCXT client**, its own request
limiter, its own writer connection with transactions of at most 100 rows that yield to the candle writer and a
`busy_timeout` of 1 000 ms; at most 2 requests in flight; reads on its own thread, one series at a time. Whether the
code is shared with open interest or written beside it is a plan-level decision, not an architectural one.

**The limit and what is known.** `fundingRate` shares "500/5min/IP" with `fundingInfo` [D E-1]. The responses carry no
used-weight header [M E-9], so a local counter is the only view of the usage. Not known: whether other endpoints share
the 500, whether the window is rolling or fixed, how Binance counts a request (at arrival?), the effect of repeated
`429`. The worst case is assumed.

**The local limiter (what this ADR can guarantee).** `b = budget_per_5min`, allowed **`50 ≤ b ≤ 250`** (250 is 50 % of
the documented 500), default **150**; any other value is a config error at start.

- *Window:* `Wl = 300 000 ms + G` with a guard `G = 5 000 ms` for network jitter between our send instant and
  Binance's count. `Wl = 305 000 ms`.
- *Rule:* a request may **start** at time `t` iff fewer than `b` requests started at times `s` with `t − Wl < s ≤ t`
  (left-open, right-closed). The server-time call counts as a start. Therefore request number `k + b` of a saturated
  stream starts exactly `Wl` after request `k`, never earlier.
- *Class caps inside the same window:* round and seed requests (class **R**, including retries and the round's
  server-time call) ≤ `m = floor(b / 2)`; catch-up and history requests (classes **C** and **H**, including their
  retries) ≤ `b − m`. The caps sum to `b`, so the total never exceeds `b`. R's share is never lent to C or H, and the C+H share is never
  lent to R (rounds never wait for backfill and backfill never takes a round's share). Inside the C+H share the two
  classes alternate (P-10, selection rule 2).
- *Guarantees (tested with a fake clock):* G1 in any interval of length `Wl` this task starts at most `b` requests;
  hence also at most `b` in any 300 s window, and in any fixed window of 300 s. G2 after a `429` no request starts for
  `max(Retry-After, 300 s)`, doubling on repeats up to 1 h; after a `418` none for the rest of the process run; a
  request that was waiting when the stop arrived sends nothing. G3 the candle `RateLimiter` is never acquired.

**What is not guaranteed.** The local limiter says nothing about: other processes or deployments on the same IP,
other datasets of this Collector (open interest has its own bucket documented as 1 000 per 5 minutes [D], candles are
weight-limited), how Binance counts (a fixed window aligned to its clock can let two bursts of `b` through if the
guard is smaller than the jitter), or hidden retries inside CCXT (the adapter issues each raw call once; that CCXT does
not retry a failed call itself is [P], to be checked in FR-3). The operational requirement (README, both
deployments): nothing else on the IP may call `fundingRate` or `fundingInfo` more than `500 − b` times per 5 minutes.

**Coordination between datasets on one IP: one shared pause, no shared token bucket.**

- The buckets are documented separately (funding 500, open interest 1 000) and no document says they are one. A
  combined ceiling would need a fact we do not have, so none is imposed; each dataset keeps its own limiter and its
  own bounded budget. The fact "the buckets are separate" is **unverified** (assumption H-1); if it is false the
  documented limits are still respected by each budget alone only while the sum of the two stays below the shared
  value, so FR-4 logs both budgets at start and the hourly line gives each dataset's peak starts per `Wl`.
- A `429` or `418` is an IP-level warning; continuing to send in another dataset risks the escalation that ADR-0003
  already names (a ban affecting klines is not excluded). Therefore the Collector holds **one in-process object**,
  `ExtensionPause` (a monotonic `paused_until` and a `stopped` flag, set by whoever receives the response), consulted
  before every extension-dataset request. Funding sets and honors it. It adds one small class, justified by this
  concrete risk, and nothing else (no coordinator, no cross-process state; two Collectors on one IP remain excluded
  by architecture §4.1).
- Candle collection is **not** paused by it and does not read it. A `418` that also bans klines is visible in the candle
  health fields as today (F-8 is not changed).
- **Amendment A-3 to ADR-0003 (proposed, not applied, open decision O-6):** the open interest task also sets and
  honors `ExtensionPause`. Reason: otherwise a `429` on funding stops funding only, while open interest, which shares
  the IP, keeps sending. Until A-3 is approved and implemented, funding self-pauses and the residual risk is documented.
  ADR-0003's accepted text is not edited by this ADR.

### P-10 Scheduler and per-series state machine

The scheduler is a pure function of (persisted state, in-memory flags, `now`, the limiter's grants); the same inputs
give the same next request. All orderings are total (ties by `symbol` ascending).

**Per-series state**, derived at every planning instant (`b_now = server_lower_bound − publication_delay_ms`):

| State | Condition |
|---|---|
| `UNSEEDED` | no span row |
| `ACTIVE` | a span row exists; with the derived flags below |
| `SUSPENDED` | in-memory overlay on any state: `suspended_until > now` (below) |

Flags of an `ACTIVE` series: `behind` = `b_now − through_ms ≥ 7 200 000`; `history_pending` =
`through_ms − from_ms < backfill_days × 86 400 000`, that is, the span is not yet as wide as the configured depth. It is a
function of the stored span and the configuration only: no clock, no tolerance, and the same value in the Collector and
in the API. Define `target_from = through_ms − backfill_days × 86 400 000`; then `history_pending` is `from_ms >
target_from`. `from_ms` never rises and `through_ms` never falls (P-6), so the target moves forward with `through_ms` but
never reopens finished history: once `history_pending` is false it stays false until `backfill_days` is raised.
`continuing` (in memory) = a forward range was truncated and
its next page is waiting. A series with `behind` and no `continuing` flag (after a restart) is treated as
`continuing` from `through_ms + 1`.

**Work classes** (limiter shares in P-9):

| Class | Request | Range | Share |
|---|---|---|---|
| **S** | seed of an `UNSEEDED` series | `[b_now − recheck_ms, b_now]`, first page | R |
| **R** | the hourly check of an `ACTIVE` series | `[max(from_ms, through_ms − recheck_ms), b_now]`, **first page only** | R |
| **C** | next page of a truncated forward range | `[a', b_now]` | C+H |
| **H** | next chunk of history | `[max(target_from, from_ms − 90 d), from_ms − 1]` with `target_from` as above, consecutive pages | C+H |

**Selection.** At each grant opportunity:

1. If a round is active (`wake ≤ now < deadline`) and the R share has room: the next series, among those that have not
   been asked in this round, are not busy and are not suspended, in the order (a) `UNSEEDED` series first (S before R),
   (b) ascending `verified_at_ms` (the series verified longest ago first), (c) `symbol`. On a fresh database and in the
   first round this is symbol order. Afterwards a series skipped at a deadline comes first in the next round, so no
   series can be skipped permanently; a round ordered by `symbol` alone left the tail of the list unserved for all 48
   rounds under overload in the simulation of E-14.
2. Otherwise, if the C+H share has room, one in-memory toggle `turn` decides between the two classes. `turn` names the
   class that was **not** granted last (initially C). The named class is granted if it has an eligible series,
   otherwise the other class is. While both classes have work they therefore alternate, one request each. Within C: the
   series with the oldest `verified_at_ms`. Within H: **a series never served in this process run comes first** (so a
   new symbol is not behind long-served ones), then the series with the smallest `last_served` sequence number, then
   `symbol`; a series whose chunk has further pages stays first in H until the last page of that chunk.
3. A series is *busy* while one of its requests is in flight or its page is being written; at most one request per
   series is active, so the writer's per-series order is the planning order.

**Rounds and deadline.** The round of hour `B` wakes at `B + round_offset_ms` (default 60 000 ms [P]: twice the delay and about 20 times the largest observed publication time, E-10) and has the deadline
`B + 3 600 000 − 15 000`. One server-time observation per round (class R, index 0). A series not started by the
deadline is **skipped** (logged with the count), not failed: the next round's range overlaps by `recheck_ms`, so nothing
is lost, only verified one hour later (`behind` after two), and the skipped series is first in the next round (selection rule 1). Started requests are cancelled at the deadline; a
cancelled page keeps its committed row chunks and grows the span only if its final transaction committed (P-7). Two
rounds never overlap (the deadline is before the next wake). If trusted time cannot be obtained at wake time the round
is skipped and logged.

**Transitions.**

| Event | Effect |
|---|---|
| S page successful | `UNSEEDED → ACTIVE` (`history_pending` set by the derived rule) |
| R / C page successful | `through_ms` grows; if the page was truncated, `continuing` is set, else cleared |
| H chunk successful | `from_ms` falls |
| a request fails (network, `SourceError`, parse error, write failure, unknown outcome) | counted per series; retried with the existing bounded backoff (ADR-0003 P-10) inside its class share |
| 3 consecutive failed requests of one series | `SUSPENDED` until `now + 6 h` (first time), `12 h`, then `24 h` for repeats |
| `suspended_until` reached | one probe request; success clears the count, failure suspends again with the longer time |
| `429` / `418` | not a series failure: the limiter stops all (P-9) |
| process start | in-memory flags are cleared; the state is re-derived from the database |

**Long outages.** `through_ms` is never "too old": history does not expire, so after an outage of any length the R
request of the next round covers `[through_ms − recheck_ms, b_now]` and its further pages are class C. Cost: a 40-day
outage of an 8h symbol is 120 rows, one request; a year of a 1h symbol is 9 pages. The seed request needs no outage
logic.

**Catch-up and rounds at the same time.** They cannot take each other's share (P-9 class caps) and they never work on
one series at once (busy rule). Class C of one series can run while the round asks the others.

**Starvation.**

- *Rounds* cannot be starved: their share is reserved by the caps. *Inside* a round no series is starved either: the
  order is by `verified_at_ms` (selection rule 1), so a series skipped at a deadline is served first in the next round.
- *History* cannot be starved by catch-up. Inside the C+H share the classes alternate (selection rule 2), so while H
  has an eligible series at least `floor(n / 2)` of any `n` consecutive C+H request starts are H starts, whatever the size
  of the C backlog; at saturation (`n = b − m` per window) that is `h = floor((b − m) / 2)` H starts per window
  (12 at `b = 50`, 37 at `b = 150`, 62 at `b = 250`). A C backlog only halves the history speed. H is served
  round-robin, so with `P` series pending and one-page chunks every pending series starts a chunk at least once per
  `ceil(P / h)` windows (659 series at `b = 150`: 18 windows, about 92 minutes); a 3-page chunk of a 1 h symbol uses three
  starts. The same alternation gives class C at least `h` starts per window, so a large history backlog cannot starve
  catch-up either. With no C work H uses the whole share, and the reverse.
- *A new symbol* is seeded in the first round that starts after it appears (at most one round duration, bounded by
  the sustainable capacity below) and its first H chunk comes before any chunk of a series that was already served.
- *A suspended series* does not block others (skipped by rule 1 and 2), is visible in the Collector log (the API shows
  its effect, `behind`, P-13) and is probed again by itself.

**Capacity: one isolated round, and what is sustainable.** Notation: `N = S + 1` requests per round (one server-time
request, index 0, and `S` series requests, index `1 … S`); `Wl = 305 000 ms`; the limiter releases R requests in groups
of `m` at most `Wl` apart (P-9). Request `j` of a round that starts with an **empty window** starts at
`floor(j / m) × Wl` after the wake time. Requiring the last request to start 30 s before the deadline gives
`K = floor((3 555 000 − round_offset_ms) / 305 000)`.

*Capacity of one isolated round* (an empty window at the wake, as in the first round after a start):
`S ≤ (K + 1) × m − 1`. For the default `round_offset_ms = 60 000`, `K = 11` and the three budgets give 1 499, 899 and
299. **This is not a sustainable capacity** and is not used for the validation. Rounds repeat every hour and share the
limiter's window: at `S = (K + 1) × m − 1` the last group of a round starts 3 355 s after its wake and its window
ends 60 s after the *next* wake, so the next round's first group is delayed by 60 s, the one after by 120 s, the one
after by 180 s, and the fourth round loses a whole group of `m` series at its deadline; the cycle then repeats. The
simulation (E-14) shows this for all three budgets: 12 of 48 rounds skip `m` series each.

*Sustainable capacity* (the validation). A round starts with an empty window if its last start plus `Wl` is not after
the next wake. With `Q = min(K + 1, floor(3 600 000 / Wl)) = min(K + 1, 11)` full groups per round,
the last group starts at `(Q − 1) × Wl`, and `Q × Wl ≤ 3 600 000` holds, so every round is again an isolated one,
provided the in-flight limit lets the last group start within the slack of `3 600 000 − Q × Wl` (245 s at `Q = 11`),
roughly `m × d̄ / 2 ≤ 245 s` for a mean request time `d̄` (a few starts carrying over into the next round is still
absorbed in the simulation, up to the in-flight bound below):

**`S_sustained = Q × m − 1`**, `Q = min(K + 1, 11)`.

`Q` is smaller than `K + 1` exactly when `round_offset_ms ≤ 200 000` (then `K + 1 ≥ 12`), which includes the
default; for offsets above 200 000 ms both capacities coincide, and from 505 001 ms `Q = 10`. The Collector **refuses to
start** (config error naming the key and the values) when `S > S_sustained`, and **logs a warning** when
`S > floor(0.9 × S_sustained)` ("less than 10 % reserve for retries and slow requests").

| `b` | `m` | isolated round (not used) | **`S_sustained` (refusal above)** | warning above | greedy maximum in the simulation (information) |
|---|---|---|---|---|---|
| 250 | 125 | 1 499 | **1 374** | 1 236 | 1 467 |
| 150 (default) | 75 | 899 | **824** | 741 | 880 |
| 50 | 25 | 299 | **274** | 246 | 292 |

The last column is what one greedy policy sustained in the model at the default offset; it is about 7 % above
`S_sustained` (6 % at 120 s, 0 at 300 s and above), depends on that policy and is **not** a guarantee, so it is not
used. `S_sustained` is a lower bound that holds for every `m` from 25 to 125 (E-14). The 659 USDT perpetuals fit at
default (80 % of `S_sustained`, below the warning).

*Reserve for errors and slow requests* (simulated, E-14; one retry after a 1 s backoff, requests of 0.4 s, at most 2 in
flight, retries inside the R share): at `S_sustained` a failure rate of 5 % causes no skip and 10 % causes skips in every
round; at `0.9 × S_sustained` 10 % causes none and 20 % causes skips, each series skipped in at most 2 consecutive
rounds (selection rule 1). The skip is the safety valve (a skipped series is verified an hour later, P-8), not an error.
The in-flight limit also bounds the throughput: if every request takes `d̄`, a round needs about `N × d̄ / 2` of
request time, so it is sustained only while `N × d̄ / 2 ≤ 3 555 000 ms − round_offset_ms`, i.e. `d̄ ≤ 5.1 s` at
`b = 250`, `8.5 s` at `b = 150` and `25 s` at `b = 50` at the default offset (simulated: 4 s passes and 6 s fails at
`b = 250`; 8 s passes at `b = 150`). 10 % of requests taking 8 s was absorbed at all three budgets. `d̄` is a runtime
quantity: it is logged in the hourly line and is not validated.

**Planned latency** from settlement to a stored row, ideal run (first attempt succeeds, no `429`, no yielding,
request and write time ignored), for `S ≤ S_sustained`: series number `j` (1-based, in round order, selection rule 1) is
asked at `round_offset_ms + floor(j / m) × 305 s`. In the simulation this was identical in each of 48 consecutive
rounds (no drift); with 0.4 s requests and 2 in flight each start is 0 to 15 s later (2.0 s for `S = 10`, 5.2 s for 100, 0 for 300,
12 s for 659, 14.8 s for 824).

| `S` | `j` of the last | `floor(j / 75)` | latest, `b = 150` |
|---|---|---|---|
| 10 (today) | 10 | 0 | 60 s |
| 100 | 100 | 1 | 365 s (about 6 min) |
| 300 | 300 | 4 | 1 280 s (about 21 min) |
| 659 (all USDT perpetuals) | 659 | 8 | 2 500 s (about 42 min) |

**No latency is guaranteed.** Errors, a `429` pause, a missed deadline, yielding to candle writes or a stopped task add
delay without a bound. What **is** guaranteed is the honesty of the read side: `through_ms` only grows by a successful
page (P-7), so a settlement that is not stored yet is outside the verified span, never presented as complete.

### P-11 Restart reconcile and bounded backfill

There is no checkpoint beyond the stored rows and `funding_rate_verified`; both are facts (as for candles, S15 D1).

On start (deterministic: the same stored data, `as_of` and config give the same plan):

1. Read, per configured series, the span and the conflicted events (one bounded read per series on the funding thread;
   a failing read stops the funding task and leaves candles running).
2. Derive each series' state and flags (P-10). No span → class S at the first round. `behind` → class C from
   `through_ms + 1`. `history_pending` → class H.
3. Log a summary per series: span, stored count, conflicted events, flags, pages planned.

Backfill is **only backward chunks of at most 90 days** (and the forward pages of an outage). There is no forward
walk from `history_start`: the first page of a new series is the seed, so the series is current at once (P-7). Raising
`backfill_days` needs no special case: `history_pending` (P-10: span width below the configured depth) becomes true and
chunks continue below `from_ms`. Lowering it makes it false and requests nothing (the span never shrinks).

Cost: with `backfill_days = 365` a series needs one seed (class R) and `ceil(365 / 90) = 5` chunk requests (class H),
more for a 1h or 2h symbol (a 90-day chunk holds 2 160 or 1 080 rows, so 3 or 2 pages). For the 659 USDT perpetuals:
the 659 seeds fit in the first round (index 659 starts at 2 440 s), and `659 × 5 = 3 295` chunk requests (plus a few
extra pages) at `b − m = 75` per 305 s window take `3 295 / 75 × 305 s ≈ 3.7 h` when no class C work exists (the normal case; with a C backlog history gets at least half
of the share, P-10), **about 4 hours**; for today's 10
symbols 10 seeds and 50 chunk requests, one window. A larger chunk would cut the requests (a 365-day chunk is 1 to 9 pages) at the price of losing more progress on
a crash; 90 days is a [P] trade-off, constants in code, not configuration.

### P-12 Configuration

A new **optional** top-level table `[funding_rate]` in `config/quasar.toml` [P]:

```toml
[funding_rate]
enabled = true
publication_delay_ms = 30000
round_offset_ms = 60000
budget_per_5min = 150        # integer, validated 50 <= value <= 250
backfill_days = 365          # integer, validated 1 <= value <= 3650
recheck_ms = 86400000        # validated 3600000 <= value <= 604800000
```

Absent means disabled, so existing configuration files keep working. Inside the table every key is required and unknown
keys are an error (S20 rule). Funding is collected for every symbol of the venue; there is no second symbol list. The
API reads the same file (as for open interest) to know `publication_delay_ms`, `recheck_ms` and `backfill_days`.
`round_offset_ms` is validated in `[60 000, 600 000]` and must be at least `publication_delay_ms + 5 000`, otherwise the end bound `b` of the round would lie before the settlement it is meant to verify; the sustainable capacity of P-10 is checked at start (refusal above `S_sustained`, warning above 90 % of it). A failed check
names the key and the values. The constants of P-9/P-10 (`G`, `HISTORY_CHUNK_MS`, the 2 h `behind` threshold, the
suspend times) are code constants.

### P-13 API

All coverage fields are defined over the **requested window**, `[start, end]`, and are computed **per request** from that
request's own read snapshot over the whole window, never from the rows of the page; paging changes only `rows`, `next`
and the per-row `conflicted`. For an unchanged database every page of a window therefore carries the same values.

- `GET /v1/funding-rates?venue&symbol&start&end&limit` returns rows ordered by `(funding_time_ms, rate_type)`:
  `funding_time_ms`, `rate_type`, `funding_rate` and `mark_price` (decimal strings, `mark_price` may be `null`),
  `conflicted`. `start` is required. `end` defaults to `as_of_ms`, the API's clock at the snapshot (so a stopped series
  answers `partly_verified`, not `verified`: the defaulted-end weakness noted for open interest does not arise). The cursor
  is `(funding_time_ms, rate_type)`; paging, limits and errors mirror `GET /v1/candles`.
- The response carries one `coverage` block: `as_of_ms`; `verified_from_ms`, `verified_through_ms`, `verified_at_ms`
  (null without a span); `window_state` (`verified`, `partly_verified`, `unverified`, P-7); `events_in_window` (rows
  with `start ≤ funding_time_ms ≤ end`, counted over the whole window, not only the page); `conflicted_events` (distinct `(funding_time_ms, rate_type)` in the
  window with at least one conflict record, a record without a stored row included); `complete`
  (`window_state = verified` and `conflicted_events = 0`). All of it is computed from one read snapshot over the
  window: the span row, a range count on `funding_rates`, a range count on `funding_rate_conflicts` (indexed by
  P-6), never from the rows of the page.
- **Snapshot scope: one HTTP request.** The rows and the `coverage` block of one response come from one read snapshot
  (one transaction on one connection, as for the other endpoints). Different pages of one window are different
  requests with different snapshots, and the API promises **nothing** about how they relate: between two requests the
  Collector may have extended the span, stored rows or recorded a conflict, so `window_state`, `events_in_window`,
  `conflicted_events` and `complete` of one page may differ from those of another, in either direction (a window can
  become `verified` as the span grows, and `complete` can turn false when a conflict appears). The response contains no
  cross-page change detection. A consumer that needs one consistent answer for a window reads it in a single request
  (where the window fits one page); otherwise it treats the coverage of each page as describing that response's
  moment only.
- The response states `time_semantics: "settlement"` and repeats, in the contract text, that `funding_rate` is per
  settlement and that the interval is not part of the row.
- `GET /health/ingest` gains an **additive** `funding_rate` object (null when not enabled): `status`, `series_count`,
  `current`, `behind`, `catching_up`, `conflicted_series` and `quiet_series`. A series is **quiet**
  when its newest stored settlement is older than 48 h [P: within a series the longest spacing was 16 h except one of 955 h, E-5, E-13, so a
  genuine pause of two days or more is reported as `degraded` until the next event arrives, which is accepted]; quiet makes the
  status `degraded`, because it is the only signal that distinguishes a valid empty answer from a wrong symbol. `behind`
  means `verified_through_ms` is older than `publication_delay_ms + 2 h`. `catching_up` means `history_pending` (P-10): `through_ms − from_ms < backfill_days × 86 400 000`, computed from the stored span and the configuration, with no clock.
  A suspended series (P-10) is state in the memory of the Collector and is not visible to the API: the API shows the
  effect (`behind`), the Collector log shows the cause, and there is no `suspended_series` field.
- Without an enabled `[funding_rate]` table the endpoint answers `404 funding_rate_disabled` without opening the database.
- `GET /v1/candles`, `/v1/coverage`, `/v1/indicators` and `/v1/open-interest*` are unchanged.
- The exact schema is a new section (6.7) of `docs/contracts/api-v1.md`, approved before the API slice.

### P-14 Deployment and migration impact

Schema version 3 follows ADR-0003 P-14 exactly: an API of version 2 answers `503 not_ready` once the Collector has
applied migration 3 and the reverse; a short, announced API outage; no rollback by redeploy (a previous Collector
refuses a newer database), only restore of a backup taken before the migration. The user accepted this trade-off for
schema 2 (ADR-0003 O-1); it is **not re-opened** here unless the user asks. If funding and open interest are released
together the database moves 1→3 in one release and the procedure is written once.

### P-15 Tests (regression set, all with mocked exchange responses)

Each slice adds its tests first. Live tests stay under `make live`, never in `make check`. "Fake clock" means injected
time, no sleeping.

| Area | Test (behavior that must fail when the guard is removed) | Slice |
|---|---|---|
| Parser | a list with an error object instead of a list (HTTP 200 body of E-4), a row outside `[start, end]`, a non-int `fundingTime`, a repeated key, non-ascending rows, wrong symbol, a float or int in `fundingRate`, `markPrice` that is neither `""` nor a decimal, an unknown `rateType`, more rows than `limit` → the whole response is rejected, nothing stored, nothing verified | FR-1/FR-3 |
| Parser | a negative rate and `0.00000000` parse; `-0.00000000` is canonicalised to `0`; `markPrice ""` becomes NULL; a Special row 1 002 ms after a Regular one of the same hour is accepted as a different key | FR-1 |
| Heuristic | a row whose `fundingTime` is 30 minutes past the hour is **accepted**, stored, counted in `off_hour_rows` and logged; a timestamp in seconds is rejected by the range rule | FR-1/FR-3 |
| Request | no request without `startTime`, with `limit` ≠ 1000 or for a symbol the market check did not verify; `200 []` for an unknown symbol is never reached | FR-3 |
| Eligibility | a row with `T + delay > server_lower_bound` is not stored and a forward request never ends after `server_lower_bound − delay`; untrusted time stores nothing | FR-1/FR-4 |
| Paging | **exactly 1 000 rows in a range** → page covers `[a, last − 1]`, the next request starts at `last`, returns one row, and the span is the whole range; 999 rows → one request; 1 000 rows where the last two share a `fundingTime` (Regular and Special) → neither is skipped; 2 500 rows → three requests, consecutive, rows stored once | FR-1/FR-3 |
| Storage | `UPDATE`, `DELETE`, `INSERT OR REPLACE` on `funding_rates` and `funding_rate_conflicts` abort; the span trigger refuses a lowered `through_ms`, a raised `from_ms`, a lowered `verified_at_ms`, a changed key and a `DELETE`, and accepts growth | FR-2 |
| Storage | the signed check accepts `-0.0001` and refuses `-0`, `-`, `+1`, `1.50`, `00.1`, a float | FR-2 |
| Conflicts | a re-delivered row with a different rate or a different `mark_price` (also NULL vs price) → one conflict row, stored row unchanged; the same observation twice → still one row; Regular and Special of one hour never conflict | FR-2 |
| Span | `merge`: creating, extending upward, extending downward, a non-adjacent interval (`a = through + 2`) refused and the final transaction rolled back, an interval inside the span a no-op; the union is exact at the boundaries `a = through + 1` (accepted) and `a = through + 2` (refused); `e = from − 1` (accepted) and `e = from − 2` (refused) | FR-1/FR-2 |
| Span | a failed page, a parse error, a dropped row, an unknown write outcome and a rolled-back final transaction extend nothing; an empty valid answer extends; the end of a page never exceeds `server_lower_bound − delay` | FR-1/FR-4 |
| Crash | **crash between data and coverage**: kill the writer after chunk `k` of a 250-row page, after the last earlier chunk, inside the final transaction (injected failure before and after the COMMIT), after the commit before the bookkeeping → in every case the span never covers a settlement whose row is not stored, a reader snapshot never shows a span without its rows, and a restart converges to the same final state with no duplicate and no conflict | FR-2/FR-4 |
| Crash | unknown outcome of the final transaction, then restart with the growth committed, and with it not committed → both converge | FR-4 |
| Backfill | restart **mid forward catch-up**: after page 1 of 3 the span ends at `last − 1`; the restart continues from `through + 1` | FR-4 |
| Backfill | restart **mid backward history**: after chunk 2 of 5 `from_ms` is the lower edge of chunk 2; after a crash inside chunk 3 `from_ms` is unchanged and chunk 3 is repeated, stored pages are no-ops; a crash loop that completes one chunk per run still ends with all chunks | FR-4 |
| Backfill | `backfill_days` raised from 365 to 730 → `history_pending` true and chunks continue below `from_ms`; lowered → false and nothing requested; a young symbol's chunks before its listing return `[]` and extend the span until the width reaches the depth | FR-4 |
| Backfill | **`history_pending` boundary, no tolerance**: width `through_ms − from_ms` equal to `backfill_days × 86 400 000` → not pending; one millisecond less → pending; 24 h less (the tolerance of revision 2) → pending; the last chunk is shorter than 90 days and ends the backfill exactly at `target_from` | FR-1/FR-4 |
| Backfill | **moving target**: while a series is pending `through_ms` advances by a round (+1 h) between the planning and the completion of its last chunk → the series is not pending afterwards; a series that is not pending stays not pending as `through_ms` advances through 24 rounds; a property test over random sequences of chunk completions, round advances, restarts, crashes and a raised or unchanged `backfill_days` asserts that `history_pending` never goes from false to true unless `backfill_days` was raised, and that `from_ms` never rises | FR-1/FR-4 |
| Backfill | after an outage of 40 days the next round's R request and C pages catch up; nothing is reported unrecoverable | FR-4 |
| Scheduler | the same stored data, clock and grants give the same request order (determinism); R requests are in symbol order, S before R; C and H alternate inside the C+H share (the toggle names the class not granted last, an empty class is skipped); H serves a never-served series first | FR-4 |
| Scheduler | **rounds and catch-up overlap**: with C and H backlogs saturating their share, every R request of the round still starts at its planned `floor(j / m) × 305 s`; with R saturated, C/H still start within their share; no class exceeds its cap in any 305 s window; one series never has two requests in flight | FR-4 |
| Scheduler | **history cannot starve behind catch-up**: 200 series permanently in class C (each range truncated again) and 300 series pending history, for 50 windows at `b = 50, 150, 250` → of the C+H starts at least `floor(n / 2)` of every `n` consecutive ones are H while H has an eligible series (`h = floor((b − m) / 2)` per window at saturation), every pending series starts a chunk within `ceil(P / h)` windows (one-page chunks), C still receives at least `h`; with C empty H uses the whole `b − m`, with H empty C does; every R request keeps its planned start time and no class exceeds its cap; candle requests are unaffected | FR-4 |
| Scheduler | starvation: with 100 series pending history and one new symbol appearing, the new symbol is seeded in the next round and gets its first H chunk before any already-served series' second chunk | FR-4 |
| Scheduler | deadline: a round with more series than fit is cut at the deadline, the rest are skipped and logged, nothing marked failed, the next round covers them; two rounds never overlap | FR-4 |
| Scheduler | suspension: three consecutive failures suspend a series for 6 h (then 12 h, 24 h), other series continue, a probe after the time clears or extends it, a restart clears it | FR-4 |
| Capacity | `S_sustained = Q × m − 1` with `Q = min(K + 1, 11)`: accepted at `S_sustained` and refused at `S_sustained + 1`, for `b = 50, 150, 250` and for every `m` from 25 to 125; `round_offset_ms` boundaries 60 000 (`Q = 11`), 200 000 (`K = 11`, `Q = 11`), 200 001 (`K = 10`, `Q = 11`), 505 000 (`K = 10`, `Q = 11`), 505 001 (`K = 9`, `Q = 10`) and 600 000 (`Q = 10`); a warning, not a refusal, above `floor(0.9 × S_sustained)` | FR-1/FR-4 |
| Capacity | **48 consecutive hourly rounds with the limiter state carried over** (fake clock; wake at `B + round_offset_ms` for `B` = 0, 1 h, 2 h, …; one server-time request per round counted in R): at `S_sustained` for `b = 50, 150, 250` no round skips a series, the first and the last start offsets are the same in every round (no drift) and every last start is at least 30 s before the deadline; the same for 200 rounds at `b = 150`, with 0.4 s requests, 2 in flight and a history backlog saturating the C+H share (R start times unchanged) | FR-4 |
| Capacity | **the isolated-round capacity is not sustainable** (documents why it was replaced): at `S = (K + 1) × m − 1` (299, 899, 1 499) the first start of rounds 0 to 3 is delayed by 0, 60, 120 and 180 s and round 3 skips `m` series, repeating, 12 of 48 rounds | FR-4 |
| Capacity | **retries and delays**: at `S_sustained` 5 % failures (one retry after a 1 s backoff) cause no skip and 10 % cause skips; at `0.9 × S_sustained` 10 % cause none and 20 % cause skips with no series skipped in more than 2 consecutive rounds; the same run ordered by `symbol` leaves tail series unserved in all 48 rounds (the test asserts the rotation of selection rule 1, not the symbol order); 10 % of requests taking 8 s cause no skip; all requests taking `d̄` cause no skip while `N × d̄ / 2 ≤ 3 555 000 ms − round_offset_ms` and skips above it (`b = 250`: 4 s passes, 6 s fails) | FR-4 |
| Latency | for `S ≤ S_sustained` the last series starts at `floor(j / m) × 305 s` after the wake in each of 48 rounds (instant requests) and at most 15 s later with 0.4 s requests and 2 in flight; `S = 10, 100, 300, 659` give 0, 305, 1 220 and 2 440 s | FR-1/FR-4 |
| Rate limiting | the limiter never allows more than `b` starts in any interval of length 305 s, with rounds, retries, time and backfill competing; **boundary**: request `k + b` of a saturated stream starts at exactly 305 s after request `k`, not at 304.999 s; R never exceeds `m`, C+H never exceed `b − m` | FR-3/FR-4 |
| Rate limiting | **repeated 429 and budget exhaustion**: a `429` stops all starts for `max(Retry-After, 300 s)`; a second one doubles, up to 1 h; after the pause the window still counts earlier starts; a request waiting at the `429` sends nothing; with the budget exhausted a request waits and an expired round deadline drops it unsent; a `418` stops the task for the run; candle requests continue throughout | FR-3/FR-4 |
| Rate limiting | `budget_per_5min` 49, 251, `"150"`, 150.0, a missing key and an unknown key are config errors naming the key; `backfill_days` 0 and 3651, `recheck_ms` out of range likewise | FR-4 |
| Multi-dataset | **funding and open interest enabled on one IP**: each limiter is independent (saturating funding does not delay open interest starts); the sum of both datasets' starts never exceeds `b_funding + b_oi` in a 305 s window; a `429` set on `ExtensionPause` by funding stops funding and, once A-3 is implemented, a fake open interest task; candle requests continue throughout and are never counted | FR-4 |
| Independence | a funding source that raises on every call, hangs or returns `429`: candle rounds collect the same candles with the same `lateness_ms` bound as with funding disabled; `[funding_rate]` absent → no client, no request | FR-4 |
| Contention | no funding transaction begins while a candle write is active; a transaction holds at most 100 rows; `SQLITE_BUSY` fails and retries without conflicts | FR-2/FR-4 |
| API window | for an unchanged database, a window of 2 500 events read as three pages returns the same `window_state`, `events_in_window`, `conflicted_events`, `complete` and span fields on every page, and `events_in_window` equals the sum of the rows over the pages | FR-5 |
| API snapshot | **one request, one snapshot**: a writer that grows the span and records a conflict while a request is being served (injected between the row read and the coverage reads) never produces a response whose rows and coverage come from different states | FR-5 |
| API snapshot | **no cross-page promise**: with the span grown and a conflict recorded between page 1 and page 2, page 2's coverage reflects the new state and page 1's the old one, `complete` is allowed to differ in both directions, and no field of either response claims or implies consistency with the other | FR-5 |
| API window | **a conflicted event outside the current page** (on page 1 of 3, request page 3): `conflicted_events = 1` and `complete = false` on page 3 while no row of page 3 is flagged; a conflict outside the window does not change `complete`; a conflict record without a stored row counts | FR-5 |
| API window | `window_state` at the edges: `start = from_ms` and `end = through_ms` → `verified`; `start = from_ms − 1` → `partly_verified`; `end = through_ms + 1` → `partly_verified`; disjoint or no span → `unverified`; the defaulted `end` of a stopped series is `partly_verified` | FR-5 |
| API | rows ordered and paged by `(funding_time_ms, rate_type)` with no row repeated or skipped; a conflicted row returns its first stored values; a NULL `mark_price` is `null`; `404 funding_rate_disabled`; `503` never an empty `200`; one read snapshot per request | FR-5 |
| API health | a series with no event for more than 48 h is `quiet` and the health status `degraded`; `behind` and `catching_up` follow the span, `catching_up` being false at width `backfill_days × 86 400 000` and true one millisecond below it, with no clock; existing health fields and other endpoints unchanged | FR-5 |
| Deployment | migration 2→3 on a copy keeps every candle and open interest row (checksum before and after); an API of version 2 against a version 3 database answers `503` | FR-2 |

### P-16 Slices

| Slice | Content | Gate |
|---|---|---|
| FR-1 | domain `FundingRate` value object (signed decimal, nullable mark price, `rate_type`), response parser (contract rows of P-3), eligibility, the pure page-coverage and `merge` rules of P-5/P-7, the sustainable-capacity function (`S_sustained`, `Q`, `K`) and the latency function of P-10 (pure) | this ADR |
| FR-2 | migration 003, funding writer with conflicts, the final-transaction `merge`, own connection and thread, transactions of ≤ 100 rows | this ADR (P-6, P-7, P-8, P-9) |
| FR-3 | adapter method on its own CCXT client, request rules (P-5), limiter with the 305 s window and class caps, `429`/`418` handling, `ExtensionPause` (P-9); `fetch-funding-once` CLI command | this ADR |
| FR-4 | funding task: scheduler and state machine, rounds, restart reconcile, backfill, config table, isolation (P-9 to P-12) | this ADR |
| FR-5 | `GET /v1/funding-rates`, `/health/ingest` extension | contract section 6.7 approved first |

New invariant rows (from INV-100) mirror INV-071 to INV-078 for funding and add: "funding never delays or stops candle
collection", "funding request starts stay within the local budget in every 305 s window", "the verified span only grows,
only by a page whose rows are committed with or before it, and only adjacent to what was verified", "a window is
`complete` only if all of it lies in the span and no event in it is conflicted", "an unknown symbol is never requested"
and "an extension dataset sends nothing while `ExtensionPause` is set".

## Consequences

- One additive schema version bump with an announced API outage and no rollback by redeploy (P-14).
- Funding coverage is stated per **window of verification**, not per slot: a consumer learns whether Binance's list was
  complete for its window, not which settlements were expected. That is what the data allows (Context 1).
- Binance can still revise or add rows after a range is verified; revisions are caught only while a row is inside the
  recheck window (P-8). Late events are logged, not shown by the API.
- At the architecture's 600-symbol target the last symbol of a round is stored up to about 42 minutes after the settlement, in
  an ideal run (P-10). Today's 10 symbols are stored about `round_offset_ms` after it.
- The sustainable capacity at the default budget is 824 symbols (not 899, which is only the capacity of one isolated round); the 659 USDT perpetuals use 80 % of it (P-10, E-14).
- History of a 659-symbol universe takes about 4 hours to backfill at the default budget; each new symbol is current
  after one request (P-10, P-11).
- One table is updated in place (`funding_rate_verified`), growth only (P-7, O-1).
- `mark_price` is NULL for history before 2023-10-31 (P-2, O-4).
- Funding takes its own client, budget, writer connection and time slot; candle contracts, tables and schedule do not
  change (P-9). The only shared object is `ExtensionPause` (P-9).
- Measurement debt: one machine, one afternoon, three symbols for publication timing, a recheck of about 90 minutes,
  a 365-day sample per symbol for timestamps. The soak test after FR-4 re-measures publication time, revisions and the
  100-row transaction time.

## Open decisions (for the user)

| ID | Question | Proposal in this ADR |
|---|---|---|
| O-1 | Coverage model: a **verified span** per series (one table updated in place, growth only), **append-only** merge rows, or **no completeness claim** (rows only, with `spacing_ms`)? | verified span, updated in place (P-7) |
| O-2 | `rate_type` in the key: keep `Regular` and `Special` as two records of one hour, or key without it (a coincident pair becomes a conflict)? | in the key (P-3) |
| O-3 | Default backfill depth: 365 days (about 3 300 chunk requests for 659 symbols, about 4 hours), or the full history back to 2019? | 365, configurable to 3 650 (P-11, P-12) |
| O-4 | `mark_price` nullable (history from 2019 kept, price NULL before 2023-10-31) or required (rows without a price are a parse error, so history effectively starts 2023-10-31)? | nullable (P-2) |
| O-5 | Latency at scale: is up to about 42 minutes (ideal run, no guarantee) acceptable at 659 symbols and `b = 150`? The alternative is a larger budget share than 30 %. | accept (P-10) |
| O-6 | Amendment A-3 to ADR-0003: the open interest task also honors `ExtensionPause`, so a `429`/`418` stops both extension datasets. | approve A-3 (P-9) |

## Part 3 — Evidence

Probe of 2026-11-03 (Europe/Prague), public endpoints only, no credentials, from one macOS machine against
`fapi.binance.com`, standard-library HTTP for the raw responses and CCXT 4.5.89 for method names. Scripts
were throwaway in the session scratchpad and are not in the repository.

- **E-1 Official pages** (`developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/`
  `Get-Funding-Rate-History` and `Get-Funding-Rate-Info`, read 2026-11-03 through a page-to-text fetch): path
  `/fapi/v1/fundingRate`; "share 500/5min/IP rate limit with GET /fapi/v1/fundingInfo"; `startTime` and `endTime`
  inclusive; `limit` default 100, max 1000; "If startTime and endTime are not sent, the most recent 200 records are
  returned."; "If the number of data between startTime and endTime is larger than limit, return as startTime + limit.";
  ascending order; `markPrice`: "mark price associated with a particular funding fee charge"; `rateType` `Regular` or
  `Special` ("for the additional funding rate generated by stock dividends"); `fundingInfo`: weight 0, "symbols that had
  FundingRateCap/FundingRateFloor / fundingIntervalHours adjustment". Not stated: the units, the definition of
  `fundingTime`, any alignment of `fundingTime`, whether the 500 is shared with other endpoints, the window type, the
  behavior on `429`/`418`, whether Binance can revise a row.
- **E-2 Shape and numbers.** Keys: `symbol`, `fundingTime` (int), `fundingRate` (string), `markPrice` (string),
  `rateType` (string). 86 870 rows of 12 symbols: `fundingRate` always has 8 fractional digits, 16 740 are negative,
  4 328 are `0.00000000` and none is `-0.00000000`. BTCUSDT's latest rate was `0.00010000`, the standard base rate,
  which fits a fraction per settlement.
- **E-3 History.** BTCUSDT from `startTime` 2019-01-01 returned 1 000 rows, the first at 1568102400000 (2019-09-10
  08:00 UTC). Full history of 12 symbols: 6 151 to 7 835 rows each, 86 870 in total, all ascending, **0 duplicate
  `fundingTime`**. A second sweep of all 235 TradFi perpetuals: 92 799 rows, 0 symbols with a duplicate `fundingTime`.
- **E-4 Request behavior** (BTCUSDT unless noted). No parameters: 100 rows. `limit=1`: 1. `limit=1000` without
  `startTime`: **500**. `limit=1000` with `startTime`: 1 000. `limit=1001` and `1500`, and `startTime=x`: **HTTP 200 with
  `{"status":"ERROR","type":"GENERAL","code":"99099990","errorData":"illegal params.",…}`**. Unknown symbol
  `NOPEUSDT`: `200 []`. `startTime > endTime`: `200 []`. A range of 100 days with `limit=5`: the **5 oldest** rows of the
  range. `startTime` only: ascending to the latest row. `endTime` only with `limit=3`: the 3 newest rows at or before
  `endTime`. Inclusive bounds at a row `ft`: `[ft, ft]` 1 row, `[ft+1, ft+1]` none, `[ft−1, ft]` 1, `[ft, ft+10]` 1.
  Not tested: a range of exactly 1 000 rows, and two rows with one `fundingTime` (none exist in the sample), which is why
  P-5 does not rely on either.
- **E-5 Intervals.** `fundingInfo` returned 820 entries: 467 with 4 h, 352 with 8 h, 1 with 1 h (HOTUSDT); BTCUSDT is
  listed with 8 h and `updateTime: null`. The interval equalled the last observed spacing in 25 of 25 sampled symbols.
  History spacing, 12 crypto symbols: 8 h 85 088, 4 h 1 292, 2 h 99, 1 h 378, 3 h 1; no spacing deviated more than 1 s from
  a whole number of hours. Changes in one symbol's history: ENJUSDT 8 (for example 8 h → 1 h → 3 h → 4 h → 1 h → 2 h →
  4 h within weeks), SOLUSDT 3, HOTUSDT 1, the other nine 0. The 235 TradFi perpetuals: 8 h 74 042, 4 h 17 861, 1 h 544,
  and rare 2, 3, 5, 6, 7 and 16 h spacings (the longest in that sample; E-13 found one of 955 h in a larger one).
- **E-6 Time offsets and `Special`.** Regular rows, 86 870 (12 crypto symbols): `fundingTime mod 1 h` 0 ms for 47 183
  (54 %), 1 ms 9 310, 2 ms 3 687, … maximum **47 ms**, never negative. `Special`: 90 rows of 56 TradFi symbols (none among
  the 12 crypto symbols); 82 at 00:00 UTC, 5 at 23:00, 3 at 01:00; offsets after the hour 1 001 to 1 003 ms (73 rows) and 2 to
  12 ms (17 rows). In the six cases examined, a Regular row of the same hour existed (1 ms after the hour in all six); the
  Special one differed in `fundingTime` (7 to 1 002 ms after the hour). All 90 Special rates are negative (−0.01663491 to
  −0.00014625). `premiumIndex.nextFundingTime` is exact (`…000`), the settled rows are not.
- **E-7 `markPrice`.** 46 305 of 86 870 rows have `""`, 40 565 have a decimal with 8 fractional digits. For BTCUSDT,
  ETHUSDT and HOTUSDT every row before 2023-10-31 08:00 UTC is `""` and none after it.
- **E-8 Symbols.** `exchangeInfo`: 942 symbols: `PERPETUAL` 703, `TRADIFI_PERPETUAL` 235, `CURRENT_QUARTER` 2,
  `NEXT_QUARTER` 2; status `TRADING` 804, `SETTLING` 137, `PENDING_TRADING` 1; `PERPETUAL` by quote asset: USDT 659,
  USDC 39, others 5. `fundingRate` answered for TradFi symbols (for example `XAUUSDT`).
- **E-9 Limits and headers.** `GET /fapi/v1/time` carried `x-mbx-used-weight-1m`; the `fundingRate` and `fundingInfo`
  responses carried no `x-mbx-*` header and no `Retry-After`. No `429` was provoked. About 2 500 requests were made over about three and a half hours (two scans of 938 and 800 symbols, one of 138, the
  probes of E-2 to E-8, a recheck of about 115 requests in 75 s); the exact count was not logged. The first scan recorded
  0 HTTP errors and no `429` or `418` was noticed; the later scripts did not log non-list answers, so this is not a
  complete check of refusals.
- **E-10 Publication.** A script started 15 s before the whole-hour boundary 20:00 UTC (`T` = 1793736000000) and cycled over HOTUSDT (1 h
  interval), GTCUSDT and TUSDT (4 h), each with `GET /fapi/v1/fundingRate?symbol=…&startTime=T−60000&limit=5`, one
  request about every 0.4 s, a full cycle about 4.2 s, until a row with `fundingTime ≥ T−1000` appeared. Twelve requests
  before the settlement (1.1 to 14.5 s before `T`) returned no row for it. The first request after it, started 2 260 ms
  (HOTUSDT), 2 631 ms (GTCUSDT) and 3 008 ms (TUSDT) after `T` and answered 2 631, 3 008 and 3 414 ms after `T`, returned
  the row, with `fundingTime` exactly `T` (0 ms offset), a non-empty `markPrice` and `rateType` `Regular`. The publication
  time is therefore at most 2.3 to 3.0 s after the settlement (3.4 s counting the response). The difference
  `serverTime − local clock` was −133 ms, so the times have an error of about ±0.2 s. **One settlement, three symbols, a
  resolution of about 4 s.** Not measured: an 8 h settlement (00:00, 08:00, 16:00 UTC), many symbols at once, a
  heavily loaded boundary, a day-long distribution. The soak test after FR-4 repeats this at the 00:00 boundary with
  many symbols.
- **E-11 Recheck.** The 86 870 rows of the 12 symbols fetched at about 19:10 UTC (E-2, E-3) were fetched again from 2019-01-01 at
  20:12 UTC, **about 62 minutes later**, with the same paging. Result: **0 rows changed** (any field), **0 rows
  vanished**, **0 new rows inside the old span**, 2 new rows after its end (the 20:00 settlements of HOTUSDT and
  ENJUSDT). The 3 rows first seen at the 20:00 settlement (E-10: HOTUSDT, GTCUSDT, TUSDT), fetched again at 20:12:55,
  about 12.9 minutes after their first appearance, were **identical in every field** to the first-seen row. What this does
  not show: almost all rechecked rows were days to years old, so it mostly tests that history is stable, not that a
  fresh row is final; the rows that matter for the recheck window (P-8) are the ones in their first hours, and only 3
  rows were observed for 13 minutes. The same weakness as in ADR-0003 E-6 (32 minutes). The soak test after FR-4 is
  the real measurement.
- **E-12 CCXT unified method** [C]. `parse_funding_rate_history` returns `fundingRate` through `safe_number` and no
  `markPrice` or `rateType`; `fetch_funding_rate_history` sends `startTime` and `limit` only.
- **E-13 Timestamps across the whole universe.** Scan of all 938 USDT-M perpetuals of `exchangeInfo` (703 `PERPETUAL`, 235 `TRADIFI_PERPETUAL`, every status), one
  request per symbol with `startTime` 365 days back and `limit=1000`, one request per second: **0 HTTP errors**, 888
  symbols returned rows, 50 returned `[]` (no settlement in the last 365 days), 707 940 rows in total (the oldest at most
  1 000 rows of the year per symbol, so for 1 h and 4 h symbols the older part of the year). 0 series with a repeated key,
  0 series not ascending. **Regular rows, 707 850:** offset after the hour 0 ms for 280 079 (40 %), 1 ms for 158 591,
  2 ms for 63 829, **maximum 26 ms**, none at or above 100 ms, none before the hour. **Special rows: 90** (the 90 of E-6),
  maximum 1 003 ms. No row at or above 5 000 ms. Spacing between consecutive rows of one series, in whole hours: 1 h
  36 526, 2 h 27, 3 h 30, 4 h 449 910, 5 h 3, 6 h 4, 7 h 3, 8 h 220 455, 16 h 2, and **955 h once**; 0 spacings deviated
  more than 5 s from a whole number of hours (pairs at the same hour, that is a Regular and a Special row, are not
  spacings). The 955 h spacing is **AIAUSDT**, a symbol that is `TRADING` now, between 2025-12-11 12:00 and 2026-01-20 07:00
  UTC; its first daily kline is 2026-01-20 00:00, so the gap coincides with a period for which the klines endpoint has no
  data for this symbol. That is consistent with a delisting and relisting and is **not evidence of omitted rows**; it is
  also exactly the case that a spacing cannot tell from an omission (P-7). A second search (every `TRADING` symbol, and
  separately every non-trading one, spacings above 20 h) found no other.

- **E-14 Capacity simulation** (fake clock, deterministic; throwaway script in the session scratchpad, not in the
  repository; the FR-4 tests of P-15 reproduce it). **Model:** hourly rounds that wake at `B + round_offset_ms`;
  the limiter of P-9 (305 000 ms window, left-open, class R capped at `m`, state kept across rounds); one server-time
  request per round first, the series requests after its answer; at most 2 requests in flight; a round's unstarted
  requests are dropped and its in-flight requests cancelled at `B + 3 600 000 − 15 000`; the C+H class optionally
  saturating its own cap `b − m` (6 rounds: 1 775, 5 325 and 8 875 history starts for `b` = 50, 150, 250, that is at
  the cap); writes instant. Failures: deterministic pattern, one retry per series per round after a 1 s backoff (a
  stand-in; the real policy is the existing `RetryPolicy`, not read for this ADR), retries inside the R share. A round
  is *ok* if no series is skipped and the last start is at least 30 s before the deadline. **Results:**
  (1) isolated-round formula `(K + 1) × m − 1`, 48 rounds, offset 60 s: first-start delay of rounds 0 to 5 = 0, 60, 120,
  180, 0, 60 s; skipped per round 0 to 7 = 0, 0, 0, `m`, 0, 0, 0, `m`; 12 of 48 rounds skip, 300 / 900 / 1 500 series in
  total for `b` = 50 / 150 / 250. (2) Limiter only, 48 rounds, offset 60 s: `S_sustained` and `S_sustained − 1` are
  ok for **all 101 values of `m` from 25 to 125**. (3) Offsets 60, 120, 300, 600 s × `m` ∈ {25, 26, 50, 75, 76, 100, 125}:
  `S_sustained` is ok in all 28 combinations with instant requests, with 0.4 s requests and 2 in flight, and with
  the history class saturating its share. (4) Greedy maximum (information): 292 / 880 / 1 467 at 60 s, 290 / 874 / 1 457 at
  120 s, equal to `S_sustained` at 300 s (274 / 824 / 1 374) and at 600 s (249 / 749 / 1 249); `S_sustained` is 93.6 to
  94.5 % of it at 60 and 120 s. (5) `Q` boundaries at `m = 75`: offsets 60 000, 200 000, 200 001, 300 000, 505 000 → `Q = 11`
  (`11m − 1 = 824` ok, `12m − 1 = 899` fails); 505 001 and 600 000 → `Q = 10` (749 ok, 824 fails). (6) 200 rounds with 0.4 s
  requests, 2 in flight and history saturating: no skip, first start 0.4 s after the wake in every round, last start
  3 054.8 / 3 064.8 / 3 074.8 s after the wake in every round. (7) Retries (0.4 s requests): `S_sustained` with 5 % failures
  no skip, 10 % skips in all 48 rounds, 20 % likewise; `0.9 × S_sustained` (246 / 741 / 1 236) with 10 % no skip, 20 %
  skips. Ordered by `symbol`, series at the end of the list were served 0 times in 48 rounds (10 % at `S_sustained`, 20 %
  at `0.9 ×`); ordered by `verified_at_ms`, in the same runs every series was served 39 to 47 times of 48 and skipped in at most 2
  consecutive rounds. (8) Slow requests, `S_sustained`: every request `d̄` = 0.4 to 8 s: ok at `b` = 50 and 150, ok to 4 s and
  skipping from 6 s at `b` = 250; 10 % of requests of 8 s: ok at all budgets and at `0.9 ×`; 5 % failures plus 10 % slow at
  `0.9 ×`: ok. (9) Latency: with instant requests the last series starts exactly at `floor(S / m) × 305 s` for `S` = 10,
  100, 300, 659, 824 in all 48 rounds; with 0.4 s requests 2.0, 5.2, 0, 12.0 and 14.8 s later. **Not modelled:** the real
  network and CCXT, writer contention and yielding to candle writes, Binance's own counting of the window, clock steps.

## Revision history

- **Revision 1** (2026-11-03): initial proposal.
- **Revision 2** (2026-11-03), after the user's review, without changing the architecture, the principles of
  ADR-0001 to ADR-0003, the existing API contracts or the slices FR-1 to FR-5:
  1. every placeholder replaced by measurements (E-10, E-11, E-13) or marked unverified;
  2. the verified span specified formally: closed-interval semantics, page coverage for `n = limit` (`[a, last − 1]`, the
     next page starts at `last`), the coverage merge in the final row transaction, an SQL `merge` that enforces
     adjacency, a proof of the invariant, a crash table, backward history in 90-day chunks, seed-then-backward backfill
     (P-5, P-6, P-7, P-11);
  3. a deterministic scheduler with work classes, a per-series state machine, deadlines, fairness, suspension and long
     outages (P-10);
  4. the rate limit split into local guarantees and what is not guaranteed; a window of 305 s with a left-open boundary;
     class caps; one shared `ExtensionPause` and the proposed amendment A-3 to ADR-0003 (P-9, O-6);
  5. the 5 s alignment check removed as a rejection rule: contract rows versus a heuristic that is only counted (P-3);
  6. `complete`, `window_state`, `conflicted_events` and `events_in_window` defined over the whole requested window,
     identical on every page (P-13);
  7. tests added for the listed cases (P-15);
  8. capacity and latency maths redone with the limiter's exact boundary; revision 1's formula
     `floor(b / 2) × floor((3 600 000 − offset − 15 000) / 300 000)` had an off-by-one against the request index and
     used 300 s instead of the guarded window (P-10).
- **Revision 3** (2026-11-03), after the user's targeted review; no redesign, measurements and decisions unchanged:
  1. `history_pending` no longer has the 24 h tolerance: it is `through_ms − from_ms < backfill_days × 86 400 000`, a
     function of the stored span and the configuration only, so the Collector and the API agree without a clock and the
     moving target cannot reopen finished history (P-10, P-11, P-13); boundary and moving-target tests added (P-15);
  2. history cannot be starved by catch-up: C and H alternate inside the C+H share with one toggle, the guarantee is
     `floor(n / 2)` of `n` consecutive C+H starts (P-9, P-10, P-11), and the starvation test added (P-15). The share of
     hourly rounds, the class caps and the isolation of candle collection are unchanged;
  3. P-13 states that snapshot consistency holds for one HTTP request only, removes the cross-page detection advice
     and the claim that `complete` can only turn from true to false (it can change in either direction), and tests
     the one-request and the no-cross-page cases (P-13, P-15);
  4. consistency pass over P-4, P-7, P-9, P-10, P-11, P-13 and P-15: P-10 no longer points at a `suspended_series`
     health field that P-13 does not have; P-4 no longer says backward requests need a time observation while P-10
     needed one for the target; the cost line in P-11 states its assumption.
- **Revision 4** (2026-11-03), after the user's review of the capacity calculation; verified span, backfill, API and all
  other parts unchanged:
  1. the capacity of P-10 was that of **one isolated round** (an empty limiter window at the wake). A fake-clock simulation
     of 48 consecutive rounds with the window carried over showed it is not sustainable: at the old maximum the first
     group of each round starts 60 s later than in the previous one and every fourth round skips a group of `m`
     series (E-14). The formula, the config validation, the capacity table and the tests are replaced by
     `S_sustained = Q × m − 1` with `Q = min(K + 1, 11)` (1 374, 824 and 274 at the default offset instead of 1 499, 899 and
     299), a refusal above it and a warning above 90 % of it (P-10, P-12, P-15, P-16);
  2. the isolated-round capacity and the sustainable one are stated separately; the reserve for retries and the
     in-flight bound on slow requests are stated from the simulation (P-10);
  3. selection rule 1 orders a round by `UNSEEDED`, then `verified_at_ms`, then `symbol`. The simulation showed that
     symbol order alone leaves the tail of the list unserved for ever when a round cannot finish; no other scheduling
     rule changes (P-10);
  4. the planned latency table is unchanged: its rows are below `S_sustained` and the simulation reproduces them
     exactly (E-14, P-10).
- **Approval** (2026-11-03): the user accepted revision 4 as written, including O-1 to O-6 as proposed.
- **Amendment note** (2026-11-04), with the user's approval, found in the independent review of the FR-1 plan: P-5 first listed "rows not
  strictly ascending by `(fundingTime, rateType)`" as a parse error. That would fix the order of a Regular and a Special
  row that share one `fundingTime`, which no evidence in this ADR supports (the page-coverage rule above is already
  stated "whatever the order of two rows with the same `fundingTime`"). The rule is now: `fundingTime` is
  non-decreasing, the key `(fundingTime, rateType)` is unique, and either order of a coincident pair is accepted.
  Nothing else changes; the implementation (FR-1, `parse_funding_rate_history`) and its tests follow this wording.
