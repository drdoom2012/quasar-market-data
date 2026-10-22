# S6 SQLite Schema v1 and Migration Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the approved SQLite schema version 1 through a forward-only, checksummed, atomic migration runner, with writer and reader connection factories that carry the settings the schema's guarantees depend on.

**Architecture:** A new leaf package `quasar_market_data/storage/` that both processes may use and `domain` may not. `connection.py` opens writer and reader connections with the ADR-0002 P-1 settings. `migrations/001_schema_v1.sql` holds the DDL of ADR-0002 P-2, P-4, P-5, P-6 and P-8. `migrate.py` (Collector only, enforced by an import-linter contract) applies migrations; `schema.py` holds the version constants and the read-side `verify_schema` the API uses to fail closed. No writer logic (S7), no conflict logic (S8), no CLI.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, `hashlib`, `importlib.resources`; pytest, mypy strict, ruff, import-linter. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (S6 in section 8, sections 6.3, 6.4, 11). Approved decisions: `docs/adr/0002-sqlite-schema-v1.md` (Accepted 2026-10-22; P-1 to P-9, Q1 to Q5 as recommended). Related: `docs/adr/0001-binance-ingestion-fidelity-and-closed-candles.md`, `docs/superpowers/plans/2026-10-22-s5-closed-candle-selection-and-parser.md` (style, register rules).

## Global Constraints

- Branch `market-data-init`; one commit per task, message in each task; trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Do not push.
- The schema is exactly ADR-0002: the SQL implements P-2 (`candles`), P-4 (triggers), P-5 (`candle_conflicts`), P-6 (`unavailable_candles`) and P-8 (`schema_migrations`), with `<decimal-shape>` expanded verbatim as defined in P-2. No extra table, column, index, trigger or pragma-dependent behaviour. Do not edit `docs/adr/`, `pyproject.toml` dependencies, `uv.lock`, or the domain purity allowlist. Contract changes in `pyproject.toml` are limited to the ones this plan names.
- Stdlib `sqlite3` only; minimum SQLite 3.37 (`STRICT`); a lower version is a hard error (`UnsupportedSqliteError`), never a fallback. Every table is `STRICT`.
- Connections are created with `isolation_level=None`: transactions are explicit (`BEGIN IMMEDIATE` … `COMMIT`/`ROLLBACK`).
- Writer settings on every writer connection: `journal_mode=WAL` (verified, not assumed), `synchronous=FULL`, `recursive_triggers=ON`, `busy_timeout` (default 5000 ms). Reader: `mode=ro` URI, `query_only=ON`, same busy timeout; it never creates a database and never migrates.
- `application_id` constant `0x514D4431`; `SCHEMA_VERSION = 1`. Only the Collector migrates; the API verifies.
- Fail closed: a foreign database, a tampered or missing migration, a database newer than the code, or a half-applied migration is an error that leaves the file untouched.
- `domain` is unchanged and contains no `float` and no `sqlite3` import. `storage` imports only the stdlib and (later) `domain`; it must not import `collector`, `api`, `ccxt`, `fastapi` or `talib`.
- Tests use `tmp_path` databases only, never production storage. `tests/` is mypy-strict; deliberately wrong runtime types only through `tests/typing_helpers.py::untyped`. A PreToolUse hook runs `make check` on `git commit`; run `make check` before each commit and report real output.
- TDD: failing test first, record RED and GREEN output.

## Review Focus

Failure modes the spec and ADR imply that a reasonable user would expect handled, most likely first. Each has a test in the owning task.

1. A writer connection without `recursive_triggers=ON` lets `INSERT OR REPLACE` silently overwrite a stored candle → the factory sets the pragma (Task 1) and the control test shows why it matters (Task 2).
2. A database file that is not ours (another SQLite file, or one with unknown tables) → `migrate` and `verify_schema` refuse and leave it untouched (Tasks 3, 4).
3. A migration that fails halfway → no partial schema and no `schema_migrations` row (Task 3).
4. The API pointed at a missing path must not create an empty database; pointed at a never-migrated or newer database it must report a mismatch, not read (Tasks 1, 4).
5. Non-canonical value text (exponent, sign, whitespace, leading or trailing zeros, empty string) in a value column → rejected by the schema; the one thing it cannot catch, a bound float, is documented by a test (Task 2).

