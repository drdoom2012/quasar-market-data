# S7 Idempotent Candle Writer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Collector-side writer that persists validated candles into schema v1 atomically and idempotently: a new candle is inserted, an identical duplicate is a no-op, a differing duplicate is never written over and is reported, and a failed batch leaves nothing behind. An asyncio wrapper calls it through one dedicated thread with a defined lifecycle and defined behavior under cancellation.

**Architecture:** `collector/candle_writer.py` holds the synchronous `CandleWriter` that owns one writer connection (from S6) and runs each batch in one `BEGIN IMMEDIATE` transaction. `collector/async_candle_writer.py` wraps it behind a single-thread executor so asyncio code never touches SQLite directly (spec D6, no `aiosqlite`). The writer lives in `collector`, so the API cannot import it: the existing `collector and api are independent` contract enforces "the Collector is the only writer" at import level. Recording differing duplicates is S8; S7 only detects and reports them.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, `asyncio`, `concurrent.futures`; pytest, mypy strict, ruff, import-linter. No new dependencies (no `pytest-asyncio`: async tests call `asyncio.run`).

**Spec:** `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (S7 in section 8, D6, section 6.3). Approved decisions: `docs/adr/0002-sqlite-schema-v1.md` (P-1, P-2, P-4, P-5), `docs/adr/0001-binance-ingestion-fidelity-and-closed-candles.md` (P-6). Earlier plan: `docs/superpowers/plans/2026-10-22-s6-sqlite-schema-v1-and-migrations.md` (S6 interfaces).

## What the S7 writer does not guarantee: closed candles

`ValidatedCandle` proves that a candle passed the price, relation, volume and turnover rules (INV-021). It does **not** prove that the candle is closed. A validated candle that is still forming, or that has not yet passed `end + publication delay <= trusted server lower bound`, is accepted by the writer and persisted. Closed-only persistence (INV-007) is therefore **not** a property of S7 and must not be described as one in code, docstrings, README or the register.

Closed-only persistence is a property of the S9 pipeline. S9 must (a) pass the writer only `ClosedCandleSelection.eligible` from `select_closed_candles`, and (b) prove it end to end: a test that feeds the forming fixture (`btcusdt_5m_forming_raw.json`) and an untrusted-time case through the real selection and writer and shows that no pending candle reaches the table. INV-007 stays PLANNED until that test exists. The S5 domain contract is not changed here; whether a distinct eligible-candle type would be worth adding is a decision for S9.

## Global Constraints

- Branch `market-data-init`; one commit per task, message in each task; trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Do not push.
- No schema change, no new migration, no edit of `docs/adr/`, `pyproject.toml` (dependencies and contracts), `uv.lock`, or the domain purity allowlist. `domain` and `storage` are not modified. No new framework, dependency or architectural layer.
- The writer imports only the stdlib, `domain` and `storage.connection` / `storage.schema`. It must not import `storage.migrate` (migrating is the Collector's startup step in S9), `api`, `ccxt`, `fastapi` or `talib`.
- Never `UPDATE`, `DELETE`, `INSERT OR REPLACE` or `INSERT OR IGNORE` on any table. Candles are inserted with `INSERT … ON CONFLICT DO NOTHING`; a CHECK violation must raise, not vanish (S6 ruling, `test_insert_or_ignore_would_swallow_an_invalid_conflict_record`).
- No clock reads: the caller passes `ingested_at_ms`. All values are the candle's canonical decimal text; no `float` anywhere.
- Fail closed: a misconfigured connection, an unmigrated database, an invalid candle or any database error stops the batch and writes nothing from it; a connection whose transaction state cannot be restored is never used again.
- No code, docstring, README line or register row may present the writer as closed-only persistence (see the section above).
- Every guarantee introduced here has a targeted negative test, listed in the guarantee map at the end of this plan. Tests use `tmp_path` databases only. `tests/` is mypy-strict; deliberately wrong runtime types only through `tests/typing_helpers.py::untyped`. A PreToolUse hook runs `make check` on `git commit`; run `make check` before each commit and report real output.
- TDD: failing test first, record RED and GREEN output.

## Review Focus

Failure modes the spec and ADR imply that a reasonable user would expect handled, most likely first. Each has a test in the owning task.

1. The same batch delivered again, also after a process restart → no duplicate rows, nothing changed (Task 1).
2. A re-delivered candle whose values differ from the stored one → never overwritten, reported with both versions, the rest of the batch still written (Task 1).
3. A validated but not closed candle → the writer accepts it; this non-guarantee is pinned by a test and by the register (Tasks 1, 4).
4. A candle cast to `ValidatedCandle` that is not valid → the whole batch is rejected, nothing written (Task 2).
5. A failure halfway through a batch, in `COMMIT`, in `ROLLBACK`, or an interrupt → the batch is rolled back or the writer is declared unusable; never a half-applied batch and never a reused connection in an unknown state (Task 2).
6. Writes racing with `close()`; `close()` called twice; a caller cancelled while its write is queued or already running; `open()` or `close()` cancelled → defined outcomes, no leaked thread or connection, no lost accepted write (Task 3).

---

### Task 1: Synchronous writer core

**Files:**
- Create: `src/quasar_market_data/collector/candle_writer.py`, `tests/collector/test_candle_writer.py`

**Interfaces:**
- Consumes: `ValidatedCandle` (`domain/validation.py`), `Candle`, `CandleKey` (`domain/candle.py`), `ensure_timestamp_ms` (`domain/timestamps.py`), `StorageError` (`storage/connection.py`), `verify_schema` (`storage/schema.py`).
- Produces in `candle_writer.py`:
  - `class CandleWriteError(StorageError)`.
  - `@dataclass(frozen=True) CandleConflict(incoming: Candle, stored: Candle)`.
  - `@dataclass(frozen=True) WriteReport(inserted: int, duplicates: int, conflicts: tuple[CandleConflict, ...])`.
  - `class CandleWriter`: `__init__(self, conn: sqlite3.Connection) -> None`, `write(self, candles: Sequence[ValidatedCandle], *, ingested_at_ms: int) -> WriteReport`, `close(self) -> None`.

Semantics. `__init__` refuses (`CandleWriteError`, message names the failed precondition) a connection that is not autocommit (`isolation_level` is not `None`), **is already inside a transaction (`conn.in_transaction`)**, is read-only (`PRAGMA query_only` is `1`), has `recursive_triggers` off (S6 INV-025 depends on it), or is not in WAL mode; then calls `verify_schema(conn)` (its `SchemaMismatchError` propagates). `write` first validates arguments (`ingested_at_ms` via `ensure_timestamp_ms`; every item `type(item) is Candle`, else `TypeError`) before touching the database; an empty batch returns `WriteReport(0, 0, ())` without a transaction. Otherwise one `BEGIN IMMEDIATE` … `COMMIT` covers the batch. For each candle in input order: `INSERT INTO candles (…) VALUES (…) ON CONFLICT DO NOTHING`; if one row was inserted it counts as inserted; if not, the stored row for the same key is read: equal on `open, high, low, close, volume, turnover` counts as duplicate, anything else becomes a `CandleConflict(incoming, stored)` (stored rebuilt as a `Candle` from the row) and nothing is written for it. `ingested_at_ms` is stored on inserted rows only. A conflict does not stop the batch. Within one batch the second of two candles with the same key is compared against the first, which is already stored. Nothing is written to `candle_conflicts` in S7. `close()` closes the connection; a `write` after `close()` raises `CandleWriteError`. The module docstring states the non-guarantee about closed candles in the section above.

- [ ] **Step 1: Write failing tests** in `tests/collector/test_candle_writer.py`. Fixtures: `db(tmp_path)` path; `writer_conn(db)` = `open_writer(db)` migrated with `migrate(conn, applied_at_ms=AT)`; helper `valid(**fields)` = `require_valid(make_candle(key=make_key(open_time_ms=…), **fields))` using `tests.domain.test_candle` helpers; 5m candles at `OPEN_MS + n * 300_000`.
  - `test_new_candles_are_inserted_with_exact_text`: two candles (one with `turnover=None`) → `WriteReport(2, 0, ())`; the rows read back through a raw `SELECT` equal the candles' canonical text, `turnover` is `NULL` for the second, `ingested_at_ms` equals the argument.
  - `test_rewriting_the_same_batch_is_a_no_op`: second `write` of the same batch with a different `ingested_at_ms` → `WriteReport(0, 2, ())`; the table content, including the first `ingested_at_ms`, is unchanged.
  - `test_restart_does_not_duplicate`: write, close the connection, open a new writer connection and `CandleWriter` on the same file, write the same batch → all duplicates, row count unchanged.
  - `test_differing_duplicate_is_reported_and_never_overwritten`, parametrized over `open, high, low, close, volume, turnover` (ids = field names; for `turnover` use `None` versus a value in both directions): the stored row is unchanged, `conflicts == (CandleConflict(incoming, stored),)`, `inserted == 0`.
  - `test_conflict_does_not_stop_the_rest_of_the_batch`: `[new, conflicting, new2]` → `inserted == 2`, one conflict, both new rows stored.
  - `test_duplicate_inside_one_batch`: `[c, c]` → `(1, 1, ())`; `[c, c_changed]` → one insert and one conflict whose `stored` is `c`.
  - `test_conflicts_record_nothing_yet`: after a conflict, `candle_conflicts` has 0 rows (the boundary with S8, documented in a comment).
  - `test_writer_does_not_check_eligibility` (documents the non-guarantee, named so nobody mistakes it for a guard): a validated candle whose end lies far in the future of any plausible server time (for example `open_time_ms` in 2099, aligned) is inserted. The test comment points to the section "What the S7 writer does not guarantee" and to S9.
  - `test_empty_batch_is_a_no_op`: `WriteReport(0, 0, ())` and `conn.in_transaction` is false.
  - `test_writer_refuses_a_misconfigured_connection`, parametrized with ids: a reader from `open_reader` (`reader`), `recursive_triggers=OFF` (`recursive_triggers_off`), a plain `sqlite3.connect` (`not_autocommit`), a connection with an open `BEGIN` (`already_in_transaction`) → `CandleWriteError`; an unmigrated `open_writer` database → `SchemaMismatchError` (`unmigrated`). For `already_in_transaction` the transaction is still open and untouched afterwards.
  - `test_argument_validation`: `untyped(True)` / `untyped(1.5)` for `ingested_at_ms` → `TypeError`, `0` → `ValueError`; a batch `[good, untyped("x")]` → `TypeError` with nothing written.
  - `test_write_after_close_raises`.
  - `_typing_contract(writer: CandleWriter, plain: Candle) -> None` (never called): `writer.write([plain], ingested_at_ms=AT)` with the `# type: ignore[<code mypy reports>]` comment. `make typecheck` fails if the ignore becomes unused, which is the type-level proof that a plain `Candle` is not accepted (this proves "validated", not "closed").
