# Plan: row readability (`readable_by_ms`, `readable_by_unconfirmed`), contract and decisions

- **Date:** 2026-11-14
- **Branch:** `docs/readable-by-contract`
- **Scope of this branch:** documents only. The new ADR-0005, the amendments to ADR-0002, ADR-0003 and ADR-0004, the
  contract sections, PLANNED invariants, follow-ups and the request exchange. No production code, no migration file,
  no test other than the invariant register's own integrity test. The implementation is a later branch built on the
  published contract. Sentaris asked for the contract to be published before the implementation, because its client
  is built against the contract.
- **Approval:** the user approved on 2026-11-14 ("schvaluji, pokračuj"): Q5 replaced; ADR-0002 P-2, ADR-0003 and
  ADR-0004 amended (stamp inside the transaction, `B = 2 000 ms` as a contract constant that may only grow,
  `ingested_at_ms` no longer diagnostic); one migration with the cut-over marker and the transaction and confirmation
  tables (insert-only), with a stamp column on the confirmation.
- **Inputs:** `docs/requests/2026-11-14-expose-ingestion-time.md` (request), `…-response.md`, `…-reply.md`,
  `…-response-2.md`, `…-reply-2.md` (agreed result). Code facts checked on `main` at `9eb385c`:
  - The stamp is read before the write: `fetch_once.py:117`, `fetch_open_interest_once.py:131`,
    `open_interest_task.py:805`, and the funding page execution. The writers pass it through:
    `candle_writer.py:154`, `open_interest_writer.py`, `funding_rate_writer.py:267`.
  - One stamp per page is shared by all transactions of an open interest or funding page. Between those transactions
    the writer yields to the candle writer: `async_open_interest_writer.py:200-213`,
    `async_funding_rate_writer.py:233-249`.
  - `synchronous=FULL` (`storage/connection.py:50`), so `COMMIT` includes an fsync.
  - The three row tables are `WITHOUT ROWID` (ADR-0002 P-2, ADR-0003 P-6, ADR-0004 P-6). A row cannot be tied to its
    transaction by `rowid`.
  - A writer transaction is not restricted to one series. `fetch-once` runs as its own process next to `run`.
  - The API never reads `ingested_at_ms` today.

## 1. Where the design lives

A **new ADR-0005 "Row readability"** holds the whole design: the schema, the stamping, the confirmation, the cut-over,
the API fields, the health block, the clock, the deployment and the slices. ADR-0002, ADR-0003 and ADR-0004 each get
a short dated **amendment** entry that names what changes in them and points to ADR-0005. This keeps one place for a
cross-cutting rule that touches all three datasets, instead of three copies that could drift. ADR-0002 P-9 already
says that later tables come "by new migrations, each preceded by approval of an amendment or a new ADR". The
user-approved scope ("amend ADR-0002 P-2, ADR-0003, ADR-0004") is met by the amendment entries. ADR-0005 is their
common body. **Review question R-Q1:** is a new ADR acceptable, or should the full text go into ADR-0002 as an
amendment?

## 2. The design to be written (ADR-0005 content)

### D-1 Terms

- **Stamp** `s`: the value written into `ingested_at_ms` of every row (and `first_seen_at_ms` of every conflict
  record) of one write transaction.
- **Bound** `B = 2 000 ms`: a contract constant. It may only grow within `/v1`. Growing it keeps every stored
  confirmation true and makes every served bound more conservative. Shrinking it needs `/v2`.
- **Cut-over instant** `X`: an instant at or after the commit of every row stored before migration 4. It is computed
  inside the migration's own transaction, after the write lock is held (D-2a).

### D-2 Schema version 4 (migration `004_readability.sql`)

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
-- + no_update / no_delete triggers

CREATE TABLE write_transactions (
  table_name   TEXT    NOT NULL CHECK (table_name IN ('candles', 'open_interest', 'funding_rates')),
  stamp_ms     INTEGER NOT NULL CHECK (stamp_ms BETWEEN 946684800000 AND 4102444800000),
  PRIMARY KEY (table_name, stamp_ms)
) STRICT, WITHOUT ROWID;
-- BEFORE INSERT trigger: NEW.stamp_ms > (SELECT cutover_ms FROM readability_cutover), else ABORT
-- + no_update / no_delete triggers