---

### Task 1: Connection factories and the storage import contracts

**Files:**
- Create: `src/quasar_market_data/storage/__init__.py` (docstring only), `src/quasar_market_data/storage/connection.py`, `tests/storage/__init__.py`, `tests/storage/test_connection.py`
- Modify: `pyproject.toml` (import-linter contracts only), `tests/test_import_contracts_negative.py`

**Interfaces:**
- Produces in `connection.py`:
  - `class StorageError(Exception)`; `class UnsupportedSqliteError(StorageError)`.
  - `MIN_SQLITE_VERSION: Final = (3, 37, 0)`; `DEFAULT_BUSY_TIMEOUT_MS: Final = 5000`.
  - `ensure_sqlite_version(version: tuple[int, ...] | None = None) -> None`: compares `version` (default: `sqlite3.sqlite_version_info`, read at call time) with `MIN_SQLITE_VERSION`; raises `UnsupportedSqliteError` when lower.
  - `open_writer(path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection`: checks the SQLite version first, creates the file if missing, applies the writer settings, and raises `StorageError` if the resulting `journal_mode` is not `wal` (for example `":memory:"`), closing the connection. `busy_timeout_ms` must be a non-negative `int` (`TypeError` for `bool`/non-int, `ValueError` for negative).
  - `open_reader(path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection`: version check first; opens `Path.as_uri() + "?mode=ro"` with `uri=True`; a missing file raises `sqlite3.OperationalError` and is not created; then `PRAGMA query_only=ON` and the busy timeout.
- Contracts (`pyproject.toml`): add `quasar_market_data.storage` to the `domain is pure` forbidden modules; add a `forbidden` contract named `storage is a leaf below collector and api` with source `quasar_market_data.storage` and forbidden `quasar_market_data.collector`, `quasar_market_data.api`, `ccxt`, `fastapi`, `talib`.

- [ ] **Step 1: Write failing connection tests** in `tests/storage/test_connection.py`:
  - `test_ensure_sqlite_version`: `(3, 36, 9)` raises `UnsupportedSqliteError`; `(3, 37, 0)` and `(3, 53, 1)` pass; with no argument the running version passes.
  - `test_writer_connection_settings`: after `open_writer(tmp_path / "m.db")`: `PRAGMA journal_mode` is `wal`, `synchronous` is `2`, `recursive_triggers` is `1`, `busy_timeout` is `5000` (and `1234` when passed), `conn.isolation_level is None`, the file exists.
  - `test_writer_refuses_a_database_without_wal`: `open_writer(Path(":memory:"))` raises `StorageError`.
  - `test_writer_busy_timeout_must_be_a_non_negative_int`: `untyped(True)`, `untyped(1.5)` → `TypeError`; `-1` → `ValueError`.
  - `test_reader_is_read_only_and_sees_committed_writes`: the writer creates a table and commits a row; the reader selects it; `CREATE TABLE` and `INSERT` on the reader raise `sqlite3.OperationalError`; `PRAGMA query_only` is `1`.
  - `test_reader_does_not_create_a_missing_database`: `sqlite3.OperationalError`, and `tmp_path` still has no file.
  - `test_unsupported_sqlite_blocks_both_factories`: with `monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 0))`, `open_writer` and `open_reader` raise `UnsupportedSqliteError` and create no file.