- [ ] **Step 2: Run** `uv run pytest tests/collector/test_candle_writer.py -v`. Expect: FAIL (module missing).
- [ ] **Step 3: Implement** `CandleWriter` per the semantics above in `candle_writer.py`; row to `Candle` through the `Candle` / `CandleKey` constructors.
- [ ] **Step 4: Mutation check (evidence, not committed):** temporarily (a) treat every existing row as a duplicate without comparing, (b) replace `ON CONFLICT DO NOTHING` with `OR REPLACE`, and (c) remove the `in_transaction` precondition; show that `test_differing_duplicate_is_reported_and_never_overwritten` fails under (a) and (b) and `test_writer_refuses_a_misconfigured_connection[already_in_transaction]` under (c); restore and record the outputs.
- [ ] **Step 5: Run** the tests, then `make check`. Expect: pass; record output.
- [ ] **Step 6: Commit** `feat(collector): idempotent candle writer core`.

### Task 2: Failure semantics

**Files:**
- Create: `tests/collector/faulty_connection.py`
- Modify: `src/quasar_market_data/collector/candle_writer.py`, `tests/collector/test_candle_writer.py`

**Interfaces:**
- Consumes: `CandleWriter`, `CandleWriteError` (Task 1), `validate_candle` (`domain/validation.py`).
- Produces in `faulty_connection.py` (test helper): `class FaultyConnection(sqlite3.Connection)` created with `sqlite3.connect(path, factory=FaultyConnection, isolation_level=None)`; it overrides `execute` so that a configured statement (matched by exact text or prefix, optionally only the n-th time) raises a configured exception (`sqlite3.OperationalError` or `KeyboardInterrupt`); `open_faulty_writer(path, ...)` applies the same pragmas as `open_writer` (WAL, `synchronous=FULL`, `recursive_triggers=ON`) and migrates. Verified 2026-10-22: with such a subclass a failing `COMMIT` raises, the connection stays `in_transaction`, and a later `ROLLBACK` clears it.
- Produces in the writer, no new public names. `write` additionally:
  (a) re-validates every candle with `validate_candle` before the transaction and raises `CandleWriteError` naming the offending index and violations if any candle is invalid, writing nothing;
  (b) wraps any `sqlite3.Error`, **including one raised by `COMMIT`**, in `CandleWriteError` (chained), then issues `ROLLBACK` if `conn.in_transaction`;
  (c) after the rollback, if the rollback itself raised or `conn.in_transaction` is still true, marks the writer **unusable**: the original failure is raised as `CandleWriteError`, and every later `write` raises `CandleWriteError("writer is unusable after a failed rollback; close and reopen")` without touching the database;
  (d) on `BaseException` that is not a `sqlite3.Error` (for example `KeyboardInterrupt`) attempts the same rollback, then re-raises the original exception unchanged; if the rollback fails the writer is marked unusable as in (c).

