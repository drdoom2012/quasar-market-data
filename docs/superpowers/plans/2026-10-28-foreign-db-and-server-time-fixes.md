# Foreign database refusal and out-of-range server time — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two defects found in the independent review of 2026-10-28: (1) `quasar-collector` switches a foreign
SQLite file to WAL before the migration runner refuses it; (2) an out-of-range `serverTime` from Binance ends the
whole collector instead of being an untrusted time reading.

**Architecture:** (1) The Collector checks the file's identity on a read-only connection *before* `open_writer` runs
`PRAGMA journal_mode=WAL`; the check lives in `storage/migrate.py` beside the runner and shares one identity rule with
`migrate`. (2) The Binance adapter range-checks `serverTime` and turns a bad value into `SourceError`, which
`fetch_once` already treats as "no observation" (time untrusted, nothing confirmed, bounded retry).

**Tech Stack:** Python 3.12, sqlite3 (stdlib), pytest, ruff, mypy strict, import-linter.

**Spec:** the review findings 1 and 2 (conversation of 2026-10-28), `docs/invariants.md` INV-027 and INV-011,
ADR-0002 P-8, ADR-0001 P-4, `src/quasar_market_data/collector/kline_source.py` ("Every failure to obtain data is a
`SourceError`").

## Global Constraints

- No schema change, no new migration, no change to public contracts or to `open_writer`'s signature (70 call sites).
- No new dependency; `storage` stays a leaf (no import of `collector` or `api`); `schema.py` must not import `migrate`.
- Timestamp range is the domain's: `MIN_TIMESTAMP_MS = 946_684_800_000`, `MAX_TIMESTAMP_MS = 4_102_444_800_000`
  (inclusive), via `ensure_timestamp_ms` in `domain/timestamps.py`.
- Unexpected exceptions stay fatal in the loop (fail closed on bugs); only the adapter's contract changes.
- Exit codes unchanged: a refused foreign file is exit `1` for both `fetch-once` and `run`, before any network call.
- Existing test hooks keep working: `cli._migrate`, `cli.open_writer` and `cli.migrate` remain the names tests patch.
- Run `make check` before every commit (the pre-commit hook runs the full suite).

## Review Focus

- A foreign file in WAL mode (not only rollback-journal mode): the main file must stay byte-identical — Task 1 test
  is parametrized over `delete` and `wal`.
- A file that is not SQLite at all (random bytes): refused with exit `1`, file unchanged, no traceback — Task 1.
- A 0-byte existing file: treated as fresh and migrated (today's behaviour), not refused — Task 1.
- `serverTime` exactly at the range bounds is accepted (no off-by-one that would make valid time untrusted) — Task 2.
- A bad `serverTime` during `run` keeps the process alive and every other symbol collecting — Task 2 collect test.

---

### Task 1: Refuse a foreign file before anything writes to it

**Files:**
- Modify: `src/quasar_market_data/storage/migrate.py` (add `refuse_foreign_file`, share the identity rule with `migrate`)
- Modify: `src/quasar_market_data/collector/cli.py:477-482` (`_migrate` calls `refuse_foreign_file` first)
- Modify: `docs/invariants.md` (INV-027 mechanism and evidence)
- Test: `tests/storage/test_migrate.py`, `tests/collector/test_cli_fetch_once.py`, `tests/collector/test_cli_run.py`

**Interfaces:**
- Produces: `refuse_foreign_file(path: Path) -> None` in `quasar_market_data.storage.migrate`. Raises
  `MigrationError` for a file that exists and is neither fresh (`application_id == 0` and no `sqlite_master` rows) nor
  ours (`application_id == APPLICATION_ID`); returns for a missing path (`open_writer` creates it), a 0-byte file, a
  fresh file or ours. A file that is not SQLite propagates `sqlite3.DatabaseError` unchanged. Never writes.
- Produces: private `_is_foreign(conn: sqlite3.Connection) -> bool` in the same module, used by both `migrate` and
  `refuse_foreign_file` so the rule exists once. `migrate` keeps its own check (a caller may pass any connection).

- [ ] **Step 1: Write the failing storage tests** in `tests/storage/test_migrate.py`

```python
def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def foreign_file(path: Path, journal_mode: str, kind: str) -> None:
    plain = sqlite3.connect(path)
    plain.execute(f"PRAGMA journal_mode={journal_mode}")
    if kind == "foreign_application_id":
        plain.execute("PRAGMA application_id = 12345")
    else:  # unknown_tables
        plain.execute("CREATE TABLE t (a INTEGER)")
        plain.execute("INSERT INTO t VALUES (1)")
    plain.commit()
    plain.close()


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
@pytest.mark.parametrize("kind", ["foreign_application_id", "unknown_tables"])
def test_refuse_foreign_file_leaves_the_file_byte_for_byte_unchanged(tmp_path, journal_mode, kind):
    path = tmp_path / "foreign.db"
    foreign_file(path, journal_mode, kind)
    before = sha(path)
    with pytest.raises(MigrationError, match="not a quasar-market-data database"):
        refuse_foreign_file(path)
    assert sha(path) == before
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA journal_mode").fetchone()[0] == journal_mode
    check.close()


def test_refuse_foreign_file_accepts_missing_empty_fresh_and_ours(tmp_path):
    missing = tmp_path / "missing.db"
    refuse_foreign_file(missing)
    assert not missing.exists()  # the check creates nothing
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    refuse_foreign_file(empty)
    ours = tmp_path / "ours.db"
    conn = open_writer(ours)
    migrate(conn, applied_at_ms=AT)
    conn.close()
    refuse_foreign_file(ours)


def test_refuse_foreign_file_lets_a_non_sqlite_file_fail_unchanged(tmp_path):
    path = tmp_path / "junk.db"
    path.write_bytes(b"not a database" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        refuse_foreign_file(path)
    assert path.read_bytes() == b"not a database" * 100
```

(Add the parameter type annotations mypy strict needs: `tmp_path: Path`, `journal_mode: str`, `kind: str`, `-> None`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/storage/test_migrate.py -k refuse_foreign_file -v`
Expected: FAIL with `ImportError: cannot import name 'refuse_foreign_file'`

- [ ] **Step 3: Implement `refuse_foreign_file(path: Path) -> None` and `_is_foreign(conn) -> bool` in `storage/migrate.py`**

`_is_foreign` holds today's rule from `migrate` (`application_id`, `count(*) FROM sqlite_master`; foreign when not
fresh and `application_id != APPLICATION_ID`); `migrate` calls it instead of its inline lines and raises the same
message as today. `refuse_foreign_file`: return if `not path.exists()`; otherwise open with
`storage.connection.open_reader` (`mode=ro`, `query_only`), call `_is_foreign`, close in `finally`, and raise
`MigrationError("not a quasar-market-data database (application_id or contents are foreign); the file was not modified")`.
Update the module docstring: the identity of an existing file is checked read-only before the writer opens it,
because `open_writer` sets WAL, which rewrites the file header. The check-then-open gap is accepted under the
runner's existing assumption of a single migrating process (INV-027).

- [ ] **Step 4: Run the storage tests**

Run: `uv run pytest tests/storage -v`
Expected: PASS (new tests and all existing `test_migrate.py` tests, including the `refused_untouched` ones)

- [ ] **Step 5: Write the failing CLI tests**

In `tests/collector/test_cli_fetch_once.py`:

```python
def test_a_foreign_database_file_exits_1_untouched_and_opens_no_source(tmp_path, capsys):
    db = tmp_path / "foreign.db"
    foreign_file(db, "delete", "unknown_tables")  # import from tests.storage.test_migrate
    before = sha(db)
    clock = FakeClock()
    opened: list[FakeSource] = []
    assert run_cli(db, FakeSource(clock, closed_rows()), clock, opened=opened) == 1
    assert sha(db) == before and opened == []
    assert "not a quasar-market-data database" in capsys.readouterr().err
```

In `tests/collector/test_cli_run.py` the same test named
`test_a_foreign_database_file_exits_1_untouched_and_opens_nothing`, using that file's
`run_cli(db, FakeSource(clock, forming_rows()), clock, AdvancingSleep(clock), opened=opened)`.

- [ ] **Step 6: Run them to verify they fail**

Run: `uv run pytest tests/collector/test_cli_fetch_once.py tests/collector/test_cli_run.py -k foreign_database_file -v`
Expected: FAIL on `sha(db) == before` (the file was switched to WAL)

- [ ] **Step 7: Call `refuse_foreign_file(db)` as the first line of `_migrate` in `collector/cli.py`**

Import it next to `migrate`. Nothing else in `cli.py` changes: `MigrationError` is a `StorageError` and
`sqlite3.DatabaseError` is a `sqlite3.Error`; both are already mapped to exit `1` in `_run_command` and `_fetch_once`.

- [ ] **Step 8: Run the CLI tests**

Run: `uv run pytest tests/collector/test_cli_fetch_once.py tests/collector/test_cli_run.py tests/collector/test_reconcile_run.py -v`
Expected: PASS

- [ ] **Step 9: Update INV-027 in `docs/invariants.md`**

Mechanism: add "`quasar-collector` checks an existing file read-only (`refuse_foreign_file`) before `open_writer`
sets WAL, so a foreign file is refused byte-for-byte unchanged; before this fix the WAL switch rewrote its header".
Evidence: append `tests/storage/test_migrate.py::test_refuse_foreign_file_leaves_the_file_byte_for_byte_unchanged`,
`tests/collector/test_cli_fetch_once.py::test_a_foreign_database_file_exits_1_untouched_and_opens_no_source`,
`tests/collector/test_cli_run.py::test_a_foreign_database_file_exits_1_untouched_and_opens_nothing`.
No literal `|` in the cell.

- [ ] **Step 10: Run `make check`**

Expected: lint, typecheck, imports (6 kept) and the full test suite pass, including `tests/test_invariant_register.py`.

- [ ] **Step 11: Commit**

```bash
git add src/quasar_market_data/storage/migrate.py src/quasar_market_data/collector/cli.py docs/invariants.md \
  tests/storage/test_migrate.py tests/collector/test_cli_fetch_once.py tests/collector/test_cli_run.py
git commit -m "fix(storage): refuse a foreign database file before the writer switches it to WAL"
```

---

### Task 2: An out-of-range `serverTime` is a `SourceError`, not a fatal error

**Files:**
- Modify: `src/quasar_market_data/collector/binance_usdm.py:95-103` (`fetch_server_time_ms`)
- Modify: `docs/invariants.md` (INV-011 mechanism, one clause)
- Test: `tests/collector/test_binance_usdm.py`, `tests/collector/test_collect_symbol.py`

**Interfaces:**
- Consumes: `ensure_timestamp_ms(value: int, name: str) -> int` from `quasar_market_data.domain.timestamps`
  (raises `ValueError` out of range; the adapter has already checked `type(...) is int`).
- Produces: `BinanceUsdmSource.fetch_server_time_ms() -> int` returns only values in
  `[MIN_TIMESTAMP_MS, MAX_TIMESTAMP_MS]` and raises `SourceError` otherwise.

- [ ] **Step 1: Write the failing adapter tests** in `tests/collector/test_binance_usdm.py`

```python
@pytest.mark.parametrize(
    "server_time",
    [0, 1_700_000_000, 946_684_799_999, 4_102_444_800_001, -1],
    ids=["zero", "seconds_not_ms", "one_below_min", "one_above_max", "negative"],
)
def test_server_time_out_of_range_is_a_source_error(server_time: int) -> None:
    exchange = FakeExchange()
    exchange.time_response = {"serverTime": server_time}
    with pytest.raises(SourceError, match="serverTime"):
        run(open_source(exchange).fetch_server_time_ms())


@pytest.mark.parametrize("server_time", [946_684_800_000, 4_102_444_800_000])
def test_server_time_at_the_range_bounds_is_accepted(server_time: int) -> None:
    exchange = FakeExchange()
    exchange.time_response = {"serverTime": server_time}
    assert run(open_source(exchange).fetch_server_time_ms()) == server_time
```

- [ ] **Step 2: Write the failing loop-level test** in `tests/collector/test_collect_symbol.py`

```python
def test_a_server_time_in_seconds_from_the_binance_adapter_is_untrusted_not_fatal(db: Path) -> None:
    exchange = FakeExchange()  # from tests.collector.test_binance_usdm
    exchange.time_response = {"serverTime": 1_700_000_000}
    clock = FakeClock()
    result = collect_real(db, open_source(exchange), clock)  # no exception escapes
    assert result.outcome is SymbolOutcome.GAVE_UP and result.attempts == 6
    assert result.last_problem == "time untrusted: NO_OBSERVATION"
    assert stored_opens(db) == []
```

Import `FakeExchange` and `open_source` from `tests.collector.test_binance_usdm` (alias `open_source` as
`open_binance_source` if the name is taken in this file). Widen `collect_real`'s `source` parameter from `FakeSource` to `KlineSource` (its only use is `collect_symbol`, which
takes a `KlineSource`).

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/collector/test_binance_usdm.py tests/collector/test_collect_symbol.py -k "server_time" -v`
Expected: the out-of-range cases and the loop test FAIL (no `SourceError`; the loop test raises `ValueError`); the
bound cases PASS.

- [ ] **Step 4: Range-check in `fetch_server_time_ms`**

After the existing `type(server_time) is not int` check, call `ensure_timestamp_ms(server_time, "serverTime")` and
turn its `ValueError` into `SourceError(f"serverTime {server_time} is outside the accepted range")` with `from error`.
Add one sentence to the module docstring: a reading the domain cannot accept is a failed time request, so the loop
retries with time untrusted instead of stopping.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/collector/test_binance_usdm.py tests/collector/test_collect_symbol.py -v`
Expected: PASS

- [ ] **Step 6: Update INV-011 in `docs/invariants.md`**

In the Mechanism cell, after the `fetch-once` clause, add: "an out-of-range `serverTime` is a `SourceError` from the
adapter, so time is untrusted and the loop retries; it is no longer a fatal error
(`tests/collector/test_collect_symbol.py::test_a_server_time_in_seconds_from_the_binance_adapter_is_untrusted_not_fatal`)".
The status stays PLANNED.

- [ ] **Step 7: Run `make check`**

Expected: everything passes.

- [ ] **Step 8: Commit**

```bash
git add src/quasar_market_data/collector/binance_usdm.py docs/invariants.md \
  tests/collector/test_binance_usdm.py tests/collector/test_collect_symbol.py
git commit -m "fix(collector): treat an out-of-range Binance serverTime as a failed time request"
```

---

## Out of scope (recorded, not fixed here)

- **A plausible but wrong `serverTime` is trusted.** A value inside the range but far ahead of the true time (for
  example a year ahead) passes every ADR-0001 P-4 gate, because the wall-clock offset is diagnostic only (decision
  D-D). The lower bound would then be in the future and a forming candle could be selected as closed. Closing this
  needs a gate on `|S − local wall|`, which changes an accepted ADR and needs the user's approval. Related to F-8 (no
  regression check across calls).
- **`AsyncCandleWriter.open` still calls `open_writer` without the pre-check.** It runs only after `_migrate` has
  verified the file one step earlier, and `CandleWriter` verifies the schema; a file swapped in between is the
  single-process assumption of INV-027.