CREATE TABLE write_confirmations (
  table_name      TEXT    NOT NULL,
  stamp_ms        INTEGER NOT NULL,
  confirmed_at_ms INTEGER NOT NULL CHECK (confirmed_at_ms BETWEEN 946684800000 AND 4102444800000),
  PRIMARY KEY (table_name, stamp_ms)
) STRICT, WITHOUT ROWID;
-- BEFORE INSERT trigger: a write_transactions row with the same key must exist, else ABORT
-- + no_update / no_delete triggers

-- on candles, open_interest, funding_rates and their three conflict tables:
-- BEFORE INSERT trigger: a write_transactions row (table_name of the dataset, NEW.ingested_at_ms or
-- NEW.first_seen_at_ms) must exist, else ABORT
```

- **D-2a: how `X` is taken.** The migration SQL stays static and checksummed. The runner (`storage/migrate.py`
  `_apply`) changes in two ways:
  - It reads `applied_at_ms` from an injected clock **after** `BEGIN IMMEDIATE`. Today `cli.py:946` reads it before
    the lock, so a transaction of the earlier release that commits during the busy wait would lie after it.
  - It binds `applied_at_ms` as the named parameter `:applied_at_ms` to every statement that uses it.

  The `max` over the six stamp columns makes `X` lie above every stored stamp. A host clock that stepped back across
  the restart therefore cannot leave an old row above `X`, and every row stored before the cut-over is served
  `false`, as promised to Sentaris (response 2, section 1). It is still a true upper bound, because the lock is held
  and every old row committed before it. The cost is one scan per table, once, during the accepted outage. Every
  process that migrates gets this: `run` and the three `fetch-*` commands (`cli.py:963, 1023, 1043`). The tests stay
  deterministic, because the clock is injected.
- The `max(...)` is the SQLite scalar `max` of several arguments. It needs no `unixepoch()`, which would need
  SQLite 3.38 against `MIN_SQLITE_VERSION` 3.37.
- **Verified by the plan review (SQLite 3.53):** a BEFORE INSERT trigger fires also for an
  `INSERT … ON CONFLICT DO NOTHING` that turns out to be a no-op. An identical re-write under a newly registered
  stamp passes with `rowcount` 0, so the writers' duplicate counting is unchanged. An unregistered stamp aborts even
  for an identical duplicate, which is intended.
- The **registration trigger** on the six data tables is the database-level guard for request R3.3 ("every
  ingestion path"). No row can enter without a registered transaction. A writer of an older release, still running
  after the migration, aborts instead of storing a row that looks readable under the new rule but is not. This holds
  also for a future import path.
- **Stamp uniqueness:** the key `(table_name, stamp_ms)` makes one stamp name exactly one transaction of one table,
  across processes. Without it, a confirmation could cover another, unconfirmed transaction with the same
  millisecond.
- **`confirmed_at_ms`** is the wall clock read inside the confirmation transaction after its lock, the column the
  user approved. It is stored and not exposed. Its meaning for consumers is **not** defined now: it is a pre-commit
  value, like the row stamp. Sentaris's possible later request (reply 2, section 2) would define a bound for it,
  presumably `+ B` with the same check, without a second migration. The ADR says so. No other diagnostic column is
  added.
- Rows already stored are not touched. The migration is additive.
- **No empty transactions.** A transaction that inserted no row, recorded no conflict and, for funding, merged no
  span is rolled back instead of committed. It then registers nothing and needs no confirmation. Most writes in a
  steady round re-write duplicates, so this keeps the tables small.
- **Growth estimate (not measured):** about 40 bytes per key in each of the two tables. Live candle collection
  stores about one new row per symbol per round. At 260 symbols that is about 75 000 transactions a day, about 6 MB a
  day or 2.2 GB a year for both tables. At 600 symbols it is about 5 GB a year, against about 6 to 7 GB a year of
  candles (ADR-0002 P-3). The ratio is high because live transactions hold one row each. This is a stated cost.
  Batching several symbols into one transaction would lower it, but would change the isolation rules, so it is not
  proposed.
- **Benchmarks (R-Q2, answered by the review):** in RB-4, re-run `make bench-oi-write` measuring the data
  transaction plus its confirmation, `make bench-health` and `make bench-health-oi` with the readability object,
  and add a timing of the candle writer.

### D-3 Stamping (Collector, every writer)

Per write transaction, inside the writer, on the executor thread:

1. `BEGIN IMMEDIATE`. The write lock is held from here on, so lock waits, executor queueing and earlier
   transactions of the same page are outside the measured interval.
2. Read `t0 = monotonic`. Set
   `s = max(wall_ms, X + 1, coalesce((SELECT max(stamp_ms) FROM write_transactions WHERE table_name = ?), 0) + 1)`.
   A larger `s` than the wall clock is still a correct upper-bound base. The `max` keeps `s` unique and
   increasing while the lock is held.
3. `INSERT INTO write_transactions`, then the rows and conflict records with `s`.
4. Before `COMMIT`: if nothing was inserted, recorded or merged, then `ROLLBACK` and return the report (D-2, no
   empty transactions). If `monotonic - t0 > B - M`, where `M` is a margin for the commit (an implementation
   constant, proposed 500 ms, not part of the contract), then `ROLLBACK` and restart at 1 with a fresh stamp. After a
   fixed number of attempts (proposed 3), the writer raises a new error class per dataset, for example
   `CandleWriteNotStored`, meaning "certainly not written, retry later". A row is never committed with a stamp older
   than `B - M` at `COMMIT`.
   - **Error handling of the new class:** the callers treat it like a failed attempt of that symbol or page, never as
     fatal. For candles this means `collection_loop.py:172` adds it next to `CandleWriteOutcomeUnknown`. Today every
     other `CandleWriteError` becomes `FatalLoopError` (`collection_loop.py:438`), and a slow disk must not stop the
     Collector. For open interest and funding, it is a failed page that is retried as today.
5. `COMMIT`. Read `t1 = monotonic`.
6. If `t1 - t0 <= B`: write the confirmation in its own short transaction (`BEGIN IMMEDIATE`, insert, `COMMIT`) on
   the same connection. If `t1 - t0 > B`: no confirmation. An error line is logged with the table, the stamp, the
   list of series, the time range of the rows and the duration (response 1, section 3.3, promised the series and the
   time range).
   - **A failed confirmation is never a write failure.** The data is committed, and a caller that saw an error
     might retry or exit. The writer logs the failure and keeps the confirmation in memory. It inserts it inside that
     writer's next committed data transaction (a rolled-back one carries it on). It is lost when the process stops. The write itself returns its normal report.
7. Outcome-unknown `COMMIT` (the existing error paths): no confirmation. A retry of the same rows registers a new
   stamp. Rows that the first attempt did store keep the first stamp, which stays unconfirmed. That is correct: its
   commit was never measured.

8. A forward step of the wall clock pins later stamps ahead through `max(..., last + 1)`. This is safe: the bound is
   only more conservative. It delays availability, though, so the writer logs a warning when `s > wall_ms + B`.

The interface changes from "the caller passes `ingested_at_ms`" to "the writer takes the clock and stamps". This is
an adapter change in `collector/`; the domain still reads no clock. The callers lose their `ingested_at_ms`
argument:

- `fetch_once.py:117`
- `fetch_open_interest_once.py:131`
- `fetch_funding_rate_once.py:155`
- `open_interest_task.py:805`
- the funding page execution.

Funding keeps a caller clock for `SpanMerge.verified_at_ms` (`funding_rate_execution.py:120-134`). That value is not
part of this change.

**Isolation (ADR-0003 P-9, the ADR-0004 counterpart):** each executor job of open interest and funding now holds the
lock twice, once for the data and once for the confirmation. The guarantee "a candle write waits for at most one
extension transaction" becomes "at most one extension transaction and its confirmation". The confirmation is a
single-row insert. Both ADRs amend P-9, and the benchmark measures the pair (D-2).

### D-4 What the API serves per row

With `X` read once per request (inside the same read transaction):

| Row | `readable_by_ms` | `readable_by_unconfirmed` |
|---|---|---|
| `ingested_at_ms < X` (stored before the cut-over) | `X` | `false` |
| `ingested_at_ms > X` and a confirmation exists | `ingested_at_ms + B` | `false` |
| `ingested_at_ms > X` and no confirmation | `ingested_at_ms + B` | `true` |

(`ingested_at_ms = X` cannot occur. Old stamps are below `X` by D-2a, and new stamps above it by the trigger.)

- **Why the first row is a correct bound:** `X` is taken by the migration under the write lock, and it lies above
  every stored stamp (D-2a). Every row stored before it committed before the lock was taken. This holds whichever
  process migrates. A writer of the earlier release that is still running cannot insert after the migration (the
  D-2 registration trigger), so no old-style row can appear later. Every old row is `false` (response 2,
  section 1).
- `readable_by_ms` of an unconfirmed row is **not** a bound; the contract says so.
- The SQL is one `LEFT JOIN write_confirmations` on the primary key in the existing row query, plus one
  `SELECT cutover_ms FROM readability_cutover WHERE id = 1` per request. The `WHERE id = 1` makes it a key search, not
  a `SCAN`, as `tests/api/test_funding_rate_page.py::test_every_statement_uses_a_primary_key_or_index_search`
  requires. The statement count stays independent of the rows (`test_the_statement_count_does_not_depend_on_the_rows`).
- **Lifetime of a database:** the one-way change and the stable values hold within one database. A restore from a
  backup drops the confirmations written after the backup, so a row can turn from `false` back to `true`. Migrating
  a schema-3 backup gives a new `X`. The contract states this, and the README says that a restore is announced to
  consumers.

### D-5 Health: the `readability` object of `GET /health/ingest`

A new last key `readability` (after `funding_rate`), present whatever datasets are enabled:

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

- It counts registered transactions with `as_of_ms - window_ms < stamp_ms` and `stamp_ms + bound_ms <= as_of_ms` that
  have no confirmation. Transactions still within `B` are not counted. The read is a range on the primary key of
  `write_transactions`.
- It is information only: it never changes `status` or an HTTP status. The window keeps the read bounded; older
  unconfirmed transactions stay visible per row, never in this counter. The contract states that a consumer must
  not rely on it (Sentaris agreed, reply 1, 6.2).
- **Window (R-Q3, answered):** a constant, echoed as `window_ms`. It may change without a contract break, and the
  contract says so. This is a **change from response 1, section 3.3**, which described a cumulative counter plus the
  instant of the last violation. A cumulative count would scan every transaction ever written. Section 6.2.4 and the
  notice to Sentaris state the change. Sentaris does not rely on the counter (reply 1, 6.2).
- `newest_stamp_ms` is the newest unconfirmed stamp in the window, not a time of detection.

### D-6 Clock

The fields use the Collector host's wall clock. NTP is assumed and not checked. The exchange offset (ADR-0001 D-D) is
logged only. The API's `as_of_ms` comes from the API host. A backward step of the wall clock between stamp and
commit is not caught by the monotonic check, and its effect is limited to the size of the step. The contract states
this and promises nothing more (response section 3.5 item 4).

### D-7 What the fields do not cover

Rows only. Not conflict records (their stamp is registered but not exposed; Sentaris's possible later request), not
`unavailable_candles`, not the funding verified span, not coverage blocks, indicators or `/v1/coverage`. The contract
states that an indicator value carries no readability, and that a consumer that needs a point-in-time view computes
from rows.

### D-8 Deployment

As ADR-0003 P-14 and ADR-0004 P-14, with the following:

- schema 3 → 4;
- the short outage of the API;
- no rollback by redeploy, only a restore of the backup.

**Precondition:** every Collector-side process of the earlier release is stopped before the new Collector starts, and
no ad-hoc `fetch-*` process is running. `docker compose up -d` recreates the collector container (stop, then start).
The D-2 trigger turns a violation of the precondition into failed writes, not wrong rows. The README procedure gains
the precondition in the implementation slice.

### D-9 Slices of the implementation (a later branch, one PR, per the user's one-PR rule)

- **RB-1** migration 4, the runner change (D-2a), the triggers, and the negative tests:
  - an unregistered insert aborts for each of the six tables;
  - the cut-over guard;
  - immutability;
  - `X` lies above every old stamp, also with a host clock behind the stored stamps;
  - `X` is read after the lock;
  - the migration is idempotent and restart-safe (response 1, section 4).

  **A seeding helper for the tests:** about 25 test and bench files insert rows directly (`tests/api/seed.py`,
  `tests/storage/test_schema_v*.py`, `tests/api/bench_*.py`, `tests/deploy/seed_db.py`,
  `tools/bench_open_interest_write.py`). Fixed past stamps can never be registered after migration 4. The helper
  seeds at schema 3 (`migrate(migrations=load_migrations()[:3])`) and then applies migration 4, which gives
  old-style rows. For new-style rows it registers a transaction and, if wanted, a confirmation.
- **RB-2** the stamping in the three writers (D-3), callers updated, tests with fake monotonic and wall clocks
  (restamp, retry limit, no confirmation above `B`, failed confirmation retried once, outcome-unknown commit, one
  stamp per transaction of a split page with interleaved candle writes, cross-process uniqueness).
- **RB-3** the API fields on the three endpoints and the `readability` health object, with tests for each row of the
  D-4 table, and byte-identical other fields.
- **RB-4** the README deployment text, the benchmark re-run, the invariants turned ENFORCED, and the release notes for
  Sentaris.

## 3. Changes in this branch, file by file

1. `docs/adr/0005-row-readability.md`, new. Status: Accepted by the user on 2026-11-14. The structure follows
   ADR-0003 and ADR-0004:
   - context: the request and the agreed exchange, with the paths;
   - Part 1, already decided: the Sentaris agreement points;
   - Part 2, proposals P-1 to P-10: D-1 to D-9 above (as written: P-1 terms, P-2 schema, P-3 stamping, P-4 API,
     P-5 health, P-6 isolation, P-7 ingestion paths, P-8 clock, P-9 scope, P-10 deployment and slices);
   - consequences;
   - open decisions: none, or the review questions if any stay open;
   - revision history.
2. `docs/adr/0002-sqlite-schema-v1.md`:
   - The P-2 bullet on `ingested_at_ms` (`:65`) is corrected. It said "the wall-clock time the Collector committed the
     row", "diagnostic, no logic depends on it" and "The caller supplies it". It now says: the stamp of the row's write
     transaction, taken by the writer after the write lock is held, the base of `readable_by_ms`. It is not
     diagnostic since schema 4, and it stays excluded from the identical-duplicate comparison. The domain still reads
     no clock.
   - P-8 gains a note on the `:applied_at_ms` parameter and on reading it after the lock (D-2a).
   - A dated amendment entry is added at the end, naming the user's approval, ADR-0005 and the schema version 4. It
     also records that the original wording described a commit time the code never took.
3. `docs/adr/0003-open-interest.md` and `docs/adr/0004-funding-rate.md`:
   - An amendment entry **A-5** is added to both ADRs (A-4 is the last label in each). It states:
     - one stamp per page is replaced by one stamp per transaction taken inside the writer, which ends the shared
       stamp;
     - the new error class that means "not stored" and is retried as a failed page;
     - the P-9 amendment: a candle write waits for at most one extension transaction plus its confirmation (D-3,
       isolation);
     - the deployment of schema 4, with a pointer to ADR-0005.
   - The P-6 schema text and the P-9 text each gain a one-line note pointing to A-5.
   - No other rule changes.
4. `docs/contracts/api-v1.md`:
   - **Status line:** readability sections approved on 2026-11-14 (ADR-0005), not yet implemented. Until they are
     implemented, the deployed API does not send the fields. That is compatible, because a client must not assume
     an unknown field is there.
   - **§0:** a new item 7. Every row of the three row endpoints carries `readable_by_ms` and
     `readable_by_unconfirmed`, defined in §4.1. A row with `readable_by_unconfirmed: true` should be treated as
     untrusted, like `conflicted`.
   - **§1 Time row:** add `readable_by_ms` to the list of epoch-ms fields.
   - **New §4.1 "Readability of a row":** the normative definition, written once:
     - the meaning sentence;
     - the D-4 table, with the bound and the exception (confirmation absent);
     - the one-way change (`true` → `false`);
     - first stored value; backfilled and late rows carry the instant they were stored;
     - every ingestion path; old rows and the cut-over;
     - the clock (D-6) and the scope (D-7);
     - `B` and the rule that it may only grow;
     - that `readable_by_ms` of an unconfirmed row is not a bound;
     - what is not exposed (`ingested_at_ms`, `confirmed_at_ms`).
   - **§5.2, §6.5.4, §6.7.4:** *as written, after `make check`:* the JSON examples stay as the current server answers,
     because `test_the_contract_example_is_what_the_server_returns` compares them with the server. Each "exactly the
     fields shown" bullet says the two fields are added with RB-3, which also updates the examples. One illustrative
     row with both fields is in §4.1.
   - **§6.2.2 (`:334`):** the schema version becomes 4 ("4 since RB-1, ADR-0005"). It is the value of the
     implementing build, so the text says the version "of this build".
   - **§6.2.3:** the `readability` bullet in the list of objects ("last key, after `funding_rate`"). The
     `funding_rate` bullet (`:374`) says it is "the **last key**" and becomes "followed by `readability`". A new
     subsection **§6.2.4** specifies it (D-5), including the window that is not cumulative.
   - **§6.5.1 (`:521`) and §6.7.1 (`:758`):** "The response carries no `ingested_at_ms` (diagnostic … Q5)" becomes:
     it carries no `ingested_at_ms`; it carries `readable_by_ms` and `readable_by_unconfirmed` (§4.1, RB1).
   - **Status and Inputs lines** at the top: ADR-0005 and the request documents are added.
   - **§7:** add a point-in-time query (`as_of` parameter) and the readability of conflict records, spans, coverage
     and indicators as out of scope.
   - **§8:** traceability rows for §4.1 and §6.2.4 → ADR-0005 P-x, the request documents, and the new decisions.
   - **§9:** the Q5 row is kept for history and marked "superseded on 2026-11-14 by RB1". A new block of decisions,
     answered by the user on 2026-11-14:
     - RB1: expose `readable_by_ms` as an upper bound, not the raw stamp;
     - RB2: `B = 2 000 ms`, contract constant, may only grow;
     - RB3: the inverted confirmation flag instead of a violation record;
     - RB4: the cut-over instant for old rows;
     - RB5: the registration trigger;
     - RB6: the health object as information only.
     Each row carries the rejected alternative and its cost, like the existing tables. The Q5 cross-references in
     OA7 and FA8 get "(superseded by RB1 for rows)".
5. `docs/invariants.md`: new PLANNED rows with target "ADR-0005 RB-n", no ENFORCED claim:
   - INV-140, registration: no row or conflict record of the six tables can be inserted without a registered
     transaction. Target RB-1.
   - INV-141, uniqueness: a stamp names one transaction per table, and stamps are strictly above the cut-over.
     Target RB-1.
   - INV-142, the stamp is taken after the write lock; a transaction older than `B - M` at `COMMIT` is rolled back
     and restamped. Target RB-2.
   - INV-143, a confirmation is written only for a transaction whose measured stamp-to-commit-end time is at most
     `B`, and is never written for an outcome-unknown commit. Target RB-2.
   - INV-144, the served fields follow the D-4 table. Every other field of the three endpoints is unchanged. Target
     RB-3.
   - INV-145, the `readability` health object is information only and never changes a status. Target RB-3.
6. `docs/follow-ups.md`: one entry, **F-25** "Possible later requests from Sentaris". It covers `first_seen_at_ms` of
   conflict records with the readability meaning, the readable time of a confirmation (stored as `confirmed_at_ms`
   since schema 4), and the publication delay in force at write time. None of them is scheduled.
7. `docs/requests/`: the five exchange documents are committed as they are. They are the record of the agreement.
8. `docs/architecture.md` §5.2 (R-Q4, answered): one line saying that the Collector also owns the readability
   tables, with a pointer to ADR-0005. No boundary change.
9. Not changed: `README.md`. The deployment text, with the precondition and the restore notice, comes with RB-4.

R-Q1 (answered by the review): a new ADR-0005 with short amendments. ADR-0002 P-9 allows "an amendment or a new
ADR", and the rule cuts across three datasets.

## 4. Checks

- `make check` (the invariant register integrity test covers the new rows). Nothing else runs against documents.
- Consistency read: every number and name in the contract matches ADR-0005 (`B`, `M` not in the contract, the field
  names, the trigger, the window).

## 5. Workflow

1. Independent review of this plan by a fresh read-only agent on Opus, against the request documents, the ADRs, the
   contract and the code facts above. All findings applied.
2. Write the documents (section 3).
3. Independent review of the diff by a fresh read-only agent on Opus (a review of the documents against the agreed
   exchange and the code). All findings applied.
4. Commit and PR with a squash-ready title and body. No merge, tag or deployment without the user.
5. Draft for the user: a short notice to Sentaris that the contract is published, sent once the PR is merged.

## 5a. Plan review (2026-11-14, independent, Opus, read-only): findings and how they were applied

1. `X` was not specified and was read before the lock: D-2a takes it in the migration's transaction, after the lock,
   from an injected clock, and every migrating process is named.
2. Old rows above `X` would have been `true`, against response 2: `X` is the `max` over every stored stamp plus 1
   (D-2a), and D-4 no longer has that case.
3. The errors of a restamp failure and of a failed confirmation were not specified: a new non-fatal "not stored"
   class, and a confirmation failure is never a write failure (D-3).
4. P-9 of ADR-0003 and ADR-0004 also changes: amendment A-5 covers it, and the benchmark measures the pair (D-3, §3).
5. Missed contract and ADR places: added in §3 (`:334`, `:374`, `:521`, `:758`, the status lines, ADR-0002 `:65`).
6. The test seeding impact: the seeding helper in RB-1.
7. The wrong invariant was cited, and the cut-over read would have been a `SCAN`: fixed in D-4 (`WHERE id = 1`,
   the real test names).
8. Growth and cost: no empty transactions, an estimate, and benchmarks (D-2).
9. Gaps against response 1: the log names the series list and the time range, the window that is not cumulative is
   stated, and an idempotent-migration test is added (D-3, D-5, RB-1).
10. Backup and restore: the lifetime statement (D-4).
11. Loose ends:
    - `commit_ns` dropped; `confirmed_at_ms` kept, as approved, with its meaning left undefined;
    - the funding `verified_at_ms` caller clock noted;
    - `fetch_funding_rate_once.py:155` added;
    - F-25;
    - A-5 in both ADRs;
    - the forward-step warning.

## 6. Risks

- **Contract-first:** the fields are promised before they exist. Mitigation: the status line states "not yet
  implemented". The engine already fails closed on a missing field.
- **D-3 step 6, a failed confirmation:** if the retry also fails, the rows stay untrusted for ever. That is accepted
  as fail-closed. The health object makes it visible.
- **Writer interface change and the registration trigger** touch many tests. `ingested_at_ms=AT` appears in
  dozens, and about 25 files seed rows directly. The seeding helper in RB-1 and the interface change in RB-2 carry
  this. It is the largest cost of the implementation.
- **Storage growth** of about 2 to 5 GB a year at 260 to 600 symbols (D-2). It is a stated cost, not measured.
- **The registration trigger** makes an unknown writer fail hard. This is intended. A tool such as
  `tools/bench_open_interest_write.py`, which inserts directly, must register a transaction. RB-1 updates it.

## 5b. Review of the documents (2026-11-14, independent, Opus, read-only): findings and how they were applied

`make check` before this review failed two contract-example tests. The examples were reverted to the current server
(§3 item 4). The review found no blocker. Applied:

1. Major: the stamp `max` was NULL for an empty table. It now uses `coalesce(…, 0) + 1` (ADR-0005 P-3, D-3).
2. Major: INV-137 said `funding_rate` is the last key. It now says "followed only by `readability`", and RB-3 names
   the test that changes.
3. The example `cutover_ms` lay above the example row: the values are lowered in the three documents.
4. Contract 4.1 item 5 over-promised: it now says "no path that does not register and confirm".
5. A failed confirmation is carried to the next committed transaction. The contract states it as "no later attempt
   of the same process succeeded".
6. The present tense in §0, §6.5.1 and §6.7.1 became "once implemented".
7. The `unconfirmed` keys are the stored table names. This is now stated in 6.2.4.
8. The plan was updated (this section, §3 items 1 and 4).
9. ADR-0005 Related names ADR-0002 A-1. The ADR-0003 status line order was fixed.