- [ ] **Step 1: Write failing tests** (added to `tests/collector/test_candle_writer.py`):
  - `test_candle_cast_to_validated_but_invalid_is_rejected_whole_batch`: `cast(ValidatedCandle, make_candle(high="98", low="99"))` in `[good, bad]` → `CandleWriteError`; the table is empty; `conn.in_transaction` is false.
  - `test_failure_midway_rolls_back_the_whole_batch`: on the writer connection `CREATE TEMP TRIGGER boom BEFORE INSERT ON main.candles WHEN NEW.open_time_ms = <second candle's time> BEGIN SELECT RAISE(ABORT, 'boom'); END`; a batch of three → `CandleWriteError` whose `__cause__` is a `sqlite3.Error`; zero rows stored; not in a transaction. After `DROP TRIGGER boom`, writing the same batch stores all three (retry is safe).
  - `test_commit_failure_rolls_back_and_the_batch_can_be_retried`: `FaultyConnection` fails `COMMIT` once → `CandleWriteError` (chained `sqlite3.OperationalError`); zero rows stored; `conn.in_transaction` is false; the same batch written again stores all rows.
  - `test_failed_rollback_makes_the_writer_unusable`: `FaultyConnection` fails `COMMIT` and `ROLLBACK` → `CandleWriteError`; a following `write` raises `CandleWriteError` mentioning "unusable" and executes no statement (the faulty connection records statements; none after the failure); `close()` still works; a raw second connection sees zero rows.
  - `test_interrupt_midway_rolls_back_and_propagates_unchanged`: `FaultyConnection` raises `KeyboardInterrupt` on the second `INSERT` → the same `KeyboardInterrupt` object reaches the caller (not wrapped); zero rows; `conn.in_transaction` is false; a following good batch is written.
  - `test_busy_database_raises_and_writes_nothing`: a second raw connection holds `BEGIN IMMEDIATE`; a writer opened with `busy_timeout_ms=0` raises `CandleWriteError`; after the other connection rolls back, the same batch is written completely.
  - `test_writer_is_usable_after_a_failed_batch`: after the statement failure, the busy failure and the commit failure above, a good batch is written.
