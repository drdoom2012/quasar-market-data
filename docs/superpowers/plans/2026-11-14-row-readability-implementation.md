# Plan: row readability, implementation (ADR-0005 RB-1 to RB-4)

- **Date:** 2026-11-14
- **Branch:** `feat/row-readability`, stacked on `docs/readable-by-contract` (PR #31). One PR for RB-1 to RB-4 (the
  user's one-PR rule). Its base is `docs/readable-by-contract` until #31 is merged, then `main`.
- **Normative source:** ADR-0005 and contract §4.1, §6.2.4. This plan decides only what they leave to the
  implementation. Anything that differs from them is listed in §6 and is amended in the documents on this branch.
- **User instruction:** "dokonči implementaci" (2026-11-14).

## 1. Modules

### 1.1 `domain/readability.py` (new, pure)

- `READABLE_BOUND_MS: Final = 2_000`, the contract constant `B`.
- `readable_by(stamp_ms, confirmed, cutover_ms) -> tuple[int, bool]` implements the ADR-0005 P-4 table:
  - `stamp < cutover` gives `(cutover, False)`;
  - `stamp > cutover` gives `(stamp + B, not confirmed)`;
  - `stamp == cutover` raises `ValueError`, because it is impossible by construction; failing closed beats
    guessing.
- No clock and no I/O. The domain allowlist is unchanged.

### 1.2 `storage/migrations/004_readability.sql` and the runner

- The DDL of ADR-0005 P-2, verbatim, plus `no_update` and `no_delete` triggers on the three new tables.
- Six registration triggers `<table>_registered`:
  `BEFORE INSERT ... WHEN NOT EXISTS (SELECT 1 FROM write_transactions WHERE table_name = '<dataset>' AND stamp_ms = NEW.<col>) BEGIN SELECT RAISE(ABORT, 'row without a registered write transaction'); END;`
- `write_transactions_after_cutover`: the cut-over guard on `write_transactions`.
- `write_confirmations_registered`: a confirmation needs a registered transaction.
- `storage/migrate.py`:
  - `migrate(conn, *, applied_at_ms: int | Callable[[], int], migrations=...)`. A callable is called **after**
    `BEGIN IMMEDIATE` of each migration. An int keeps today's behaviour for the tests.
  - `_apply` binds `{"applied_at_ms": value}` to every statement whose text contains `:applied_at_ms`.
  - `collector/cli.py:_migrate` passes `clock.wall_ms`, the callable, not its value.
- `storage/schema.py`: `SCHEMA_VERSION = 4`.

### 1.3 `collector/readability.py` (new): the stamping shared by the three writers

`TransactionStamper(conn, table, *, wall_ms, monotonic_ns, logger=None)`. It reads `cutover_ms` once at construction,
after the writer's `verify_schema`. It is used only on the writer's own thread.

- **`register() -> Stamp`.** Called right after `BEGIN IMMEDIATE`.
  - Reads `t0 = monotonic_ns()`.
  - Sets `s = max(wall_ms(), cutover + 1, coalesce(max stamp of the table, 0) + 1)` and inserts it into
    `write_transactions`.
  - Warns when `s > wall + B`.
  - Returns `Stamp(stamp_ms, t0_ns)`.
- **`expired(stamp) -> bool`.** `monotonic − t0 > (B − M) ms`, with `M = COMMIT_MARGIN_MS = 500`.
- **`after_commit(stamp, describe)`.**
  - Measures `t1`.
  - If `t1 − t0 ≤ B`: tries the confirmation in its own transaction (`BEGIN IMMEDIATE`, `INSERT ... ON CONFLICT DO
    NOTHING`, `COMMIT`). On an `sqlite3.Error` it rolls back, keeps the stamp in `pending` and logs a warning.
  - Otherwise it logs an error with the table, the stamp, `describe()` (series list and time range) and the
    duration.
  - It never raises an `sqlite3.Error`. A failure of the rollback itself makes the writer unusable through a
    callback, as the existing writers do.
- **`flush_pending()`.** Called at the start of every `write` / `write_transaction`, before the data transaction.
  It tries every pending confirmation in one own transaction. Failures keep them pending. See §6 item 1.
- `MAX_STAMP_ATTEMPTS = 3`.

### 1.4 The three writers

`CandleWriter`, `OpenInterestWriter` and `FundingRateWriter`:

- **Construction:** they take `wall_ms` and `monotonic_ns` (default the system clocks; the candle writer gains
  both).
- **Signatures:** the `ingested_at_ms` parameter is **removed** from `write` / `write_transaction` and from the async
  fronts.
- **The loop of one call** (at most `MAX_STAMP_ATTEMPTS` attempts):
  1. `BEGIN IMMEDIATE`, then `register()` if `rows` is non-empty. A funding call with no rows and only a merge
     registers nothing, because no row is written.
  2. Every insert uses `stamp_ms`.
  3. **Empty check:** nothing inserted, no conflict newly recorded (`rowcount == 1`), and for funding the span
     unchanged. Then `ROLLBACK` and return the report. The candle writer gains `rowcount` on its conflict insert.
  4. **`expired()`:** `ROLLBACK`, reset the counters, next attempt. When the attempts are exhausted, raise
     `CandleWriteNotStored`, `OpenInterestWriteNotStored` or `FundingRateWriteNotStored`, each a subclass of the
     dataset's write error.
  5. `COMMIT`, then `after_commit()`.
- **Error classification** (`_failure`) is unchanged. An outcome-unknown `COMMIT` does not confirm.
- **Reports:** unchanged fields. Every report also gains `stamp_ms: int | None` (the committed stamp, `None` when
  nothing was committed), for logs and tests.

### 1.5 Callers

- The `ingested_at_ms=clock.wall_ms()` arguments are removed:
  - `fetch_once.py:117`;
  - `fetch_open_interest_once.py:131`;
  - `fetch_funding_rate_once.py:155`;
  - `open_interest_task.py:805`;
  - `funding_rate_execution.py`;
  - the `CandleSink` protocol and `collection_loop._write`;
  - the async writers (`async_*_writer.py`).
- `collection_loop.py:172` also catches `CandleWriteNotStored`, as a failed attempt of the symbol.
- Open interest and funding already retry a failed page for any `*WriteError` that is not fatal. The plan verifies
  this per call site and adds `*NotStored` wherever only `Busy` / `OutcomeUnknown` is caught.
- The async writers construct the sync writer with the clocks they are given (`AsyncX.open(..., wall_ms=...,
  monotonic_ns=...)`). The CLI passes the `SystemClock` methods, the tests pass fakes.

### 1.6 API

- `api/readability_sql.py` (new): constants for the join and the cut-over read:
  - `SELECT cutover_ms FROM readability_cutover WHERE id = 1`;
  - `LEFT JOIN write_confirmations w ON w.table_name = '<t>' AND w.stamp_ms = <alias>.ingested_at_ms`, selecting
    `<alias>.ingested_at_ms, w.stamp_ms IS NOT NULL`.
- **Row readers:** `candle_reader.py`, `open_interest_page.py` and `funding_rate_page.py` add the two columns to the
  row query and read the cut-over once. The row dataclasses gain `readable_by_ms: int` and
  `readable_by_unconfirmed: bool`, computed by `domain.readability.readable_by`.
- **`app.py` serialisers:** the two keys are appended after `conflicted`, the last keys of the row (contract §4.1
  illustration).
- **Health:** `health_reader` / `read_ingest_extensions` reads, in the same transaction:
  - the cut-over;
  - per table, a `count(*)` and a `max(stamp_ms)` over
    `write_transactions t WHERE t.table_name = ? AND t.stamp_ms > ? AND t.stamp_ms <= ? AND NOT EXISTS (confirmation)`.

  `_ingest_body` appends `readability` as the last key. `READABILITY_WINDOW_MS = 86_400_000` is a constant.
- No new statement per row. The cut-over read is one keyed statement per request.

### 1.7 Documents (RB-4)

- **README:** the schema-4 deployment, with the precondition (every Collector-side process of the earlier release
  stopped, no `fetch-*` running) and the restore notice to consumers.
- **Contract:**
  - the examples of §5.2, §6.5.4 and §6.7.4 gain the two fields, with the values the seeded test database produces;
  - the status lines say "implemented";
  - §6.2.2 says schema version 4.
- **Invariants:** INV-140 to INV-145 become ENFORCED with test names. INV-137 is updated.
- **ADR-0005:** the status becomes "implemented by RB-1 to RB-4". The confirmation retry wording is aligned (§6
  item 1).
- **Benchmarks:** `make bench-oi-write`, `make bench-health` and `make bench-health-oi` are run and their numbers
  recorded in ADR-0005 (Evidence). `tools/bench_open_interest_write.py` registers its transactions. A candle-writer
  timing is derived from the existing bench.

## 2. Tests (TDD: each test is written first and seen failing)

### 2.1 Storage (`tests/storage/test_schema_v4_readability.py`)

- For each of the six tables, an insert with an unregistered stamp aborts. A registered one passes. An identical
  duplicate with an unregistered stamp aborts; with a registered stamp it passes with `rowcount` 0.
- A registration at or below the cut-over aborts. A confirmation without a registration aborts.
- Update and delete abort on the three new tables.
- `cutover_ms` is `max(applied, every stored stamp) + 1`: for a database migrated to 3 with stamps above
  `applied_at_ms` (a clock behind the data), and for a fresh database.
- A callable `applied_at_ms` is called after `BEGIN IMMEDIATE`. A probe connection holds the lock while the clock
  call is checked against the lock state.
- Migrating twice is a no-op. A crash in the middle (an injected failure) leaves version 3 and the migration
  re-runs.
- The existing v1/v2/v3 schema tests, the checksum tests and `SCHEMA_VERSION` tests are updated to version 4.

### 2.2 Stamping (`tests/collector/test_readability_stamper.py` and the writer tests)

All with fake clocks:

- The stamp is the wall clock; `last + 1` on an equal millisecond; `cutover + 1` with a wall clock behind it;
  uniqueness with two writer connections on one file.
- A transaction expired before `COMMIT` is restamped. After 3 expirations `*NotStored` is raised and nothing is
  stored.
- `t1 − t0 ≤ B` gives a confirmation; `> B` gives none and the error log line.
- A failing confirmation (an injected `sqlite3.Error`) gives no exception from `write`, a pending entry, and a
  confirmation by the next write.
- An outcome-unknown `COMMIT` gives no confirmation.
- An empty transaction (only duplicates) is rolled back: no registration, no `stamp_ms`.
- A split open interest or funding page with an interleaved candle write gives one stamp per transaction, each
  confirmed.
- Candle loop: `CandleWriteNotStored` is a failed attempt, not fatal (`collection_loop`).

### 2.3 API (`tests/api/test_readability_rows.py`, health tests)

- The three rows of the P-4 table, for each of the three endpoints, from a database seeded at schema 3 and migrated
  to 4 (old rows), plus registered rows with and without a confirmation.
- Every other field is byte-identical to the response without the two keys.
- The statement count does not depend on the rows, and no statement scans: the existing tests are extended to the
  new SQL.
- `readability` health: the window edges (`as_of − window` exclusive, `as_of − B` inclusive), `null` when the count
  is 0, the key is last, every table listed with the datasets disabled, no status change.
- The contract examples (`test_the_contract_example_is_what_the_server_returns`) with the updated examples.

### 2.4 Test seeding

- `tests/api/seed.py`: `make_database` migrates to 3, seeds as today, then migrates to 4. Seeded rows are therefore
  old rows, with `readable_by_ms` equal to the cut-over and `false`.
- New helpers `register(conn, table, stamp)` and `confirm(conn, table, stamp)` for new-style rows.
- Other raw seeders follow the same rule: `tests/deploy/seed_db.py`, the `bench_*` files, `test_schema_v*`, and the
  collector fakes and tests that insert directly.
- **Writer tests:** the writer runs on a database migrated with a small `applied_at_ms`, and a fake wall clock above
  it. Assertions on the stored stamp use `report.stamp_ms` instead of the old `AT`.

## 3. Order of work

1. RB-1: domain function, migration, runner, schema version. Fix the seeding so that `make check` is green again.
2. RB-2: stamper and writers. Update the callers and fakes; then the writer, loop and task tests.
3. RB-3: API rows and health.
4. RB-4: documents, invariants, benchmarks.
5. `make check`, then an independent code review of the diff (Opus, read-only), apply every finding, `make check`,
   then the PR.

## 4. Commits

One commit per slice, with `make check` green before each. The pre-commit hook runs it.

## 5. Risks

- **Test churn:** about 60 files. Mitigation: the mechanical removal of the `ingested_at_ms=` arguments first, then
  the semantic fixes.
- **Contention:** one more lock for each confirmation. The benchmarks measure it. If the P-9 criterion (p99 ≤ 50 ms
  for a 100-row transaction) is no longer met, the result is reported, not hidden.

## 6. Deviations from the documents, to be amended on this branch

1. **The pending-confirmation retry.** ADR-0005 P-3 says the confirmation is inserted "inside its next committed
   data transaction". The plan uses its own transaction at the start of the next write call. This is simpler: a data
   transaction that is rolled back (empty or restamped) never carries it, and it is still fail-closed. The documents
   change to "retried in its own transaction at the start of each later write of that writer, until it succeeds or
   the process stops".
2. **`stamp_ms` in the reports.** It is internal and not in the contract. No document change is needed.

## 7. Plan review (2026-11-14, independent, Opus, read-only): findings and how they are applied

1. **Blocker:** a funding forward page with no new settlement only advances `verified_at_ms`. Under §1.4 step 3 it
   would have been rolled back as empty. **Applied:** a transaction that carries a `merge` is never empty and always
   commits. Test: a forward page with no new row advances `verified_at_ms`.
2. **Major:** the claim in §1.5 that open interest and funding "already retry" is false.
   `open_interest_task.py:771-779` and `funding_rate_task.py:600-609` turn every unknown write error into
   "writer unusable". `backfill.py:341` catches only `CandleWriteOutcomeUnknown`. **Applied:** `*NotStored` is
   handled like `*Busy` in both tasks, and as a failed hole in backfill, each with a test.
3. **Major:** contention against ADR-0005 P-6, and the §6 item 1 deviation would have changed accepted text without
   approval. **Applied, so that no ADR text changes:**
   - The pending confirmations are inserted **inside the writer's next committed data transaction**, as ADR-0005 P-3
     says. A rolled-back data transaction carries them on. There is no extra lock and no `flush_pending`.
   - The restamp attempts are a writer parameter: 3 for candles, **1** for open interest and funding. An expired
     extension transaction raises `*NotStored` at once, and the task retries the page through the async front, which
     yields to candles first. One extension job therefore holds the lock for one data transaction plus its
     confirmation (P-6).
   - §6 item 1 is withdrawn.
4. **Major:** the seeding missed inserts made after `make_database`. **Applied:**
   - The API seed migrates to schema 4 at once with `applied_at_ms = MIN_TIMESTAMP_MS`, so the cut-over is
     `MIN + 1`.
   - The `insert_*` helpers register their stamp (`ON CONFLICT DO NOTHING`). Seeded rows are therefore **new-style,
     unconfirmed** rows unless a test confirms them.
   - Old rows are seeded explicitly where a test needs them: a schema-3 database, rows, then migration 4.
   - `make_database_from_fixture` uses the real writer with fixed fake clocks, so its rows are confirmed new-style
     rows and the §5.2 example is deterministic.
5. **Major:** health has three ingest readers. **Applied:** `read_stored_series`, `read_ingest_snapshot` and
   `read_ingest_extensions` all read the readability facts, and `as_of_ms` is passed to them. All three
   `_ingest_body` call sites append `readability`.
6. **Minor:** **Applied:**
   - Every per-attempt variable is reset on a restamp.
   - A failed `ROLLBACK` of an expired or empty attempt makes the writer unusable and raises the base write error,
     never `NotStored`.
7. **Minor:** **Applied:** separate parameters `wall_ms` (required) and `stamp_monotonic_ns`, so the finite tick
   iterators of the existing tests are not consumed by the stamper.
8. **Minor:** **Applied:** the `OpenInterestSink` and funding sink protocols, and `funding_rate_execution.py:134`,
   are added to §1.5.
9. **Minor:** **Applied:** `bench-health` is a gate in RB-4. If the readability read makes the p99 of the existing
   health benchmark exceed its criterion, the change is reported and the window is reconsidered with the user.
10. **Minor:** **Applied:** `collector/readability.py` is added as a source to the extension-isolation contracts, so
    it stays a leaf.
11. **Minor:** the runner is fine. Test databases use an `applied_at_ms` within the valid timestamp range.
