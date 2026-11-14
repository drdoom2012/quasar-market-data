# ADR-0005: Row readability (`readable_by_ms`, `readable_by_unconfirmed`)

- **Status:** Accepted (approved by the user on 2026-11-14: "schvaluji, pokračuj"). Approved items:
  - decision Q5 of the API contract is replaced;
  - ADR-0002 P-2, ADR-0003 and ADR-0004 are amended: the stamp is taken inside the write transaction, `B = 2 000 ms`
    is a contract constant that may only grow, and `ingested_at_ms` is no longer diagnostic;
  - one migration adds the cut-over marker and insert-only transaction and confirmation tables, with a stamp column
    on the confirmation.

  The plan was reviewed independently before this text was written (`docs/superpowers/plans/2026-11-14-readable-by-contract.md`,
  section 5a). **Not yet implemented.** Slices RB-1 to RB-4 (P-10) implement it.
- **Date:** 2026-11-14
- **Scope:**
  - when a stored row of `candles`, `open_interest` and `funding_rates` became readable, and how the API says so;
  - the schema version 4 that records it;
  - how every writer stamps a transaction.

  Not in scope: conflict records, unavailable candles, the funding verified span, coverage, indicators (P-9).
- **Related:**
  - ADR-0001 D-D (time trust, the exchange offset as a diagnostic);
  - ADR-0002 P-1 (`synchronous=FULL`), P-2 (`ingested_at_ms`), P-4 (immutability), P-8 (migrations), P-9 (governance)
    and amendment A-1;
  - ADR-0003 P-6, P-9 and amendment A-5;
  - ADR-0004 P-6, P-9 and amendment A-5;
  - contract `docs/contracts/api-v1.md` sections 4.1 and 6.2.4, decisions RB1 to RB6.