- [ ] **Step 2: Run** `uv run pytest tests/collector/test_candle_writer.py -v`. Expect: the new tests FAIL, the Task 1 tests still pass.
- [ ] **Step 3: Implement** behaviors (a) to (d) in `write` and the test helper.
- [ ] **Step 4: Mutation check (evidence, not committed):** (a) drop the re-validation, (b) drop the rollback after a failed `COMMIT`, (c) drop the "unusable" marking, (d) wrap `KeyboardInterrupt` in `CandleWriteError`; show that respectively `test_candle_cast_to_validated_but_invalid_is_rejected_whole_batch`, `test_commit_failure_rolls_back_and_the_batch_can_be_retried`, `test_failed_rollback_makes_the_writer_unusable` and `test_interrupt_midway_rolls_back_and_propagates_unchanged` fail; restore; record the four outputs.
- [ ] **Step 5: Run** the tests, then `make check`. Expect: pass; record output.
- [ ] **Step 6: Commit** `feat(collector): atomic batches and fail-closed failure handling in the candle writer`.

### Task 3: Asyncio wrapper: lifecycle and cancellation

**Files:**
- Create: `src/quasar_market_data/collector/async_candle_writer.py`, `tests/collector/test_async_candle_writer.py`

**Interfaces:**
- Consumes: `CandleWriter`, `WriteReport`, `CandleWriteError` (Tasks 1–2), `open_writer`, `DEFAULT_BUSY_TIMEOUT_MS` (`storage/connection.py`).
- Produces in `async_candle_writer.py`: `class AsyncCandleWriter` with
  - `@classmethod async def open(cls, path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> AsyncCandleWriter`
  - `async def write(self, candles: Sequence[ValidatedCandle], *, ingested_at_ms: int) -> WriteReport`
  - `async def close(self) -> None`