- [ ] **Step 2: Run** `uv run pytest tests/storage/test_connection.py -v`. Expect: FAIL (module missing).
- [ ] **Step 3: Implement** `storage/__init__.py` and `connection.py` per the interfaces; run the tests. Expect: PASS.
- [ ] **Step 4: Write failing contract tests.** In `tests/test_import_contracts_negative.py` replace the hard-coded "2 kept" and single-other-contract logic with a `CONTRACT_NAMES` tuple of every contract: a case with `broken_contract=None` asserts `"{len(CONTRACT_NAMES)} kept, 0 broken"`; a broken case asserts `"1 broken"`, `"{name} BROKEN"` and `"{other} KEPT"` for every other contract. Add cases `domain_importing_storage_fails_domain_is_pure` (probe `domain/_probe.py`: `import quasar_market_data.storage`), `storage_importing_collector_fails_storage_leaf` and `storage_importing_api_fails_storage_leaf` (probes in `storage/_probe.py`), and `storage_importing_sqlite3_is_allowed` (positive: the unmodified-style pass with a probe `import sqlite3`). Run `uv run pytest tests/test_import_contracts_negative.py -v`. Expect: FAIL (new contract and forbidden entry missing).
- [ ] **Step 5: Edit `pyproject.toml`** as in Interfaces; run the contract tests, `make imports`, then `make check`. Expect: pass; record output.
- [ ] **Step 6: Commit** `feat(storage): connection factories with ADR-0002 settings and storage import contracts`.

### Task 2: Schema version 1 and its guard tests

**Files:**
- Create: `src/quasar_market_data/storage/migrations/__init__.py` (docstring only), `src/quasar_market_data/storage/migrations/001_schema_v1.sql`, `tests/storage/conftest.py`, `tests/storage/test_schema_v1.py`

**Interfaces:**
- Consumes: `open_writer` (Task 1).
- Produces: the SQL file creating exactly these objects: tables `candles`, `candle_conflicts`, `unavailable_candles`, `schema_migrations`; explicit indexes `candle_conflicts_observation` (unique, with `coalesce(turnover, '')`), `candle_conflicts_slot`; triggers `candles_no_update`, `candles_no_delete`, `candle_conflicts_no_update`, `candle_conflicts_no_delete`, `unavailable_candles_no_update`, `unavailable_candles_no_delete` (each `RAISE(ABORT, '<table> are immutable')`-style message containing the word `immutable`). Column names, types and CHECKs as in ADR-0002 P-2, P-5, P-6, P-8; `schema_migrations` has no triggers.
- Fixture in `conftest.py`: `schema_conn(tmp_path) -> Iterator[sqlite3.Connection]` opens `open_writer(tmp_path / "market.db")`, runs the packaged `001_schema_v1.sql` (read through `importlib.resources`) with `executescript`, yields, closes.

Candle row used below: `CANDLE = ("binance-usdm", "BTC/USDT:USDT", "5m", 1792676700000, "81700.8", "81700.8", "81454.7", "81555.9", "2123.997", "173139160.7713", 1)` for the eleven `candles` columns in table order.

