# OI-2 Open Interest Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Schema version 2 with the open interest tables, and an open interest writer that is idempotent, records conflicts, runs on its own connection and thread, writes at most 100 rows per transaction and yields to candle writes; plus the measurement of the 100-row transaction (issue #2).

**Architecture:** One additive migration `002_open_interest.sql` exactly as ADR-0003 P-6. A synchronous `OpenInterestWriter` (one transaction of ≤ 100 rows per call), mirroring `CandleWriter` without touching it. An asyncio front `AsyncOpenInterestWriter` on its **own** single-thread executor and its **own** writer connection (`busy_timeout` 1 000 ms), which splits a page into ≤ 100-row transactions and, before each `BEGIN`, waits while a `CandleWriteActivity` reports a candle write in flight (P-9 rules 1–3). Nothing calls the open interest writer yet: entering the activity around candle writes and running the open interest task is OI-4. A standard-library benchmark measures the 100-row transaction on any disk, including the VPS (P-9 acceptance criterion).

**Tech Stack:** Python 3.12, stdlib `sqlite3`, asyncio, pytest, mypy strict, ruff, import-linter.

**Spec:** `docs/adr/0003-open-interest.md` (P-6, P-8, P-9, P-14, P-15 rows marked OI-2, P-16). Related: `docs/adr/0002-sqlite-schema-v1.md` (P-1, P-4, P-5, P-8), OI-1 (`domain/open_interest.py`). Patterns: `collector/candle_writer.py`, `collector/async_candle_writer.py`, `tests/collector/faulty_connection.py`, `tests/storage/test_schema_v1.py`, `tests/collector/test_reconcile_bench_smoke.py`.

## Global Constraints

- Branch `oi-2-open-interest-storage` from `main`; one commit per task, message given in each task, trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. The PR is squash-merged: its title is a conventional commit subject and its body reads as the squash commit message.
- **Candle behaviour and candle code do not change.** `001_schema_v1.sql`, the candle tables, `candle_writer.py`, `async_candle_writer.py`, `collection_loop.py`, the candle connection settings (`busy_timeout` 5 000 ms) and the stdout of `fetch-once` and `run` stay exactly as they are. The only new Collector output is one **stderr** line in `run` (Task 1).
- `002_open_interest.sql` is the SQL of ADR-0003 P-6 with `<decimal-shape>` replaced by the exact value-column `CHECK` of `001_schema_v1.sql` (same `typeof`, `length BETWEEN 1 AND 97` and six `NOT GLOB` terms). Additive; once released it is never edited.
- No API endpoint, response field, config key or new contract section (OI-4, OI-5). The API sees only `SCHEMA_VERSION = 2`.
- `storage` stays a leaf; the open interest writer lives in `collector` and imports `domain` and `storage` only.
- Fail closed; immutable storage; a differing re-delivered row is recorded, never written over; nothing fabricated.
- TDD: failing test first; record RED and GREEN output. A PreToolUse hook runs `make check` on `git commit`; run it before each commit and report the real output. Tests use `tmp_path` only; the benchmark never touches the production database or its volume.

## Decisions this plan makes (not fixed by the ADR)