Design. One `ThreadPoolExecutor(max_workers=1)` is the only place SQLite is touched; the connection and the `CandleWriter` are created inside the worker thread (connections are thread-bound) and every job, including the close job, is submitted to that same executor, so jobs run strictly in submission order.

**Lifecycle.** States `OPEN → CLOSING → CLOSED`, one way. `open()` returns an `OPEN` writer; if opening fails (including `SchemaMismatchError`) the executor is shut down before the error propagates.
- `write()` checks the state and submits its job to the executor with no `await` between the two, so the check and the acceptance are atomic with respect to `close()`. In `CLOSING` or `CLOSED` it raises `CandleWriteError` ("closing" / "closed") and submits nothing. A submitted job is an **accepted write**.
- `close()`: in `OPEN` it sets `CLOSING` and starts one shared close task (shielded from the cancellation of whoever called `close()`): the task submits the close job (which calls `CandleWriter.close()`), awaits it, shuts the executor down off the event loop, and sets `CLOSED`. Because the executor is FIFO, **every write accepted before `close()` runs to completion (commit or error) before the connection is closed**; its caller receives the real result unless that caller was cancelled. In `CLOSING`, `close()` awaits the same task; in `CLOSED` it returns at once. If closing the connection raises, the writer still ends `CLOSED` with the executor shut down, every concurrent closer receives `CandleWriteError` (chained), and later `close()` calls return without raising.
- A writer that became unusable inside the worker (Task 2) keeps raising `CandleWriteError` from `write()`; `close()` still works.

**Cancellation.** A caller's `asyncio.CancelledError` never reports success and never leaves the worker mid-transaction, because the worker finishes or rolls back its transaction before it takes another job.
- Cancelled while the job is queued and has not started: the pending executor job is withdrawn when the event loop processes the cancellation, so the write is not performed. This is best effort: if the worker reaches the job first, it runs. A caller that was cancelled therefore cannot know whether its batch was applied; re-sending it is safe because writes are idempotent (INV-029).
- Cancelled after the job started: the transaction completes (commit or rollback) on the worker thread, the result is discarded, the caller sees `CancelledError`, and the writer stays `OPEN` and usable. `close()` waits for that job like for any other accepted write.
- `close()` cancelled: closing continues (shielded); a later `close()` awaits the same task.
- `open()` cancelled while the worker is connecting: the connection that finishes opening is closed and the executor shut down; nothing leaks.