- [ ] **Step 1: Write failing tests** in `tests/storage/test_schema_v1.py` (RED before the SQL exists means the fixture fails; that counts as the expected failure):
  - `test_schema_objects_are_exactly_the_approved_set`: names in `sqlite_master` excluding `sqlite_%` equal the 4 tables, 2 indexes and 6 triggers above, nothing else (this pins P-9: an unapproved object fails the suite).
  - `test_candle_round_trips_exact_text`: insert `CANDLE`, select returns the identical strings; turnover `NULL` is accepted.
  - `test_value_columns_reject_non_canonical_decimal_text`, parametrized over each of `open, high, low, close, volume, turnover` and the values `"1e5"`, `"-1"`, `"1.50"`, `"01"`, `"1."`, `".5"`, `"1.2.3"`, `"1 "`, `""` (ids `<column>-<case>`): `sqlite3.IntegrityError`. A separate test accepts `"0"`, `"100"`, `"0.5"`, and rejects `NULL` for every column except `turnover`.
  - `test_open_time_must_be_an_integer_in_range`: `946684799999`, `4102444800001` and the text `"abc"` raise `IntegrityError`; the bounds themselves are accepted.
  - `test_duplicate_identity_is_rejected_and_do_nothing_keeps_the_row`: a plain second insert raises; `INSERT … ON CONFLICT DO NOTHING` with a differing `close` leaves the stored `close` unchanged and changes nothing.
  - `test_candles_are_immutable`: `UPDATE`, `DELETE`, `INSERT OR REPLACE` and `INSERT … ON CONFLICT DO UPDATE` each raise `IntegrityError` matching `immutable`; the row is unchanged afterwards.
  - `test_insert_or_replace_overwrites_without_recursive_triggers` (control, documents ADR-0002 E-2): after `PRAGMA recursive_triggers=OFF` on the fixture connection, `INSERT OR REPLACE` succeeds and the stored value changes. A comment states that this is why the writer factory must set the pragma.
  - `test_bound_float_is_coerced_not_rejected` (documents the limit in ADR-0002 E-3): binding Python `0.5` to `close` is stored as the text `"0.5"`; the guard against floats lives in the writer's types, not here.
  - `test_conflict_recording_is_idempotent`: `INSERT OR IGNORE` of the same rejected observation twice with `NULL` turnover and twice with `"5"` leaves two rows; a different `close` adds a row; `first_seen_at_ms` of the first insert is kept.
  - `test_conflict_values_use_the_decimal_shape` (one rejected value is enough) and `test_conflict_rows_are_immutable` (`UPDATE`, `DELETE`).
  - `test_unavailable_reason_is_a_closed_enum`: `"EXCHANGE_NO_DATA"` accepted, `"OTHER"` rejected; `test_unavailable_slot_is_unique`; `test_unavailable_rows_are_immutable`.
  - `test_slot_may_hold_a_candle_and_an_unavailable_row` (documents ADR-0002 P-6: no cross-table prevention; coverage reports it as conflicted later).
- [ ] **Step 2: Run** `uv run pytest tests/storage/test_schema_v1.py -v`. Expect: FAIL (SQL file and fixture missing).
- [ ] **Step 3: Write `001_schema_v1.sql`** (DDL per ADR-0002, `<decimal-shape>` expanded in every value column check) and `conftest.py`. Run the tests. Expect: PASS.
- [ ] **Step 4: Run** `make check`. Expect: pass; record output.
- [ ] **Step 5: Commit** `feat(storage): schema v1 DDL with guard tests`.

### Task 3: Migration runner

**Files:**
- Create: `src/quasar_market_data/storage/schema.py`, `src/quasar_market_data/storage/migrate.py`, `tests/storage/test_migrate.py`
- Modify: `pyproject.toml` (one new contract), `tests/test_import_contracts_negative.py`

**Interfaces:**
- Consumes: `StorageError` (Task 1), `001_schema_v1.sql` (Task 2).
- Produces in `schema.py`: `APPLICATION_ID: Final = 0x514D4431`; `SCHEMA_VERSION: Final = 1`. (`verify_schema` is added in Task 4.)
- Produces in `migrate.py`:
  - `class MigrationError(StorageError)`.
  - `@dataclass(frozen=True) Migration(version: int, name: str, sql: str, checksum: str)` and `Migration.from_bytes(version: int, name: str, raw: bytes) -> Migration` (`checksum` = SHA-256 hex of `raw`, `sql` = `raw` decoded as UTF-8).
  - `load_migrations() -> tuple[Migration, ...]`: reads `*.sql` from the `quasar_market_data.storage.migrations` package; file names must match `^(\d{3})_([a-z0-9_]+)\.sql$`; versions must be unique and run `1..n` without a gap; otherwise `MigrationError`.
  - `migrate(conn: sqlite3.Connection, *, applied_at_ms: int, migrations: Sequence[Migration] | None = None) -> int`: returns the resulting version. `conn.isolation_level` must be `None`. `applied_at_ms` passes `ensure_timestamp_ms`-style rules (`int`, not `bool`, within `[946684800000, 4102444800000]`; reuse `domain.timestamps.ensure_timestamp_ms`). `migrations=None` means `load_migrations()`.