- **Request and agreement:** `docs/requests/2026-11-14-expose-ingestion-time.md` (Sentaris Engine's request), and in
  this order `…-response.md`, `…-reply.md`, `…-response-2.md`, `…-reply-2.md`.

Statements about the current code carry **[C]** (checked on `main` at `9eb385c`, 2026-11-14). Design statements are
**[P]**.

## Context

Sentaris Engine evaluates market data as it was available at an instant `as_of`, both live and in historical replay.
It must never use a row that it could not have read at `as_of`. The API cannot say when a row became readable:

- **Rows carry no time.** They carry no ingestion time (contract decision Q5).
- **The stored stamp is a lower bound with no limit.** It is read before the write [C]: `fetch_once.py:117`,
  `fetch_open_interest_once.py:131`, `fetch_funding_rate_once.py:155`, `open_interest_task.py:805`, and the funding
  page execution. The row commits later, after the executor queue and the SQLite lock wait.
- **Split pages share one stamp.** For open interest and funding, one stamp covers every transaction of a page, and
  the writer yields to candle writes between them [C] (`async_open_interest_writer.py:200-213`,
  `async_funding_rate_writer.py:233-249`).
- **The commit has no hard upper bound.** `COMMIT` includes an fsync (`synchronous=FULL`, `storage/connection.py:50`)
  [C].

ADR-0002 P-2 said the column holds "the wall-clock time the Collector committed the row". The code never did that.

## Part 1 — Agreed with the consumer

| ID | Agreement | Source |
|---|---|---|
| A-1 | One field `readable_by_ms` per row on `GET /v1/candles`, `GET /v1/open-interest` and `GET /v1/funding-rates`: an upper bound on when the row became readable. A row is served as readable at an instant only if its bound has been reached. | request R1, R2; response 1 §3.1; reply 1 §6.1 |
| A-2 | `B = 2 000 ms`; it may only grow within `/v1`. | response 1 §3.2; reply 1 §6.3 |
| A-3 | A commit slower than `B` can only be detected afterwards. Every affected row is recognisable per row. The flag is set by the **absence** of a durable confirmation, so no failure mode leaves a row unflagged: `readable_by_unconfirmed`, `true` exactly when no confirmation is stored for the row's transaction, `false` for rows stored before the cut-over, and changing only from `true` to `false`. | reply 1 §6.2; response 2 §1; reply 2 §1 |
| A-4 | Rows stored before the change are served with `readable_by_ms` set to the cut-over instant. | response 1 §3.4; reply 1 §6.4 |
| A-5 | The field belongs to the first stored value; backfilled and late rows carry the instant they were stored; every ingestion path means the same; the clock is described, not guaranteed; the stamp stays outside identity. | request R3; response 1 §3.5 |
| A-6 | A health counter is welcome as an operational signal; the consumer does not rely on it. | reply 1 §6.2 |

## Part 2 — Proposals (accepted)

### P-1 Terms

- **Stamp** `s`: the value written into `ingested_at_ms` of every row, and into `first_seen_at_ms` of every conflict
  record, of one write transaction.
- **Bound** `B = 2 000 ms`: a contract constant. Growing it keeps every stored confirmation true and makes every
  served bound more conservative. Shrinking it needs `/v2`.
- **Cut-over instant** `X`: an instant at or after the commit of every row stored before schema 4 (P-2).
- **Margin** `M`: the part of `B` reserved for `COMMIT`. It is an implementation constant (proposed 500 ms) and is not
  part of the contract.

### P-2 Schema version 4 (`004_readability.sql`)

```sql
CREATE TABLE readability_cutover (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  cutover_ms   INTEGER NOT NULL CHECK (cutover_ms BETWEEN 946684800000 AND 4102444800000)
) STRICT;
INSERT INTO readability_cutover (id, cutover_ms) SELECT 1, max(
  :applied_at_ms,
  coalesce((SELECT max(ingested_at_ms)   FROM candles), 0),
  coalesce((SELECT max(first_seen_at_ms) FROM candle_conflicts), 0),
  coalesce((SELECT max(ingested_at_ms)   FROM open_interest), 0),
  coalesce((SELECT max(first_seen_at_ms) FROM open_interest_conflicts), 0),
  coalesce((SELECT max(ingested_at_ms)   FROM funding_rates), 0),
  coalesce((SELECT max(first_seen_at_ms) FROM funding_rate_conflicts), 0)) + 1;
-- + readability_cutover_no_update / _no_delete triggers

CREATE TABLE write_transactions (
  table_name   TEXT    NOT NULL CHECK (table_name IN ('candles', 'open_interest', 'funding_rates')),
  stamp_ms     INTEGER NOT NULL CHECK (stamp_ms BETWEEN 946684800000 AND 4102444800000),
  PRIMARY KEY (table_name, stamp_ms)
) STRICT, WITHOUT ROWID;
-- BEFORE INSERT: ABORT unless NEW.stamp_ms > (SELECT cutover_ms FROM readability_cutover WHERE id = 1)
-- + write_transactions_no_update / _no_delete triggers

CREATE TABLE write_confirmations (
  table_name      TEXT    NOT NULL,
  stamp_ms        INTEGER NOT NULL,
  confirmed_at_ms INTEGER NOT NULL CHECK (confirmed_at_ms BETWEEN 946684800000 AND 4102444800000),
  PRIMARY KEY (table_name, stamp_ms)
) STRICT, WITHOUT ROWID;
-- BEFORE INSERT: ABORT unless a write_transactions row with the same (table_name, stamp_ms) exists
-- + write_confirmations_no_update / _no_delete triggers

-- BEFORE INSERT on candles, candle_conflicts, open_interest, open_interest_conflicts, funding_rates and
-- funding_rate_conflicts: ABORT unless a write_transactions row exists with the dataset's table_name and
-- NEW.ingested_at_ms (rows) or NEW.first_seen_at_ms (conflict records)
```

- **How `X` is taken.** The migration SQL stays static and checksummed (ADR-0002 P-8). The runner (`_apply` in
  `storage/migrate.py`) changes in two ways:
  - It reads `applied_at_ms` from an injected clock **after** `BEGIN IMMEDIATE`. Today the value is read before the
    lock (`collector/cli.py:946`) [C]. A transaction of the earlier release that committed during the busy wait would
    then lie after it.
  - It binds the value as the named parameter `:applied_at_ms`.

  The `max` over every stored stamp, plus 1, puts `X` above every old stamp, even when the host clock stepped back
  across the restart. `X` is still a true upper bound, because the lock is held and every old row committed before
  it. The cost is one scan per table, once, during the deployment outage. Every process that migrates does this:
  `run` and the three `fetch-*` commands (`cli.py:963, 1023, 1043`) [C].
- **The registration trigger** is the database-level guard for "every ingestion path" (request R3.3). No row and no
  conflict record can enter without a registered transaction. A writer of an earlier release that is still running
  after the migration fails its inserts. It cannot store a row that would look readable under this ADR. The same
  holds for any future import path.
  - The trigger fires also for an `INSERT … ON CONFLICT DO NOTHING` that turns out to be a no-op (checked on SQLite
    3.53 by the plan review). An identical re-write under a newly registered stamp passes with `rowcount` 0, so the
    writers' duplicate counting is unchanged.
- **Uniqueness.** The key `(table_name, stamp_ms)` makes one stamp name exactly one transaction of one table, across
  processes (`fetch-*` runs next to `run`). Without it, a confirmation could cover another, unconfirmed transaction
  with the same millisecond.
- **`confirmed_at_ms`** is the wall clock read inside the confirmation transaction, after its lock. It is stored and
  not exposed. Its meaning for a consumer is **not defined** here: like the row stamp, it is a pre-commit value. A
  later request to expose when a confirmation became readable (reply 2 §2) would define its bound, presumably `+ B`
  with the same check, without a second migration.
- **No empty transactions.** A transaction that inserted no row, recorded no conflict and merged no funding span is
  rolled back, not committed. It registers nothing.
- The rows already stored are not touched. The migration is additive.
- **Growth (estimate, not measured).** About 40 bytes per key in each of the two tables. Live candle collection
  stores about one new row per symbol per round:
  - 260 symbols: about 75 000 transactions a day, about 6 MB a day or 2.2 GB a year for both tables;
  - 600 symbols: about 5 GB a year, against about 6 to 7 GB a year of candles (ADR-0002 P-3).

  The ratio is high because a live transaction holds one row. Batching several symbols into one transaction would
  lower it, but would change the isolation rules (ADR-0003 P-9), so it is not proposed.

### P-3 Stamping (every writer, on its executor thread)

1. `BEGIN IMMEDIATE`. The write lock is held from here on. Lock waits, executor queueing and earlier transactions of
   the same page are outside the measured interval.
2. `t0 = monotonic`.
   `s = max(wall_ms, X + 1, coalesce((SELECT max(stamp_ms) FROM write_transactions WHERE table_name = ?), 0) + 1)`.
   A stamp later than the wall clock is still a correct base for an upper bound.
3. `INSERT INTO write_transactions`, then the rows and conflict records with `s`.
4. Before `COMMIT`:
   - nothing inserted, recorded or merged: `ROLLBACK`, return the report;
   - `monotonic − t0 > B − M`: `ROLLBACK` and restart at 1 with a fresh stamp. After a fixed number of attempts
     (proposed 3), raise the dataset's new "**not stored**" error, for example `CandleWriteNotStored`.

   A row is never committed with a stamp older than `B − M` at `COMMIT`.
5. `COMMIT`. `t1 = monotonic`.
6. `t1 − t0 ≤ B`: write the confirmation in its own short transaction on the same connection. `t1 − t0 > B`: no
   confirmation. Log an error line with the table, the stamp, the list of series, the time range of the rows and the
   duration.
7. An outcome-unknown `COMMIT` (the existing error paths) gets no confirmation. A retry of the same rows registers a
   new stamp. Rows that the first attempt stored keep the first stamp, which stays unconfirmed. This is correct: that
   commit was never measured.
8. A forward step of the wall clock pins later stamps ahead through `last + 1`. This is safe, but it delays
   availability, so the writer warns when `s > wall_ms + B`.

Error handling:

- **"Not stored" is never fatal.** The callers treat it like a failed attempt of that symbol or page. For candles,
  `collection_loop.py:172` handles it next to `CandleWriteOutcomeUnknown`. Today every other `CandleWriteError`
  becomes `FatalLoopError` (`collection_loop.py:438`) [C], and a slow disk must not stop the Collector. For open
  interest and funding it is a failed page, retried as today.
- **A failed confirmation is never a write failure.** The data is committed, and the write returns its normal
  report. The writer logs the failure and keeps the confirmation in memory. It inserts it inside its next **committed** data
  transaction; a data transaction that is rolled back (empty or restamped) carries it on to the next one. It is lost
  when the process stops. The rows then stay unconfirmed, which fails closed.

The interface changes from "the caller passes `ingested_at_ms`" to "the writer stamps". This is an adapter change in
`collector/`; the domain still reads no clock. Funding keeps its caller clock for `SpanMerge.verified_at_ms`
(`funding_rate_execution.py:120-134`) [C], which is not part of this change.

### P-4 What the API serves per row

`X` is read once per request with `SELECT cutover_ms FROM readability_cutover WHERE id = 1`, inside the request's one
read transaction:

| Row | `readable_by_ms` | `readable_by_unconfirmed` |
|---|---|---|
| `ingested_at_ms < X` (stored before the cut-over) | `X` | `false` |
| `ingested_at_ms > X`, a confirmation exists | `ingested_at_ms + B` | `false` |
| `ingested_at_ms > X`, no confirmation | `ingested_at_ms + B` | `true` |

- `ingested_at_ms = X` cannot occur: old stamps are below `X` (P-2), new ones above it (the trigger).
- **The first row is a correct bound.** `X` was taken under the write lock and lies above every old stamp, so every
  old row had committed before it. A writer of the earlier release cannot add an old-style row afterwards (the
  registration trigger).
- **The second row is a correct bound.** A confirmation exists only when `COMMIT` ended within `B` of `t0`
  (monotonic), and `s ≥ wall_ms` at `t0`. The bound holds unless the host wall clock steps backwards between `t0`
  and the end of `COMMIT`. Such a step is not detected, and its effect is limited to its size (P-8).
- **`readable_by_ms` of an unconfirmed row is not a bound.**
- `ingested_at_ms` and `confirmed_at_ms` are not exposed.
- **The query.** One `LEFT JOIN write_confirmations` on the primary key in the existing row query, and the cut-over
  read by key:
  - the statement count stays independent of the rows (`test_the_statement_count_does_not_depend_on_the_rows`);
  - no statement scans (`test_every_statement_uses_a_primary_key_or_index_search`).
- **Lifetime.** The values are stable, and the change from `true` to `false` is one-way, within one database. A
  restore from a backup drops the confirmations written after the backup, so a row can turn from `false` back to
  `true`. Migrating a schema-3 backup gives a new `X`. A restore is announced to consumers (README, RB-4).

### P-5 Health: the `readability` object of `GET /health/ingest`

The last key of the body, after `funding_rate`, is present whatever datasets are enabled:

```json
"readability": {
  "bound_ms": 2000,
  "cutover_ms": 1792600000000,
  "window_ms": 86400000,
  "unconfirmed": {
    "candles":       { "transactions": 0, "newest_stamp_ms": null },
    "open_interest": { "transactions": 1, "newest_stamp_ms": 1792674000000 },
    "funding_rates": { "transactions": 0, "newest_stamp_ms": null }
  }
}
```

- **What is counted.** Registered transactions with `as_of_ms − window_ms < stamp_ms` and
  `stamp_ms + bound_ms ≤ as_of_ms` that have no confirmation. A transaction still within `B` is not counted. The read
  is a range on the primary key. `newest_stamp_ms` is the newest such stamp, not a detection time.
- **Information only.** It never changes `status` or an HTTP status. A consumer must not rely on it; the per-row
  flag is the guarantee.
- **The window** is a constant (24 h), echoed as `window_ms`, so it can change without a contract break. Response 1
  §3.3 described a cumulative counter plus the instant of the last violation. A cumulative count would read every
  transaction ever written, so it is replaced by the window. Older unconfirmed transactions stay visible per row.

### P-6 Isolation (amends ADR-0003 P-9 and ADR-0004 P-9)

Each executor job of open interest and funding now takes the write lock twice: once for the data transaction and
once for its single-row confirmation. Before, a candle write waited for at most one extension transaction. Now it
waits for at most one extension transaction plus its confirmation. The P-9 benchmark (`make bench-oi-write`) measures
the pair (RB-4).

### P-7 Every ingestion path

`run` with its backfill, `fetch-once`, `fetch-open-interest-once` and `fetch-funding-rate-once` all write through the
three writers, so they stamp in the same way. Any other writer fails the registration trigger (P-2). No import path
exists. If one is added, it must register and confirm the same way, or its rows must be distinguishable.

### P-8 Clock

- The fields use the **Collector host's wall clock**. NTP is assumed and not checked.
- The offset to the exchange (ADR-0001 D-D) is logged only, never stored or exposed.
- The API's `as_of_ms` comes from the API host, which may be a different machine.
- A backward step of the wall clock between `t0` and the end of `COMMIT` is not caught by the monotonic check. Its
  effect is limited to the size of the step.

Quasar promises nothing more about the clock.

### P-9 What the fields do not cover

Rows only. The fields do not cover:

- conflict records: their stamps are registered but not exposed. The conflicted flag of a row can appear after the
  row was readable, as before;
- `unavailable_candles`;
- the funding verified span;
- the coverage blocks, `/v1/coverage`, `/v1/open-interest/coverage`;
- indicators: an indicator value carries no readability. A consumer that needs a point-in-time view computes from
  rows.

There is no point-in-time query parameter.

### P-10 Deployment and slices

**Deployment** follows ADR-0003 P-14 and ADR-0004 P-14:

- schema 3 → 4;
- a short, announced API outage (an API of schema 3 answers `503 not_ready` after migration 4, and the reverse);
- no rollback by redeploy, only a restore of the backup.

**Precondition:** every Collector-side process of the earlier release is stopped before the new one starts, and no
ad-hoc `fetch-*` process runs. `docker compose up -d` recreates the collector container. A violated precondition
causes failed writes, not wrong rows (P-2).

**Slices** (one branch and one pull request, after the contract is published):

- **RB-1:** migration 4, the runner change, the triggers and their negative tests.
  - The tests cover:
    - an unregistered insert aborts in each of the six tables;
    - the cut-over guard;
    - immutability;
    - `X` lies above every old stamp, also with a host clock behind the stored stamps;
    - `X` is read after the lock;
    - the migration is idempotent and restart-safe.
  - **A seeding helper** for the many tests that insert rows directly: it seeds at schema 3 and then migrates to 4
    for old-style rows, or registers and confirms for new-style rows. `tools/bench_open_interest_write.py` registers.
- **RB-2:** the stamping of P-3 in the three writers and their callers. Fake monotonic and wall clocks cover:
  - restamp and the retry limit;
  - "not stored" is not fatal;
  - no confirmation above `B`;
  - a failed confirmation is carried to the next committed transaction and is not a write failure;
  - an outcome-unknown commit;
  - one stamp per transaction of a split page with interleaved candle writes;
  - uniqueness across two writer connections.
- **RB-3:** the API fields on the three endpoints and the `readability` object. The tests cover every row of the P-4
  table and check that every other field stays byte-identical. `tests/api/test_funding_rate_health.py::test_funding_rate_is_followed_only_by_readability_and_null_when_not_enabled`
  (INV-137) changes to "followed only by `readability`", and the contract examples of 5.2, 6.5.4 and 6.7.4 gain the two
  fields (their tests compare them with the server).
- **RB-4:** the README deployment text, with the precondition and the restore notice; the benchmarks
  (`bench-oi-write` for the pair, `bench-health`, `bench-health-oi`, and a timing of the candle writer); the
  invariants INV-140 to INV-145 turned ENFORCED; and the notice to Sentaris.

## Consequences

- A consumer can tell, per row, whether the row was readable at an instant, and the guarantee fails closed. A row
  without a confirmation is untrusted whatever went wrong.
- Replay before the cut-over, and before a row's own backfill, sees no available data. That is the consumer's
  accepted consequence (reply 1 §6.4).
- Storage grows by about 2 to 5 GB a year at 260 to 600 symbols (P-2). This is not measured; the soak test should
  measure it.
- Every writer and its callers change interface, and about 25 test and bench files that seed rows directly need the
  seeding helper. This is the largest cost of the implementation.
- An unknown writer fails hard instead of storing. This is intended.

## Revision history

- **Revision 1** (2026-11-14): written from the reviewed plan, accepted by the user with the scope stated in the
  status line.