- [ ] **Step 1: Write failing tests** (plain `def` tests calling `asyncio.run`; a `Gate` helper in the test file wraps `CandleWriter.write` via `monkeypatch` so a test can hold a job inside the worker (`started` and `release` `threading.Event`s) and then call the real method):
  - `test_all_writes_run_on_one_dedicated_thread_off_the_event_loop`: the wrapper records `threading.get_ident()`; several awaited writes record one identifier, different from the event loop's.
  - `test_concurrent_writes_are_serialized_and_none_is_lost`: `asyncio.gather` of 20 writes with distinct candles plus 5 repeats of one of them → no exception; each distinct candle is stored once; summed `inserted` equals the distinct count and summed `duplicates` the repeats.
  - `test_open_fails_closed_on_an_unmigrated_database`: `SchemaMismatchError`; no worker thread of the writer remains (checked through `threading.enumerate()` names).
  - `test_event_loop_stays_responsive_while_a_write_waits`: another raw connection holds the write lock for about 0.2 s (released from a `threading.Timer`); the writer has `busy_timeout_ms=1000`; a concurrent coroutine counts ticks with `await asyncio.sleep(0.01)` until the write finishes; the write succeeds and the tick count is at least 5.
  - Lifecycle: `test_write_after_close_has_started_is_rejected` (a write held in the worker, `close()` started as a task, `state` is `CLOSING`: a new `write` raises `CandleWriteError` and stores nothing; after release the held write returns its report and `close()` completes); `test_close_waits_for_every_accepted_write` (three writes accepted behind a held one, then `close()` as a task: it is not done until release, then all three reports are returned and all rows are stored); `test_write_close_race_never_loses_an_accepted_write` (tasks created in the order write, close, write: the first returns a report, the second raises `CandleWriteError`, and the stored rows equal the first report); `test_concurrent_close_calls_close_the_connection_once` (two closers; the wrapped `CandleWriter.close` is called once); `test_close_is_idempotent_after_closed`; `test_close_failure_is_reported_and_the_writer_still_ends_closed` (patched `CandleWriter.close` raises: every concurrent closer gets `CandleWriteError`, a later `close()` returns, the executor thread is gone); `test_write_after_closed_raises`.
  - Cancellation: `test_cancelled_started_write_still_completes` (the job is held inside the worker, the caller is cancelled and sees `CancelledError`, the job is released; after `close()` a raw connection sees the committed rows; a rewrite on a fresh writer reports them as duplicates); `test_cancelled_queued_write_is_withdrawn` (one job held, a second queued and its caller cancelled, then `await asyncio.sleep(0)` so the loop processes the cancellation, then release: the second batch is not stored; the comment states the best-effort race); `test_writer_stays_usable_after_a_cancelled_write`; `test_cancelled_close_still_closes` (the closer task is cancelled while a write is held; after release, a second `close()` returns and a write raises "closed"); `test_cancelled_open_leaves_nothing_behind` (the opening of the connection in the worker is held through a patched `open_writer`, `open()` is cancelled, the hold is released: the connection the worker produced is closed — using it raises `sqlite3.ProgrammingError` — and no worker thread remains).
- [ ] **Step 2: Run** `uv run pytest tests/collector/test_async_candle_writer.py -v`. Expect: FAIL (module missing).
- [ ] **Step 3: Implement** `AsyncCandleWriter` per the design.
- [ ] **Step 4: Mutation check (evidence, not committed):** (a) run the close job on a separate executor so it can overtake queued writes, (b) skip the state check in `write`, (c) replace the shielded close task with an unshielded await; show that respectively `test_close_waits_for_every_accepted_write`, `test_write_after_close_has_started_is_rejected` and `test_cancelled_close_still_closes` fail; restore; record the outputs.
- [ ] **Step 5: Run** the tests, then `make check` (also confirms `make imports`: the new modules import nothing forbidden). Expect: pass; record output.
- [ ] **Step 6: Commit** `feat(collector): asyncio candle writer with a defined lifecycle and cancellation behavior`.

### Task 4: Register and README

**Files:**
- Modify: `docs/invariants.md`, `README.md`