- Contract: add a `forbidden` contract named `only the collector migrates` with source `quasar_market_data.api` and forbidden `quasar_market_data.storage.migrate`.

Rules (ADR-0002 P-8, all checked before anything is changed):
1. Fresh means `PRAGMA application_id` is `0` and `sqlite_master` has no objects. A database that is not fresh and whose `application_id` is not `APPLICATION_ID` is foreign: `MigrationError`, untouched. (Verified 2026-10-22: `PRAGMA application_id` set inside `BEGIN IMMEDIATE` is rolled back with the transaction, and `sqlite3.complete_statement` treats a trigger body as one statement.)
2. Applied versions come from `schema_migrations` (absent on a fresh database). They must be exactly `1..k`, each present in the shipped list with equal `name` and `checksum`; `k` larger than the shipped maximum (database newer than the code), a gap, or a mismatch is a `MigrationError`.
3. Pending migrations are applied in order, each in its own `BEGIN IMMEDIATE` … `COMMIT`. On a fresh database the first transaction also sets `PRAGMA application_id`. Statements are split with `sqlite3.complete_statement` (trigger bodies contain semicolons), executed one by one, then the `schema_migrations` row is inserted with parameters; any exception rolls the transaction back and is re-raised as `MigrationError` (chained).

- [ ] **Step 1: Write failing tests** in `tests/storage/test_migrate.py` (`two = (m1, m2)` built with `Migration.from_bytes`; helper `fresh(tmp_path)` returns `open_writer(...)`):
  - `test_fresh_database_is_migrated_to_version_one`: `migrate(conn, applied_at_ms=1_792_677_000_000)` returns `1`; `application_id` is `APPLICATION_ID`; `schema_migrations` has one row with the shipped name, checksum and `applied_at_ms`; the four tables exist.
  - `test_migrate_is_idempotent`: a second call returns `1`, changes nothing (row count, `applied_at_ms` of the first row unchanged).
  - `test_pending_migration_is_applied_on_top` (version 1 then 2 with a trivial table): only version 2 runs, both rows present.
  - `test_failing_migration_is_atomic`: a second migration whose SQL creates a table and then a syntax error → `MigrationError`; the table does not exist, no row for version 2, version 1 intact.
  - `test_checksum_mismatch_is_refused`: update the stored checksum (via a raw connection); `MigrationError`; nothing else changed.
  - `test_database_newer_than_the_code_is_refused`: stored version 2 with only migration 1 shipped → `MigrationError`.
  - `test_gap_in_applied_versions_is_refused`: stored versions `{1, 3}` → `MigrationError`.
  - `test_foreign_database_is_refused_untouched`: a database with `application_id` 12345, and another with `application_id` 0 but a table `t`, both → `MigrationError` and the file's `sqlite_master` is unchanged.
  - `test_non_autocommit_connection_is_refused` (`isolation_level=""`), `test_applied_at_ms_is_validated` (`untyped(True)`, `untyped(1.5)` → `TypeError`; `0` → `ValueError`).
  - `test_split_statements_keeps_trigger_bodies_together`, `test_trailing_comment_is_ignored`, `test_unterminated_statement_is_refused` (through a migration, since the splitter is private).
  - `test_shipped_migrations_are_consistent`: `load_migrations()` versions are `1..n`, and `SCHEMA_VERSION == n`.
  - `test_load_migrations_rejects_bad_names_and_gaps`: via a temporary package directory or by calling the private name parser with `"1_x.sql"`, `"001_X.sql"`, `"002_a.sql"` without `001`.
  - `test_migrated_database_matches_the_schema_v1_fixture`: the object inventory of a `migrate`d database equals that of the Task 2 `schema_conn` fixture.