- **The open interest writer copies, not shares, the candle writer's machinery.** `require_writer_connection`, `_failure` / `_roll_back` and the `AsyncCandleWriter` lifecycle are copied in the same shape, so `candle_writer.py` is not edited at all. `WriterState` is reused (public enum). Each copy's docstring names its original so they change together.
- **An unusable open interest writer closes its connection at once** (on its worker thread), so a connection whose rollback failed cannot keep the write lock and stall candle writes (P-9). The candle writer's tolerance of that state is not copied.
- **The yield check is `CandleWriteActivity`**, an asyncio counter with a sync context manager `writing()` (OI-4 wraps `_WriteWatch.write` with it) and `async wait_idle()`. The writer loops `while activity.active: await activity.wait_idle()` immediately before submitting each transaction, because an `asyncio.Event` waiter stays released after a re-entry.
- **At most one open interest transaction in flight, also after cancellation:** the async writer keeps the future of the last submitted transaction and awaits it (shielded) before its next activity check, so a write after a cancelled one never queues a second transaction behind a running one.
- **Errors carry what OI-4 needs:** `OpenInterestPageError` has `.cause` (the transaction's `OpenInterestWriteError`), `.committed_transactions` (confirmed commits only; an outcome-unknown transaction is not counted) and `.partial` (the report of the committed transactions). A busy database is `OpenInterestWriteBusy(OpenInterestWriteError)`, detected from `sqlite_errorcode`, so OI-4 can count `SQLITE_BUSY` (P-9 rule 5).
- **Conflicts report whether they are new** (`newly_recorded`), for P-8's "one log line per newly recorded conflict". After an outcome-unknown `COMMIT`, a retry reports `newly_recorded=False` for a conflict the unknown commit stored; the per-round count still shows it (documented in the writer).
- **Durations:** `lock_wait_ns` (from `BEGIN IMMEDIATE` start to lock acquired) and `duration_ns` (lock acquired to end of `COMMIT`) are reported separately; the second is what a candle write can wait for. Both include checkpoint work SQLite runs inside `COMMIT`.
- **Collector line:** `run` prints `migrated to version 2 (from 1)` (ADR P-14 wording) on stderr when it applied migrations, `from 0` for a fresh file; nothing when none were applied. `fetch-once` prints nothing new. `_migrate` reads the applied count before calling `migrate` with a new read-only helper `applied_version`.
- **`docs/contracts/api-v1.md` §6.2.2 and `docs/reference.md`:** the example `"schema_version":1` becomes `"schema_version":<schema version of this build>` with the note "(2 since OI-2)"; the field and its meaning are unchanged. **Approved by the user on 2026-11-02** (placeholder wording, so later schema versions need no contract edit).
- **The measurement stays in OI-2 (as the ADR says) and does not need a release:** the benchmark imports only the standard library and reads `002_open_interest.sql` from a path, so it runs on the VPS with `docker run python:3.12-slim` in a directory on the same filesystem as the database volume, **outside** the volume. Its SQL statements are asserted equal to the writer's by a test. The PR waits for the VPS number.

## Review Focus

1. Upgrading a populated v1 database: every candle, conflict and unavailable row survives byte-identically; a second start applies nothing (Task 1).
2. `INSERT OR REPLACE` and `ON CONFLICT DO UPDATE` on both new tables abort on a writer connection; the control without `recursive_triggers` shows why the pragma matters (Task 1).
3. A page whose 3rd transaction fails **mid-transaction**: 200 rows stored, nothing of the 3rd, the error says 2, and a retry stores the rest with 200 duplicates and no conflict (Task 4).
4. No open interest transaction begins while `CandleWriteActivity` is active, including when a candle write exits and a new one enters in the same loop iteration, and after a cancelled write (Task 4).
5. A held write lock beyond the 1 000 ms busy timeout fails only the open interest transaction as `OpenInterestWriteBusy`; nothing is written; a failed rollback releases the lock by closing the connection (Tasks 2, 4).

---

### Task 1: Migration 002 and schema version 2

**Files:**
- Create: `src/quasar_market_data/storage/migrations/002_open_interest.sql`, `tests/storage/test_schema_v2_open_interest.py`
- Modify: `storage/schema.py` (`SCHEMA_VERSION = 2`), `storage/migrate.py` (`applied_version`), `collector/cli.py` (`_migrate` returns `(old, new)`; `run` prints the stderr line), `tests/storage/conftest.py`, `tests/storage/test_migrate.py`, `tests/storage/test_verify_schema.py`, `tests/api/test_health_reader.py`, `tests/api/test_health_endpoints.py`, `tests/test_api_process.py`, `tests/collector/test_cli_run.py`, `docs/contracts/api-v1.md` (§6.2.2 example, approved placeholder wording), `docs/reference.md` (readiness example at line 43, schema reference names ADR-0003 P-6, the `run` stderr line)

**Interfaces:**
- `migrate.applied_version(conn: sqlite3.Connection) -> int`: `0` for a fresh file; the number of rows of `schema_migrations` for ours; `MigrationError` for a foreign file and for ours without `schema_migrations`. Read-only; does not compare with the shipped files (`migrate` does).
- `cli._migrate(db: Path, clock: Clock) -> tuple[int, int]` returns `(applied before, version after)`. `run` prints `migrated to version {new} (from {old})` to stderr when `new > old`; `fetch-once` ignores the result.
- `tests/storage/conftest.py`: `schema_sql(version: int) -> str` (shipped files `1..version` joined); fixture `schema_v2_conn`. `schema_v1_sql` and `schema_conn` stay.

- [ ] **Step 1: Write failing tests** in `tests/storage/test_schema_v2_open_interest.py` (on `schema_v2_conn`):
  - `test_schema_objects_are_exactly_the_approved_set`: the v1 set plus `open_interest`, `open_interest_conflicts`, `open_interest_conflicts_observation`, `open_interest_conflicts_slot` and the four `*_no_update` / `*_no_delete` triggers.
  - `test_open_interest_round_trips_exact_text`: `("binance-usdm", "BTC/USDT:USDT", "5m", 1793620800000, "95677.779", "7446123150.675", 1)` reads back unchanged.
  - `test_value_columns_accept_canonical_text`: `"0"`, `"100"`, `"0.5"` in both value columns of both tables.
  - `test_value_columns_reject_non_canonical_text`, parametrized over both value columns of both tables and the `BAD_DECIMALS` of `test_schema_v1.py` (exponent, negative, trailing zero, leading zero, trailing point, leading point, two points, space, empty), plus bound `1e20` (text `"1e+20"`) and `-1` → `IntegrityError`.
  - `test_bound_number_is_coerced_not_rejected`: a bound `5` is stored as `'5'` (control, mirrors `test_bound_float_is_coerced_not_rejected`).
  - `test_key_checks`: `time_ms` 946684799999 and 4102444800001, `period` `"5"` and `"5min"`, venue `""` → `IntegrityError`.
  - `test_open_interest_is_immutable`, parametrized with ids `update`, `delete`, `insert_or_replace`, `upsert_do_update` on `open_interest`, and `update`, `delete`, `insert_or_replace` (same observation, hits the unique index), `upsert_do_update` on `open_interest_conflicts` → `IntegrityError` matching `immutable`; the stored rows are unchanged.
  - `test_insert_or_replace_overwrites_without_recursive_triggers`: control on `open_interest` with `PRAGMA recursive_triggers=OFF`.
  - `test_conflict_observation_is_unique`: the same observation twice with `ON CONFLICT DO NOTHING` → one row; a different observation for the same slot → two.
- [ ] **Step 2: Write failing tests** in existing files:
  - `test_migrate.py`: `test_fresh_database_is_migrated_to_version_one` → `…_to_the_latest_version` (returns 2, two `schema_migrations` rows); `test_migrate_is_idempotent` expects `[(1, AT), (2, AT)]`; `test_migrated_database_matches_the_schema_v1_fixture` → `…_matches_the_shipped_schema_fixture` (inventory equals `schema_v2_conn`'s); new `test_upgrade_from_v1_keeps_every_v1_row` (runner with `migrations=(M1,)`, 3 candles, 1 conflict, 1 unavailable row, `sha256` of each v1 table's rows ordered by key, then the shipped migrations → 2, versions `[1, 2]`, hashes unchanged, a second `migrate` applies nothing); new `test_applied_version` (fresh 0, after M1 1, after all 2, foreign → `MigrationError`, ours without `schema_migrations` → `MigrationError`).
  - `test_verify_schema.py`: `test_v2_code_refuses_a_v1_database` → `SchemaMismatchError`.
  - `test_health_reader.py`: `test_ready_returns_the_schema_version` expects 2; `test_health_endpoints.py:125` and `test_api_process.py:159` expect `"schema_version": 2`; new `test_health_endpoints.py::test_v1_database_answers_503_schema_mismatch` (endpoint level).
  - `test_cli_run.py`: `test_first_start_prints_the_migration_on_stderr` (`migrated to version 2 (from 0)` in `err`), `test_upgrade_from_v1_prints_from_one`, `test_start_on_a_current_database_prints_no_migration_line`; the existing stdout assertions stay unchanged.
- [ ] **Step 3: Run** `uv run pytest tests/storage tests/api/test_health_reader.py tests/api/test_health_endpoints.py tests/test_api_process.py tests/collector/test_cli_run.py tests/collector/test_cli_fetch_once.py -q`. Expect: FAIL on the new and changed expectations only; `test_cli_fetch_once.py` passes before and after.
- [ ] **Step 4: Implement** migration, version, `applied_version`, CLI line, fixtures, doc edits.
- [ ] **Step 5: Run** the same set, then `make check`. Expect: PASS; record output.
- [ ] **Step 6: Commit** `feat(storage): schema version 2 with open interest tables (ADR-0003 P-6)`.

### Task 2: Synchronous open interest writer

**Files:**
- Create: `src/quasar_market_data/collector/open_interest_writer.py`, `tests/collector/test_open_interest_writer.py`

**Interfaces:**
- `MAX_ROWS_PER_TRANSACTION: Final = 100`.
- `class OpenInterestWriteError(StorageError)`; `class OpenInterestWriteOutcomeUnknown(OpenInterestWriteError)`; `class OpenInterestWriteBusy(OpenInterestWriteError)` (raised when the failing `sqlite3.Error` has `sqlite_errorcode` `SQLITE_BUSY` before `COMMIT` was issued; rolled back, nothing written).
- `@dataclass(frozen=True) OpenInterestConflict(incoming: OpenInterest, stored: OpenInterest, newly_recorded: bool)`.
- `@dataclass(frozen=True) OpenInterestBatchReport(inserted: int, duplicates: int, conflicts: tuple[OpenInterestConflict, ...], lock_wait_ns: int, duration_ns: int)`.
- `class OpenInterestWriter(conn: sqlite3.Connection, *, monotonic_ns: Callable[[], int] = time.monotonic_ns)`: checks the connection like `CandleWriter` (copied check, raising `OpenInterestWriteError`) and `verify_schema`; `close()`; `unusable: bool`; `write_transaction(rows: Sequence[OpenInterest], *, ingested_at_ms: int) -> OpenInterestBatchReport`.

Semantics: closed or unusable → `OpenInterestWriteError`; `ensure_timestamp_ms(ingested_at_ms)`; each row `type(row) is OpenInterest` (`TypeError`); `len(rows) > 100` → `ValueError`; all before any SQL. Empty → zero report, no transaction. Otherwise `BEGIN IMMEDIATE` … `COMMIT`; per row `INSERT … ON CONFLICT DO NOTHING`, else read stored values, equal → duplicate, different → conflict insert `ON CONFLICT DO NOTHING` with `first_seen_at_ms = ingested_at_ms`, `newly_recorded = rowcount == 1`. Failure classification as `CandleWriter._failure`. When the rollback fails or the transaction survives, the writer marks itself unusable **and closes the connection** (releasing any lock). The writer does not check eligibility (OI-4 passes only eligible rows).

- [ ] **Step 1: Write failing tests** (v2 file; a `FaultyConnection` factory that migrates and sets `busy_timeout`; fault prefix for the table is `"INSERT INTO open_interest ("`, never the bare table name; `statements` cleared after the constructor where a test counts statements):
  - `test_rows_are_inserted_once`; `test_differing_row_is_recorded_not_written_over` (`newly_recorded is True`, stored row unchanged, one conflict row with the incoming values and the second `ingested_at_ms`); `test_same_differing_value_twice_is_one_conflict_row` (`newly_recorded is False` the second time).
  - `test_more_than_100_rows_is_refused_before_any_sql` (101 rows → `ValueError`, `statements == []` after clearing); `test_rows_must_be_open_interest`.
  - `test_failure_mid_transaction_rolls_back` (fault on the 2nd row insert → `OpenInterestWriteError`, nothing stored, retry stores all); `test_commit_that_took_effect_is_outcome_unknown` (retry reports all duplicates, no conflict).
  - `test_busy_is_classified` (another connection holds `BEGIN IMMEDIATE`, writer busy 50 ms → `OpenInterestWriteBusy`, nothing stored, writer usable after release).
  - `test_failed_rollback_closes_the_connection` (faults on the insert and on `ROLLBACK` → `unusable`, next call raises naming "unusable", and a second connection can `BEGIN IMMEDIATE` at once).
  - `test_constructor_refuses_v1_database_and_bad_connections` (v1 → `SchemaMismatchError`; read-only, `recursive_triggers` off, not WAL → `OpenInterestWriteError`).
  - `test_durations_are_measured` (fake `monotonic_ns` sequence → exact `lock_wait_ns` and `duration_ns`).
- [ ] **Step 2: Run** `uv run pytest tests/collector/test_open_interest_writer.py -q`. Expect: FAIL.
- [ ] **Step 3: Implement.** **Step 4: Run** the tests and `make check`. Expect: PASS; record output.
- [ ] **Step 5: Commit** `feat(collector): idempotent open interest writer with conflict recording (ADR-0003 P-8)`.

### Task 3: `CandleWriteActivity`

**Files:**
- Create: `src/quasar_market_data/collector/candle_write_activity.py`, `tests/collector/test_candle_write_activity.py`

**Interfaces:** `class CandleWriteActivity` with `writing() -> contextlib.AbstractContextManager[None]` (increments on enter, decrements on exit, also on exception), `active: bool`, `async wait_idle() -> None` (returns at once when idle, else when the count next reaches 0). One integer count and one `asyncio.Event` set while the count is 0. Docstring: the candle path only enters and leaves it and never waits for open interest (P-9 rule 2); a caller that must be sure must re-check `active` after `wait_idle` returns.

- [ ] **Step 1: Write failing tests** (task plus a few `await asyncio.sleep(0)` and `assert not task.done()`, no `wait_for(…, 0)`): idle at start; nested `writing()`; exception still decrements; a waiter resumes only after the last exit; two waiters both resume; no public decrement.
- [ ] **Step 2: Run.** Expect FAIL. **Step 3: Implement.** **Step 4: Run** and `make check`. Expect PASS.
- [ ] **Step 5: Commit** `feat(collector): candle write activity signal for open interest to yield to (ADR-0003 P-9)`.

### Task 4: `AsyncOpenInterestWriter`

**Files:**
- Create: `src/quasar_market_data/collector/async_open_interest_writer.py`, `tests/collector/test_async_open_interest_writer.py`

**Interfaces:**
- `DEFAULT_OPEN_INTEREST_BUSY_TIMEOUT_MS: Final = 1_000`.
- `@dataclass(frozen=True) OpenInterestWriteReport(inserted: int, duplicates: int, conflicts: tuple[OpenInterestConflict, ...], lock_wait_ns: tuple[int, ...], durations_ns: tuple[int, ...], yields: int, yield_wait_ns: int)`. `yields` counts chunks that had to wait at least once.
- `class OpenInterestPageError(OpenInterestWriteError)` with `.cause: OpenInterestWriteError`, `.committed_transactions: int`, `.partial: OpenInterestWriteReport`; raised `from` the cause.
- `class AsyncOpenInterestWriter`: `@classmethod async open(cls, path: Path, *, activity: CandleWriteActivity, busy_timeout_ms: int = DEFAULT_OPEN_INTEREST_BUSY_TIMEOUT_MS, connect: Callable[[Path, int], sqlite3.Connection] | None = None, monotonic_ns: Callable[[], int] = time.monotonic_ns)`; `state: WriterState`; `async write(rows: Sequence[OpenInterest], *, ingested_at_ms: int) -> OpenInterestWriteReport`; `async close()`. `connect` defaults to `open_writer(path, busy_timeout_ms=…)`. `monotonic_ns` is used for `yield_wait_ns` only; the sync writer gets `time.monotonic_ns` unless `connect` tests inject their own writer clock (state it in the docstring).

Semantics: own `ThreadPoolExecutor(max_workers=1, thread_name_prefix="open-interest-writer")`; lifecycle, cancellation and close as `AsyncCandleWriter`. `write` splits rows in input order into chunks of 100. Per chunk: await the previous transaction's future (shielded) if it is still running; `while activity.active: await activity.wait_idle()`; submit `write_transaction`; await it. A failing chunk raises `OpenInterestPageError`; committed chunks stay. Cancellation between chunks starts no further chunk.

- [ ] **Step 1: Write failing tests** (v2 file):
  - `test_page_of_500_is_five_transactions` (5 `BEGIN IMMEDIATE`, ≤ 100 `"INSERT INTO open_interest ("` between each `BEGIN` and `COMMIT`).
  - `test_failure_inside_the_third_transaction_keeps_two_and_retry_completes`: fault on the 251st `"INSERT INTO open_interest ("` → `committed_transactions == 2`, `partial.inserted == 200`, 200 rows stored; re-send → `inserted == 300`, `duplicates == 200`, no conflicts.
  - `test_outcome_unknown_is_the_cause` (`.cause` is `OpenInterestWriteOutcomeUnknown`, `committed_transactions == 0`).
  - `test_busy_database_fails_only_that_transaction` (`.cause` is `OpenInterestWriteBusy`; nothing stored; succeeds after release).
  - `test_waits_while_candle_write_is_active` (deterministic guard: no `BEGIN` while entered; completes after exit with `yields == 1`).
  - `test_reentry_in_the_same_iteration_keeps_waiting`: exit and immediately re-enter `writing()` without yielding to the loop → still no `BEGIN`; completes after the second exit.
  - `test_cancel_between_transactions`: the `connect` proxy sets a `threading.Event` after the 1st `COMMIT`; the test awaits it via `asyncio.to_thread`, enters `activity.writing()`, cancels the write → 100 rows stored, no further `BEGIN`, writer open and usable.
  - `test_write_after_cancel_waits_for_the_running_transaction`: a proxy blocks the 1st `COMMIT` on a `threading.Event`; cancel the write, start a new write, enter the activity; assert the new write's `BEGIN` does not happen before the blocked one commits and the activity exits.
  - `test_lifecycle` (write after close raises, double close, thread name, `PRAGMA busy_timeout` is 1000).
- [ ] **Step 2: Run.** Expect FAIL. **Step 3: Implement.** **Step 4: Run** and `make check`. Expect PASS; record output.
- [ ] **Step 5: Commit** `feat(collector): async open interest writer on its own thread, transactions of at most 100 rows (ADR-0003 P-9)`.

### Task 5: Contention with the candle writer on a real file

**Files:**
- Create: `tests/collector/test_open_interest_candle_contention.py`

Tests only; a failure is fixed in Tasks 2–4, never by weakening the test. The candle side is observed by monkeypatching `async_candle_writer.open_writer` with a recording connection factory (test code only; candle code unchanged).

- [ ] **Step 1: Write the tests** (one file, both async writers in one loop, shared activity; the test wraps candle writes in `activity.writing()` as OI-4 will):
  - `test_candle_write_waits_for_at_most_one_open_interest_transaction`: the OI proxy signals "reached COMMIT #1" and blocks on a `threading.Event`; the test then enters the activity and starts a candle write; the candle proxy signals "BEGIN IMMEDIATE issued"; only then the test releases OI `COMMIT` #1. Timeline (`monotonic_ns` recorded by both proxies): candle `COMMIT` after OI `COMMIT` #1 and before OI `BEGIN` #2; exactly one OI `BEGIN` before the candle `COMMIT`; all rows stored. This test catches a removed yield only by timeline; `test_waits_while_candle_write_is_active` is the deterministic guard.
  - `test_busy_on_open_interest_never_reaches_the_candle_writer`: a third connection holds the write lock for 300 ms; the OI write (busy 50 ms) fails with `.cause` `OpenInterestWriteBusy`; a candle write started at the same time succeeds after release; no conflict rows.
- [ ] **Step 2: Run** `uv run pytest tests/collector/test_open_interest_candle_contention.py -v` five times (`--count` is not installed: a shell loop). Expect: PASS every time.
- [ ] **Step 3: Run** `make check`. **Step 4: Commit** `test(collector): open interest yields to candle writes on a real SQLite file (ADR-0003 P-9)`.

### Task 6: 100-row transaction benchmark

**Files:**
- Create: `tools/bench_open_interest_write.py` (standard library only), `tests/tools/test_bench_open_interest_write.py`
- Modify: `Makefile` (`bench-oi-write`, not part of `check`)

**Interfaces:** `python tools/bench_open_interest_write.py --schema PATH [--dir DIR] [--transactions N] [--max-p99-ms MS] [--keep]`. Imports only the stdlib (it must run in `python:3.12-slim`). `--schema` is `002_open_interest.sql`; the benchmark creates only the two open interest tables from it plus their triggers (it skips nothing else in that file). `DIR` must be missing or empty (created if missing; refused otherwise, before anything is written; default a new `mkdtemp`). Connection settings as `open_writer` plus `busy_timeout` 1000. Writes `N` (default 1 000) transactions of 100 new rows with the same `INSERT` statement text as `OpenInterestWriter`, timing lock-acquired → end of `COMMIT`. Prints `oi_write rows_per_tx=100 transactions=N p50_ms=… p99_ms=… max_ms=… dir=… sqlite=… date=…` (nearest-rank percentiles). Exit 1 only when `--max-p99-ms` is given and exceeded. Removes its files unless `--keep`.

- [ ] **Step 1: Write failing tests**: `--transactions 5` in `tmp_path` prints every field and exits 0 (no threshold); non-empty dir refused before writing; a missing dir is created and removed; files removed unless kept; the module imports no network module and no `quasar_market_data`; nearest-rank on a fixed list; `test_insert_statement_matches_the_writer` (the benchmark's statement equals the writer module's constant).
- [ ] **Step 2: Run.** Expect FAIL. **Step 3: Implement.** **Step 4: Run** and `make check`. Expect PASS.
- [ ] **Step 5: Run** `make bench-oi-write` locally (`--max-p99-ms 50`) and record the line, disk, sample size and date under "Measurements". The empty database understates B-tree depth; note it.
- [ ] **Step 6: Commit** `test: benchmark for the 100-row open interest transaction (ADR-0003 P-9)`.
- [ ] **Step 7 (optional since ADR-0003 amendment A-1; was a hard stop):** on the VPS, copy the two files of the branch head, find the volume's filesystem (`docker volume inspect quasar-data` → `Mountpoint`; `df` on it), create an empty directory on that filesystem **outside** the volume, and run `docker run --rm -v <dir>:/bench -v $PWD:/src:ro python:3.12-slim python /src/bench_open_interest_write.py --schema /src/002_open_interest.sql --dir /bench/run --max-p99-ms 50`, right after a Collector `round` line. Record the line here. If p99 > 50 ms, lower `MAX_ROWS_PER_TRANSACTION` and re-measure (P-9).

### Task 7: Documentation and invariant register

**Files:**
- Modify: `README.md` (Deployment), `docs/invariants.md`

- [ ] **Step 1: README** paragraph **"Schema upgrade (version 2)"**: backup as in "Backup"; `docker compose pull && docker compose up -d`; wait until `/health/ready` answers `200` with `"schema_version":2` and `docker compose logs collector` shows `migrated to version 2 (from 1)`; the API answers `503 not_ready` (`schema_mismatch`) until both have happened (expected seconds to a minute, not measured); **no rollback by redeploy** (the previous Collector refuses the newer database; going back means restoring the backup with the previous release and loses the candles collected since). Plus how to run the benchmark (Task 6 Step 7).
- [ ] **Step 2: Register rows** INV-073 onward (INV-068 stays unused), ENFORCED unless stated, evidence as `tests/...::test_name[case]`:
  - INV-073 immutability of both open interest tables on a writer connection (`test_open_interest_is_immutable[...]` cases, control cited in the mechanism);
  - INV-074 value columns accept only non-negative canonical decimal text;
  - INV-075 the writer is idempotent, never writes a differing row over a stored one, records an observation once and reports whether it is new;
  - INV-076 an open interest transaction holds at most 100 rows; a page that fails part-way keeps its committed transactions and a retry completes it without duplicates or conflicts;
  - INV-077 the open interest writer begins no transaction while a candle write is active and has at most one transaction in flight; a failed rollback releases the write lock; scope: the writer only, the candle loop enters the activity from OI-4;
  - INV-078 PLANNED (OI-4): only eligible open interest rows are persisted (mirrors INV-007);
  - INV-079 PLANNED (OI-4): open interest never delays or stops candle collection.
- [ ] **Step 3: Run** `uv run pytest tests/test_invariant_register.py -q` and `make check`. **Step 4: Commit** `docs: schema 2 release procedure and open interest storage invariants`.

### Task 8: Implementation review and pull request

- [ ] **Step 1:** `make check` on the branch head; record output.
- [ ] **Step 2:** Independent implementation review (fresh agent, read-only, against ADR-0003, this plan and `git diff main...HEAD`); apply every finding with its own commit; re-run `make check`.
- [ ] **Step 3:** Push and open the PR `feat: OI-2 open interest schema v2 and writer`: summary, decisions (contract example change), local and VPS measurements (VPS pending until Task 6 Step 7), verification output, both reviews and what they changed, that partial migrations are covered by the existing `test_failing_migration_is_atomic`, `Closes #2`, attribution line.

## Measurements

- **Local, 2026-11-02:** `oi_write rows_per_tx=100 transactions=1000 p50_ms=0.171 p99_ms=0.236 max_ms=1.386 sqlite=3.53.1`. macOS 27.0.1, internal SSD, APFS, temp directory. **Not representative:** on macOS SQLite's `synchronous=FULL` issues `fsync`, not `F_FULLFSYNC` (`PRAGMA fullfsync` is off), so the write is not forced to the medium; the empty database also understates B-tree depth. This only shows the benchmark works.
- **VPS (target disk):** not run. ADR-0003 amendment A-1 (approved by the user on 2026-11-02) moves the criterion to the soak after OI-4, read from the Collector's hourly measurement line (issue #7). Task 6 Step 7 is therefore optional.

## Review history

- 2026-11-02, independent plan review (fresh agent, read-only). Applied all 22 findings: CLI line moved to stderr in `run` only with the ADR wording, so candle CLI stdout is unchanged (1, blocker); full list of tests and docs that change with version 2 (2); impossible REAL/INTEGER cases replaced, accept and coercion-control tests added (3); `INSERT OR REPLACE` / upsert on both tables plus the `recursive_triggers` control (4); connection check copied, `candle_writer.py` untouched (5); `while active` loop and same-iteration re-entry test (6); last transaction awaited after cancellation (7); unusable writer closes its connection (8); `.cause`, `OpenInterestWriteBusy`, `.partial`, confirmed-commit count (9); `threading.Event` gates instead of timing (10); `FaultyConnection` factory, statement clearing, prefix and clock details (11); fault inside the 3rd transaction (12); measurement kept in OI-2 per the ADR with a stdlib benchmark runnable without a release, outside the production volume (13); threshold optional in the smoke test (14); lock wait and transaction time separated (15); contract wording raised for approval before Task 1 (16); `applied_version` errors defined (17); `WriterState` reused, cross-references (18); `newly_recorded` after an unknown outcome documented (19); no `wait_for(…, 0)` (20); register rows split, IDs from INV-073, case ids (21); endpoint-level 503 test (22).
- 2026-11-02, independent implementation review (fresh agent, read-only, `main...HEAD`; it ran `make check` and three mutations of its own). Applied all 6 findings, each with its own commit and a test that failed first unless stated: two concurrent `write()` calls could queue a transaction past the activity check; now both conditions are re-checked after every `await`, `yields` counted once per chunk (1); a cancelled waiter left "Future exception was never retrieved" (2); extended busy codes and a rolled-back busy `COMMIT` classified as busy (3); `close()` mid-page raises `OpenInterestPageError` with the committed part (4); the VPS benchmark runs in the Dockerfile's pinned runtime image, busy timeout and image tested against the writer and Dockerfile (5, no RED: new drift guards); INV-073 cites all conflict-table cases, and a test pins `newly_recorded=False` after an outcome-unknown commit (6, the behaviour already held, so the test passed at once).