- [ ] **Step 1: Update the register.** Every ENFORCED row cites a test that fails when the invariant is violated; positive tests, controls and documenting tests are not evidence. Test names are fixed by Tasks 1–3.
  - `INV-007` stays **PLANNED**. Reword it to state explicitly that the S7 writer persists validated candles and does **not** check closure: `ValidatedCandle` proves validity only (`test_writer_does_not_check_eligibility` documents this), and the target is S9, which must pass only `ClosedCandleSelection.eligible` to the writer and prove it end to end with the forming fixture and an untrusted-time case.
  - `INV-008` (PLANNED → ENFORCED): the writer accepts only validated candles and an invalid candle is not persisted. Evidence: `tests/collector/test_candle_writer.py::test_candle_cast_to_validated_but_invalid_is_rejected_whole_batch`. The row names the type-level guard (`_typing_contract`, checked by `make typecheck`, not by pytest) and states that this is about validity, not closure (INV-007).
  - `INV-029` (ENFORCED): re-delivering a batch is a no-op and a differing duplicate never overwrites a stored candle through `CandleWriter`. Evidence: `tests/collector/test_candle_writer.py::test_rewriting_the_same_batch_is_a_no_op`, `tests/collector/test_candle_writer.py::test_differing_duplicate_is_reported_and_never_overwritten`. Scope: detection and reporting only; recording the conflict is S8.
  - `INV-030` (ENFORCED): a failed batch writes nothing and the same batch can be retried; a connection whose rollback failed is never reused. Evidence: `tests/collector/test_candle_writer.py::test_failure_midway_rolls_back_the_whole_batch`, `tests/collector/test_candle_writer.py::test_commit_failure_rolls_back_and_the_batch_can_be_retried`, `tests/collector/test_candle_writer.py::test_failed_rollback_makes_the_writer_unusable`, `tests/collector/test_candle_writer.py::test_interrupt_midway_rolls_back_and_propagates_unchanged`. Scope: commit and rollback failures are injected, not produced by a real disk fault.
  - `INV-031` (ENFORCED): `CandleWriter` refuses a connection that cannot give the guarantees (reader, `recursive_triggers` off, not autocommit, already in a transaction, unmigrated). Evidence: `tests/collector/test_candle_writer.py::test_writer_refuses_a_misconfigured_connection[already_in_transaction]` and `[recursive_triggers_off]`.
  - `INV-032` (ENFORCED): `AsyncCandleWriter` completes every accepted write before it closes the connection and accepts none after `close()` starts. Evidence: `tests/collector/test_async_candle_writer.py::test_close_waits_for_every_accepted_write`, `tests/collector/test_async_candle_writer.py::test_write_after_close_has_started_is_rejected`.
  - `INV-033` (ENFORCED, scope stated in the row): cancelling a caller never leaves the writer mid-transaction; a write already running is completed on the worker thread. Evidence: `tests/collector/test_async_candle_writer.py::test_cancelled_started_write_still_completes`, `tests/collector/test_async_candle_writer.py::test_cancelled_close_still_closes`, `tests/collector/test_async_candle_writer.py::test_cancelled_open_leaves_nothing_behind`. The row says that withdrawing a queued write is best effort and that a cancelled caller cannot know whether its batch was applied.
  - Reword, keeping the status: `INV-009` (a re-delivered differing candle is detected and not written; recording is S8), `INV-012` (writer idempotent per INV-029 and INV-030; checkpoints and restart reconcile are S15), `INV-014` (the writer lives in `collector`, which `api` cannot import). Do not change any other row.
- [ ] **Step 2: Update `README.md`:** one line each for `CandleWriter` and `AsyncCandleWriter` in the module description, stating that they persist validated candles and that selecting only closed candles is the caller's job (S9); no new commands.
- [ ] **Step 3: Run** `uv run pytest tests/test_invariant_register.py -v`, then `make check`. Expect: pass; record output.
- [ ] **Step 4: Commit** `docs: record S7 invariants and the writer`.

**S7 Definition of Done:** `make check` ran green; every row of the guarantee map has its negative test and its mutation output in the report; no `float` or `sqlite3` import in `domain`; the writer never issues `UPDATE`, `DELETE`, `OR REPLACE` or `OR IGNORE`; the register and README state that the writer does not guarantee closed-only persistence and name S9 as the owner.

## Guarantee map