- [ ] **Step 2: Write failing contract tests**: add `api_importing_storage_migrate_fails_only_the_collector_migrates` (probe `api/_probe.py`: `import quasar_market_data.storage.migrate`) and a positive `api_importing_storage_schema_is_allowed` (probe `api/_probe.py`: `import quasar_market_data.storage.schema`), and add the new contract name to `CONTRACT_NAMES`. Run `uv run pytest tests/storage/test_migrate.py tests/test_import_contracts_negative.py -v`. Expect: FAIL.
- [ ] **Step 3: Implement** `schema.py` and `migrate.py` per Rules and Interfaces; add the contract to `pyproject.toml`. Run the same tests. Expect: PASS.
- [ ] **Step 4: Manual packaging check (evidence, not committed):** build a wheel into the scratchpad (`uv build --wheel --out-dir <scratchpad dir>`) and list it; the report states whether `001_schema_v1.sql` is inside. If the build cannot run offline, say that it was not verified.
- [ ] **Step 5: Run** `make check`. Expect: pass (including `make imports` with the new contract); record output.
- [ ] **Step 6: Commit** `feat(storage): forward-only checksummed migration runner`.

### Task 4: Read-side schema verification and end-to-end

**Files:**
- Create: `tests/storage/test_verify_schema.py`
- Modify: `src/quasar_market_data/storage/schema.py`

**Interfaces:**
- Consumes: `APPLICATION_ID`, `SCHEMA_VERSION` (Task 3), `StorageError`, `open_reader`, `open_writer`, `migrate`.
- Produces in `schema.py`: `class SchemaMismatchError(StorageError)`; `verify_schema(conn: sqlite3.Connection) -> None`: raises `SchemaMismatchError` unless `application_id` equals `APPLICATION_ID`, the `schema_migrations` table exists, and the applied versions are exactly `1..SCHEMA_VERSION`. It only reads. `schema.py` must not import `migrate`.

- [ ] **Step 1: Write failing tests** in `tests/storage/test_verify_schema.py`:
  - `test_collector_migrates_and_api_verifies`: a writer `migrate`s a `tmp_path` database; a separate reader connection passes `verify_schema`.
  - `test_reader_sees_a_candle_written_after_it_connected`: the reader is opened first, then the writer inserts and commits a candle row; the reader's next select returns it (WAL, separate connections); the reader still cannot write.
  - `test_unmigrated_database_is_a_mismatch`: an empty file created by `open_writer` → `SchemaMismatchError`.
  - `test_foreign_application_id_is_a_mismatch`, `test_older_database_is_a_mismatch` (a migrated database where the version-1 row is deleted through a raw connection), `test_newer_database_is_a_mismatch` (an extra row with version 2).
  - `test_verify_schema_never_writes`: verifying through a reader on a mismatching database raises and leaves the file's bytes (hash) unchanged.
- [ ] **Step 2: Run** `uv run pytest tests/storage/test_verify_schema.py -v`. Expect: FAIL (`verify_schema` missing).
- [ ] **Step 3: Implement** `verify_schema` and `SchemaMismatchError`. Run the tests. Expect: PASS.
- [ ] **Step 4: Run** `make check`. Expect: pass; record output.
- [ ] **Step 5: Commit** `feat(storage): read-side schema verification`.

### Task 5: Register and README

**Files:**
- Modify: `docs/invariants.md`, `README.md`