| Guarantee | Targeted negative test | Mutation that must break it (reported) |
|---|---|---|
| Identical re-delivery is a no-op | `test_rewriting_the_same_batch_is_a_no_op`, `test_restart_does_not_duplicate` | Task 1 (b) |
| A differing duplicate is never overwritten and is reported | `test_differing_duplicate_is_reported_and_never_overwritten` | Task 1 (a), (b) |
| Misconfigured connection refused, including one already in a transaction | `test_writer_refuses_a_misconfigured_connection[...]` | Task 1 (c) |
| Bad arguments change nothing | `test_argument_validation` | n/a (validation precedes the database; asserts nothing written) |
| Non-guarantee: eligibility is not checked | `test_writer_does_not_check_eligibility` (documenting) and the register wording | n/a (a documenting test, not a guard) |
| Invalid candle, even cast to `ValidatedCandle`, is rejected with the whole batch | `test_candle_cast_to_validated_but_invalid_is_rejected_whole_batch` | Task 2 (a) |
| Statement failure rolls the batch back | `test_failure_midway_rolls_back_the_whole_batch` | Task 2 (b) |
| `COMMIT` failure rolls the batch back and retry is safe | `test_commit_failure_rolls_back_and_the_batch_can_be_retried` | Task 2 (b) |
| A failed rollback makes the writer unusable | `test_failed_rollback_makes_the_writer_unusable` | Task 2 (c) |
| An interrupt rolls back and propagates unchanged | `test_interrupt_midway_rolls_back_and_propagates_unchanged` | Task 2 (d) |
| Busy database raises and writes nothing | `test_busy_database_raises_and_writes_nothing` | n/a (asserts no rows and a raised error) |
| One dedicated worker thread, none on the event loop | `test_all_writes_run_on_one_dedicated_thread_off_the_event_loop` | n/a |
| Concurrent writes are serialized and none is lost | `test_concurrent_writes_are_serialized_and_none_is_lost` | n/a |
| The event loop stays responsive | `test_event_loop_stays_responsive_while_a_write_waits` | n/a |
| Accepted writes finish before the connection closes | `test_close_waits_for_every_accepted_write`, `test_write_close_race_never_loses_an_accepted_write` | Task 3 (a) |
| No write is accepted once closing has started | `test_write_after_close_has_started_is_rejected`, `test_write_after_closed_raises` | Task 3 (b) |
| `close()` is idempotent and closes once; failure is reported and still ends `CLOSED` | `test_concurrent_close_calls_close_the_connection_once`, `test_close_is_idempotent_after_closed`, `test_close_failure_is_reported_and_the_writer_still_ends_closed` | n/a |
| A started write completes after its caller is cancelled | `test_cancelled_started_write_still_completes` | n/a (the stored rows after cancellation are the assertion) |
| A queued write is withdrawn when cancelled (best effort) | `test_cancelled_queued_write_is_withdrawn` | n/a |
| The writer is usable after a cancelled write | `test_writer_stays_usable_after_a_cancelled_write` | n/a |
| A cancelled `close()` still closes | `test_cancelled_close_still_closes` | Task 3 (c) |
| A cancelled or failed `open()` leaks no thread or connection | `test_cancelled_open_leaves_nothing_behind`, `test_open_fails_closed_on_an_unmigrated_database` | n/a |

---

## Self-review notes

- **Spec coverage:** S7 "idempotent writer, identical duplicate is a no-op" → Task 1. Spec D6 (single writer, called from asyncio through a thread, no `aiosqlite`; "the thread-based writer is validated in S7") → Task 3. Architecture 4.1 "transactional and idempotent writes" → Tasks 1–2. INV-008 (S7 enforces it) → Task 2 plus the typing guard. INV-007 is explicitly not delivered by S7.
- **Type consistency:** `ValidatedCandle`, `Candle`, `CandleKey`, `require_valid`, `validate_candle` are the S3 and S4 names; `StorageError`, `open_writer`, `open_reader`, `verify_schema`, `migrate` are the S6 names; `WriteReport`, `CandleConflict`, `CandleWriteError` and `CandleWriter.close` (Task 1) are consumed unchanged in Tasks 2–3.
- **Facts checked while revising (2026-10-22, scratch scripts, not in the repository):** a `sqlite3.Connection` subclass passed as `factory=` can raise on a chosen statement, a failing `COMMIT` leaves `in_transaction` true and a later `ROLLBACK` clears it; a second `BEGIN` inside a transaction raises; a temporary trigger on `main.candles` aborts an insert midway; with `ThreadPoolExecutor(max_workers=1)` a cancelled running job continues, and a cancelled queued job is withdrawn only after the event loop has processed the cancellation (cancelling and releasing the worker without yielding to the loop let the queued job run), which is why the queued-cancel guarantee is best effort and its test yields to the loop.
- **Not in scope:** recording conflicts (S8), closed-candle eligibility wiring and `fetch-once` (S9), unavailable-candle writes (S16), checkpoints (S15), startup migration, health, the API read path, any CLI, a distinct eligible-candle type.
- **Judgement calls (open to review):** the writer lives in `collector` (not `storage`) so the existing independence contract keeps the API from importing it; a differing duplicate is reported, not raised, and does not abort the batch; the batch is one transaction and a conflict is not a failure; re-validation at the writer on top of the `ValidatedCandle` type; `ingested_at_ms` is passed in; queued writes of a cancelled caller are withdrawn best effort rather than shielded, because the caller has abandoned the result and re-sending is idempotent; a connection whose rollback failed is never reused.