- [ ] **Step 1: Update the register.** Every ENFORCED row cites a test that fails when the invariant is violated; positive tests, controls and the documenting tests are not evidence. Test names are fixed by Tasks 1 to 4.
  - `INV-025` (ENFORCED): a stored candle cannot be updated, deleted or replaced through the schema on a writer connection (`recursive_triggers=ON`). Evidence: `tests/storage/test_schema_v1.py::test_candles_are_immutable`, `tests/storage/test_connection.py::test_writer_connection_settings`. The row states the scope: a connection that does not set the pragma loses the guarantee (control `test_insert_or_replace_overwrites_without_recursive_triggers`), and it guards against bugs, not against a person with write access to the file.
  - `INV-026` (ENFORCED): the value columns of `candles` accept only non-negative canonical decimal text. Evidence: `tests/storage/test_schema_v1.py::test_value_columns_reject_non_canonical_decimal_text`. The row states what the schema cannot do: OHLC relations (INV-021 and S7 writer) and a float bound to a text column, which SQLite converts to text before the check (`test_bound_float_is_coerced_not_rejected`).
  - `INV-027` (ENFORCED): migrations are atomic and fail closed: a failing migration leaves nothing behind, and a foreign, tampered or newer database is refused untouched. Evidence: `tests/storage/test_migrate.py::test_failing_migration_is_atomic`, `tests/storage/test_migrate.py::test_checksum_mismatch_is_refused`, `tests/storage/test_migrate.py::test_foreign_database_is_refused_untouched`, `tests/storage/test_migrate.py::test_database_newer_than_the_code_is_refused`.
  - `INV-028` (ENFORCED): the API side cannot import the migration runner, and the read-side check refuses an unmigrated, foreign, older or newer database. Evidence: `tests/test_import_contracts_negative.py::test_import_contracts_on_copied_package[api_importing_storage_migrate_fails_only_the_collector_migrates]`, `tests/storage/test_verify_schema.py::test_unmigrated_database_is_a_mismatch`, `tests/storage/test_verify_schema.py::test_newer_database_is_a_mismatch`.
  - Reword `INV-006` (storage now exists: the schema checks canonical text shape, INV-026; the writer is still S7), `INV-009` (a database-level guard against overwriting exists, INV-025; conflict recording is still S8) and `INV-014` (reader factory exists; the API read path is S10, Docker-level read-only is S21). Keep their statuses. Do not change any other row.
- [ ] **Step 2: Update `README.md`:** the boundary table lists the new contracts (domain may not reach `storage`; `storage` is a leaf; only the collector migrates) and the contract count; add two lines describing the `storage` package and that only the Collector migrates. Do not invent commands.
- [ ] **Step 3: Run** `uv run pytest tests/test_invariant_register.py -v`, then `make check`. Expect: pass; record output.
- [ ] **Step 4: Commit** `docs: record S6 invariants and storage layer`.

**S6 Definition of Done:** `make check` ran green; the schema equals ADR-0002 and an inventory test pins it; every guard in the Review Focus has a test that fails when the guard is removed; no `float` or `sqlite3` in `domain`; both new contracts are proven by negative cases; the register states only what the named negative tests prove; the report says whether the wheel contains the SQL file.

---

## Self-review notes

- **Spec coverage:** S6 "SQLite schema v1 and migration runner, gated by ADR-0002" → Tasks 2, 3. ADR-0002: P-1 (Task 1), P-2/P-4/P-5/P-6 (Task 2), P-7 is read-time logic for S11 and is not implemented here, P-8 (Tasks 3, 4), P-9 (inventory test, Task 2). Spec section 6.4 reader mode (Task 1). `docs/contracts/` is not created: ADR-0002 is the schema contract.
- **Type consistency:** `StorageError` (Task 1) is the base of `MigrationError` (Task 3) and `SchemaMismatchError` (Task 4); `APPLICATION_ID` and `SCHEMA_VERSION` are defined once in `schema.py` (Task 3) and consumed by `migrate.py` and `verify_schema`; `schema.py` never imports `migrate.py`, which is what lets the API contract hold.
- **Not in scope:** the writer and its thread (S7), conflict comparison logic (S8), any CLI or `init-db` command, checkpoints (S15, own migration with approval), coverage (S11), a `Clock` abstraction (the caller passes `applied_at_ms`).
- **Judgement calls (open to review):** the package name `storage` and the leaf-package contract; `application_id` value `0x514D4431` ("QMD1"); migration file naming `NNN_name.sql` with contiguous versions; SHA-256 of the file bytes as the checksum; `applied_at_ms` passed in rather than read from a clock; the reader does not verify checksums (version and `application_id` only, as the ADR says); statement splitting with `sqlite3.complete_statement` instead of `executescript`, because `executescript` commits implicitly and would break atomicity.
